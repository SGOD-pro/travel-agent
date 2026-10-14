"""Evidence collector service conforming to spec 0002.

Implements source observation staging, syndication detection (AC 4), durable budget reservations
across concurrent tasks (AC 14), browser escalation forwarding (AC 13), and atomic completion
under 0001 lease fencing.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from functions.evidence_collector.fixtures import (
    RawObservation,
)
from functions.evidence_collector.models import SourceObservationModel
from functions.evidence_collector.scrapling_adapter import (
    ScraplingExtractionAdapter,
)
from functions.evidence_collector.serpapi_adapter import (
    SerpApiAdapter,
    SerpApiAdapterError,
)
from functions.orchestrator.execution.fencing import (
    TaskClaimResult,
    TaskQueueMessage,
    WorkerFencingService,
)
from functions.orchestrator.execution.models import ExecutionModel

logger = logging.getLogger(__name__)

# Default cost charged per search query tool call in minor currency units (paise).
# Matches DEFAULT_SERPAPI_COST_MINOR from serpapi_adapter (50 paise in INR).
DEFAULT_COST_PER_CALL_MINOR: int = 50

# Advance guard safety margin when checking deadlines before dispatching external operations.
DEADLINE_ADVANCE_GUARD_SECONDS: int = 20


@dataclass(frozen=True)
class TaskDispatchEnvelope:
    """Parsed outbox relay TASK_DISPATCH message envelope."""

    event_id: uuid.UUID
    execution_id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    outbox_delivery_attempts: int
    task_attempt: int
    context_version: int
    worker: str
    action: str
    inner_payload: dict[str, Any]


def parse_task_dispatch_envelope(record_or_body: dict[str, Any] | str) -> TaskDispatchEnvelope:
    """Parses and validates a TASK_DISPATCH message.

    Distinguishes top level outbox relay delivery attempts (outbox_events.attempts)
    from the inner task execution attempt (tasks.attempt).
    """
    if isinstance(record_or_body, str):
        body = json.loads(record_or_body)
    elif "body" in record_or_body:
        raw_body = record_or_body["body"]
        body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
    else:
        body = record_or_body

    kind = body.get("kind")
    if kind != "TASK_DISPATCH":
        raise ValueError(f"Unsupported event kind: {kind}, expected TASK_DISPATCH")

    outbox_delivery_attempts = int(body.get("attempts", 0))

    payload_container = body.get("payload") or body.get("payload_ref") or {}
    task_id_raw = body.get("task_id") or payload_container.get("task_id")
    execution_id_raw = body.get("execution_id") or payload_container.get("execution_id")

    if not task_id_raw or not execution_id_raw:
        raise ValueError("Missing required task_id or execution_id in TASK_DISPATCH message")

    task_id = uuid.UUID(str(task_id_raw))
    execution_id = uuid.UUID(str(execution_id_raw))
    task_attempt = int(payload_container.get("attempt", body.get("attempt", 0)))
    context_version = int(payload_container.get("context_version", body.get("context_version", 1)))
    worker = str(payload_container.get("worker", body.get("worker", "EVIDENCE")))
    action = str(payload_container.get("action", body.get("action", "collect")))
    inner_payload = payload_container.get("payload") or payload_container

    event_id_raw = body.get("event_id")
    if not event_id_raw:
        logger.warning(
            "TASK_DISPATCH message missing event_id; generating a fallback UUID. "
            "This may indicate a malformed outbox envelope."
        )
    event_id = uuid.UUID(str(event_id_raw)) if event_id_raw else uuid.uuid4()

    return TaskDispatchEnvelope(
        event_id=event_id,
        execution_id=execution_id,
        task_id=task_id,
        kind=kind,
        outbox_delivery_attempts=outbox_delivery_attempts,
        task_attempt=task_attempt,
        context_version=context_version,
        worker=worker,
        action=action,
        inner_payload=inner_payload,
    )


class EvidenceCollectorService:
    """Service managing evidence collection, observation staging, and atomic completion."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._fencing_service = WorkerFencingService(session)

    @property
    def fencing_service(self) -> WorkerFencingService:
        return self._fencing_service

    async def initialize_execution_budget_if_needed(
        self,
        execution_id: uuid.UUID,
        initial_budget: dict[str, Any],
    ) -> None:
        """Initializes executions.budget from task parameters if not already present."""
        stmt = (
            select(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .with_for_update()
        )
        res = await self._session.execute(stmt)
        execution = res.scalar_one_or_none()
        if execution and execution.budget is None and initial_budget:
            execution.budget = {
                "remaining_tool_calls": initial_budget.get("remaining_tool_calls", 5),
                "remaining_cost_minor": initial_budget.get("remaining_cost_minor", 500),
                "currency": initial_budget.get("currency", "INR"),
                "reserved_tool_calls": 0,
                "reserved_cost_minor": 0,
                "spent_tool_calls": 0,
                "spent_cost_minor": 0,
            }
            await self._session.commit()

    async def reserve_budget(
        self,
        execution_id: uuid.UUID,
        cost_minor: int,
    ) -> bool:
        """Atomically checks and reserves budget from executions.budget (AC 14).

        Guarantees safe budget bounds across concurrent tasks prior to external dispatch.
        Returns True if reservation succeeded, or False if limits are reached.
        """
        stmt = (
            select(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .with_for_update()
        )
        res = await self._session.execute(stmt)
        execution = res.scalar_one_or_none()
        if not execution:
            return False

        now = datetime.now(UTC)
        if execution.status == "CANCELLED" or execution.cancelled_at is not None:
            return False
        if execution.deadline_at < now:
            return False

        budget = dict(execution.budget or {})
        remaining_calls = 999999 if budget.get("remaining_tool_calls") is None else int(budget["remaining_tool_calls"])
        remaining_cost = 999999999 if budget.get("remaining_cost_minor") is None else int(budget["remaining_cost_minor"])

        reserved_calls = int(budget.get("reserved_tool_calls", 0))
        reserved_cost = int(budget.get("reserved_cost_minor", 0))

        available_calls = remaining_calls - reserved_calls
        available_cost = remaining_cost - reserved_cost

        if available_calls < 1 or available_cost < cost_minor:
            return False

        budget["reserved_tool_calls"] = reserved_calls + 1
        budget["reserved_cost_minor"] = reserved_cost + cost_minor
        execution.budget = budget
        await self._session.commit()
        return True

    async def reconcile_budget(
        self,
        execution_id: uuid.UUID,
        cost_minor: int,
        *,
        success: bool,
    ) -> None:
        """Reconciles durable budget reservations after external call finishes or fails (AC 14)."""
        if not getattr(self._session, "is_active", True):
            logger.warning("Session inactive; skipping budget reconciliation for %s", execution_id)
            return

        stmt = (
            select(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .with_for_update()
        )
        res = await self._session.execute(stmt)
        execution = res.scalar_one_or_none()
        if not execution or not execution.budget:
            return

        budget = dict(execution.budget)
        reserved_calls = int(budget.get("reserved_tool_calls", 0))
        reserved_cost = int(budget.get("reserved_cost_minor", 0))

        budget["reserved_tool_calls"] = max(0, reserved_calls - 1)
        budget["reserved_cost_minor"] = max(0, reserved_cost - cost_minor)

        if success:
            spent_calls = int(budget.get("spent_tool_calls", 0))
            spent_cost = int(budget.get("spent_cost_minor", 0))
            budget["spent_tool_calls"] = spent_calls + 1
            budget["spent_cost_minor"] = spent_cost + cost_minor

            if "remaining_tool_calls" in budget:
                budget["remaining_tool_calls"] = max(0, int(budget["remaining_tool_calls"]) - 1)
            if "remaining_cost_minor" in budget:
                budget["remaining_cost_minor"] = max(0, int(budget["remaining_cost_minor"]) - cost_minor)

        execution.budget = budget
        await self._session.commit()

    async def _safe_reconcile_budget(
        self,
        execution_id: uuid.UUID,
        cost_minor: int,
        *,
        success: bool,
    ) -> None:
        """Reconciles budget safely in finally blocks without suppressing caller exceptions."""
        try:
            await self.reconcile_budget(execution_id, cost_minor, success=success)
        except Exception as reconcile_err:
            logger.error(
                "Failed to reconcile budget for execution %s: %s",
                execution_id,
                reconcile_err,
            )

    async def stage_observations_and_complete(
        self,
        *,
        claim_result: TaskClaimResult,
        observations: list[RawObservation],
        usage: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        browser_requests: list[dict[str, Any]] | None = None,
        unavailable_sources: list[dict[str, Any]] | None = None,
        budget_exhausted: bool = False,
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """Stages observations, detects syndication (AC 4), and completes the task atomically.

        On duplicate snapshots for (execution_id, canonical_url, content_hash),
        inserts do nothing with zero mutations, and the existing persisted row IDs
        are queried and returned.

        Syndicated or duplicate articles sharing an identical content hash with an existing
        observation under a different canonical URL are flagged as non independent corroboration.
        """
        now = datetime.now(UTC)

        # Deduplicate observations within the current batch
        deduped_observations: list[RawObservation] = []
        seen_keys: set[tuple[str, str]] = set()
        for obs in observations:
            key = (obs.canonical_url, obs.content_hash)
            if key not in seen_keys:
                seen_keys.add(key)
                deduped_observations.append(obs)

        # Detect syndication and non independent corroboration (AC 4)
        syndication_map: dict[tuple[str, str], tuple[uuid.UUID, str]] = {}
        if deduped_observations:
            incoming_hashes = list({obs.content_hash for obs in deduped_observations})
            existing_obs_stmt = select(
                SourceObservationModel.id,
                SourceObservationModel.canonical_url,
                SourceObservationModel.content_hash,
            ).where(
                SourceObservationModel.content_hash.in_(incoming_hashes)
            )
            existing_res = await self._session.execute(existing_obs_stmt)
            existing_by_hash: dict[str, list[tuple[uuid.UUID, str]]] = {}
            for row in existing_res:
                existing_by_hash.setdefault(row.content_hash, []).append((row.id, row.canonical_url))

            for obs in deduped_observations:
                existing_matches = existing_by_hash.get(obs.content_hash, [])
                for match_id, match_url in existing_matches:
                    if match_url != obs.canonical_url:
                        syndication_map[(obs.canonical_url, obs.content_hash)] = (match_id, match_url)
                        break

        id_map: dict[tuple[str, str], uuid.UUID] = {}

        if deduped_observations:
            obs_rows = []
            for obs in deduped_observations:
                key = (obs.canonical_url, obs.content_hash)
                artifact_metadata = dict(obs.artifact_ref or {})
                if key in syndication_map:
                    match_id, match_url = syndication_map[key]
                    artifact_metadata["non_independent_corroboration"] = True
                    artifact_metadata["corroborates_observation_id"] = str(match_id)
                    artifact_metadata["syndicated_with_url"] = match_url

                obs_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "execution_id": claim_result.execution_id,
                        "source_url": obs.source_url,
                        "canonical_url": obs.canonical_url,
                        "publisher": obs.publisher,
                        "content_hash": obs.content_hash,
                        "retrieved_at": obs.retrieved_at,
                        "published_at": obs.published_at,
                        "artifact_ref": artifact_metadata or None,
                        "excerpt_text": obs.excerpt_text,
                        "excerpt_locator": obs.excerpt_locator,
                        "extraction_method": obs.extraction_method,
                        "created_at": now,
                    }
                )

            # 1. Stage observations using ON CONFLICT DO NOTHING
            insert_stmt = pg_insert(SourceObservationModel).values(obs_rows)
            insert_stmt = insert_stmt.on_conflict_do_nothing(
                index_elements=["execution_id", "canonical_url", "content_hash"]
            )
            await self._session.execute(insert_stmt)

            # 2. Query persisted row IDs for all observations
            or_clauses = [
                and_(
                    SourceObservationModel.canonical_url == obs.canonical_url,
                    SourceObservationModel.content_hash == obs.content_hash,
                )
                for obs in deduped_observations
            ]
            lookup_stmt = select(
                SourceObservationModel.id,
                SourceObservationModel.canonical_url,
                SourceObservationModel.content_hash,
            ).where(
                SourceObservationModel.execution_id == claim_result.execution_id,
                or_(*or_clauses),
            )
            res = await self._session.execute(lookup_stmt)
            for row in res:
                id_map[(row.canonical_url, row.content_hash)] = row.id

        ordered_obs_ids: list[uuid.UUID] = []
        output_observations: list[dict[str, Any]] = []
        for obs in deduped_observations:
            key = (obs.canonical_url, obs.content_hash)
            row_id = id_map[key]
            ordered_obs_ids.append(row_id)
            obs_dict: dict[str, Any] = {
                "observation_id": str(row_id),
                "canonical_url": obs.canonical_url,
                "publisher": obs.publisher,
                "content_hash": obs.content_hash,
                "retrieved_at": obs.retrieved_at.isoformat(),
                "excerpt_text": obs.excerpt_text,
                "extraction_method": obs.extraction_method,
            }
            if key in syndication_map:
                match_id, match_url = syndication_map[key]
                obs_dict["non_independent_corroboration"] = True
                obs_dict["corroborates_observation_id"] = str(match_id)
                obs_dict["syndicated_with_url"] = match_url

            output_observations.append(obs_dict)

        output_payload = {
            "observations": output_observations,
            "unavailable_sources": unavailable_sources or [],
            "browser_requests": browser_requests or [],
            "usage": usage or {},
            "budget_exhausted": budget_exhausted,
        }

        # 3. Call complete_task: commits staged observations and completion atomically on success
        try:
            result_id = await self._fencing_service.complete_task(
                execution_id=claim_result.execution_id,
                task_id=claim_result.task_id,
                lease_token=claim_result.lease_token,
                output=output_payload,
                expected_context_version=claim_result.context_version,
                warnings=warnings or [],
                usage=usage or {},
            )
        except Exception:
            await self._session.rollback()
            raise

        return result_id, ordered_obs_ids

    async def execute_collection_task(
        self,
        envelope: TaskDispatchEnvelope,
        *,
        adapter: Any | None = None,
        extraction_adapter: Any | None = None,
        lease_seconds: int = 300,
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """Claims task, enforces durable budget reservations (AC 14), and completes atomically."""
        # 1. Fenced claim using task execution attempt
        queue_msg = TaskQueueMessage(
            task_id=envelope.task_id,
            execution_id=envelope.execution_id,
            attempt=envelope.task_attempt,
        )
        claim_result = await self._fencing_service.claim_task(
            message=queue_msg,
            worker=envelope.worker,
            action=envelope.action,
            expected_context_version=envelope.context_version,
            lease_seconds=lease_seconds,
        )

        # 2. Authoritative database payload
        db_payload = claim_result.payload_ref or envelope.inner_payload
        queries = db_payload.get("queries", [])
        if isinstance(queries, str):
            queries = [queries]

        target_urls = db_payload.get("urls", []) or db_payload.get("pages", [])
        if isinstance(target_urls, str):
            target_urls = [target_urls]

        budget = db_payload.get("budget", {})
        currency = budget.get("currency", "INR")
        cost_per_call = int(budget.get("cost_per_call_minor", DEFAULT_COST_PER_CALL_MINOR))

        # Initialize shared executions.budget if needed
        await self.initialize_execution_budget_if_needed(envelope.execution_id, budget)

        active_adapter = adapter if adapter is not None else SerpApiAdapter()
        active_extraction_adapter = (
            extraction_adapter if extraction_adapter is not None else ScraplingExtractionAdapter()
        )
        calls_made = 0
        cost_spent = 0
        budget_exhausted = False
        collected_observations: list[RawObservation] = []
        emitted_browser_requests: list[dict[str, Any]] = []
        unavailable_sources: list[dict[str, Any]] = []

        try:
            # 3. Process search queries with durable pre call budget reservations (AC 14)
            for q in queries:
                # Check deadline before dispatch
                now = datetime.now(UTC)
                stmt = select(ExecutionModel).where(ExecutionModel.id == envelope.execution_id)
                res = await self._session.execute(stmt)
                exec_model = res.scalar_one_or_none()
                if exec_model:
                    if exec_model.status == "CANCELLED" or exec_model.cancelled_at is not None:
                        logger.info("Parent execution cancelled; halting collection loop")
                        break
                    if (
                        exec_model.deadline_at
                        and (now + timedelta(seconds=DEADLINE_ADVANCE_GUARD_SECONDS)) >= exec_model.deadline_at
                    ):
                        logger.warning("Execution deadline approaching; halting collection loop")
                        break

                # Missing configuration must produce explicit failure or unavailable result
                if isinstance(active_adapter, SerpApiAdapter) and not active_adapter._api_key:
                    logger.warning(
                        "SERPAPI_API_KEY is not configured; search provider unavailable for query '%s'",
                        q,
                    )
                    unavailable_sources.append({
                        "source_type": "SEARCH",
                        "query": q,
                        "status": "UNAVAILABLE",
                        "error": "SERPAPI_API_KEY is not configured; live search requires valid credentials",
                    })
                    continue

                # Atomically reserve budget in executions.budget
                has_budget = await self.reserve_budget(envelope.execution_id, cost_per_call)
                if not has_budget:
                    budget_exhausted = True
                    logger.info("Budget exhausted for execution %s; stopping queries", envelope.execution_id)
                    break

                # Execute provider call outside database transaction
                call_success = False
                try:
                    results = await active_adapter.search(q, limit=5)
                    collected_observations.extend(results)
                    calls_made += 1
                    cost_spent += cost_per_call
                    call_success = True
                except SerpApiAdapterError as e:
                    logger.warning("SerpApi search failed for query '%s': %s", q, e)
                    unavailable_sources.append({
                        "source_type": "SEARCH",
                        "query": q,
                        "status": "UNAVAILABLE",
                        "error": str(e),
                    })
                finally:
                    await self._safe_reconcile_budget(
                        envelope.execution_id,
                        cost_per_call,
                        success=call_success,
                    )

            # 4. Process web page extractions if provided
            if target_urls:
                for url_entry in target_urls:
                    if isinstance(url_entry, dict):
                        page_url = str(url_entry.get("url") or "")
                        page_cat = str(url_entry.get("category") or "general")
                    else:
                        page_url = str(url_entry)
                        page_cat = "general"

                    if not page_url:
                        continue

                    has_budget = await self.reserve_budget(envelope.execution_id, cost_per_call)
                    if not has_budget:
                        budget_exhausted = True
                        break

                    call_success = False
                    try:
                        if hasattr(active_extraction_adapter, "extract_page"):
                            obs_list, b_reqs = await active_extraction_adapter.extract_page(
                                page_url, category=page_cat
                            )
                            collected_observations.extend(obs_list)
                            emitted_browser_requests.extend(b_reqs)
                        elif hasattr(active_extraction_adapter, "extract"):
                            extracted_obs = await active_extraction_adapter.extract(page_url)
                            if extracted_obs:
                                collected_observations.append(extracted_obs)
                        calls_made += 1
                        cost_spent += cost_per_call
                        call_success = True
                    except Exception as e:
                        logger.warning("Extraction failed for URL '%s': %s", page_url, e)
                        unavailable_sources.append({
                            "source_type": "WEB_EXTRACTION",
                            "url": page_url,
                            "status": "UNAVAILABLE",
                            "error": str(e),
                        })
                    finally:
                        await self._safe_reconcile_budget(
                            envelope.execution_id,
                            cost_per_call,
                            success=call_success,
                        )

            usage_summary = {
                "tool_calls_made": calls_made,
                "cost_minor": cost_spent,
                "currency": currency,
            }

            # 5. Stage observations and complete atomically
            return await self.stage_observations_and_complete(
                claim_result=claim_result,
                observations=collected_observations,
                usage=usage_summary,
                browser_requests=emitted_browser_requests,
                unavailable_sources=unavailable_sources,
                budget_exhausted=budget_exhausted,
            )
        except Exception as e:
            logger.exception("Evidence collection execution failed for task %s: %s", envelope.task_id, e)
            try:
                await self._fencing_service.fail_task(
                    task_id=envelope.task_id,
                    execution_id=envelope.execution_id,
                    lease_token=claim_result.lease_token,
                    expected_context_version=envelope.context_version,
                    error={"message": str(e), "error_type": type(e).__name__},
                )
            except Exception as fail_err:
                logger.error("Failed to fence-fail task %s: %s", envelope.task_id, fail_err)
            raise
