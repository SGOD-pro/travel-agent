"""Evidence collector service conforming to spec 0002 Slice 1.

Implements offline fixture collection, immutable source observation persistence,
and atomic task completion under 0001 lease fencing.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from functions.evidence_collector.fixtures import (
    FixtureSearchAdapter,
    RawObservation,
)
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.execution.fencing import (
    TaskClaimResult,
    TaskQueueMessage,
    WorkerFencingService,
)

logger = logging.getLogger(__name__)

# Cost charged per fixture tool call (minor currency units, e.g. paise).
_COST_PER_CALL_MINOR: int = 50


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

    # Top level attempts represent outbox relay delivery transport retries
    outbox_delivery_attempts = int(body.get("attempts", 0))

    payload_container = body.get("payload") or {}
    task_id_raw = body.get("task_id") or payload_container.get("task_id")
    execution_id_raw = body.get("execution_id") or payload_container.get("execution_id")

    if not task_id_raw or not execution_id_raw:
        raise ValueError("Missing required task_id or execution_id in TASK_DISPATCH message")

    task_id = uuid.UUID(str(task_id_raw))
    execution_id = uuid.UUID(str(execution_id_raw))

    # Task execution attempt is in body["payload"]["attempt"]
    task_attempt = int(payload_container.get("attempt", 0))

    # Workflow context version is in body["payload"]["context_version"]
    context_version = int(payload_container.get("context_version", 1))

    worker = str(payload_container.get("worker", "EVIDENCE"))
    action = str(payload_container.get("action", "collect"))

    inner_payload = payload_container.get("payload") or {}

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

    async def stage_observations_and_complete(
        self,
        *,
        claim_result: TaskClaimResult,
        observations: list[RawObservation],
        usage: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        budget_exhausted: bool = False,
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """Stages observations in the shared session and completes the task atomically.

        On duplicate snapshots for (execution_id, canonical_url, content_hash),
        inserts do nothing with zero mutations, and the existing persisted row IDs
        are queried and returned.

        The except-Exception block rolls back ALL failures raised by complete_task:
        both the pre-savepoint lease checks (LeaseFencingError, ParentCancelledError,
        DeadlineExceededError) and any exception raised inside complete_task's own
        begin_nested() savepoint (e.g. from record_job_event). In all cases the
        staged but uncommitted observations are discarded cleanly.
        """
        now = datetime.now(UTC)

        # Deduplicate observations within the current batch to avoid SQL duplicate key errors
        deduped_observations: list[RawObservation] = []
        seen_keys: set[tuple[str, str]] = set()
        for obs in observations:
            key = (obs.canonical_url, obs.content_hash)
            if key not in seen_keys:
                seen_keys.add(key)
                deduped_observations.append(obs)

        id_map: dict[tuple[str, str], uuid.UUID] = {}

        if deduped_observations:
            obs_rows = [
                {
                    "id": uuid.uuid4(),
                    "execution_id": claim_result.execution_id,
                    "source_url": obs.source_url,
                    "canonical_url": obs.canonical_url,
                    "publisher": obs.publisher,
                    "content_hash": obs.content_hash,
                    "retrieved_at": obs.retrieved_at,
                    "published_at": obs.published_at,
                    "artifact_ref": obs.artifact_ref,
                    "excerpt_text": obs.excerpt_text,
                    "excerpt_locator": obs.excerpt_locator,
                    "extraction_method": obs.extraction_method,
                    "created_at": now,
                }
                for obs in deduped_observations
            ]

            # 1. Stage observations using ON CONFLICT DO NOTHING (zero DO UPDATE)
            insert_stmt = pg_insert(SourceObservationModel).values(obs_rows)
            insert_stmt = insert_stmt.on_conflict_do_nothing(
                index_elements=["execution_id", "canonical_url", "content_hash"]
            )
            await self._session.execute(insert_stmt)

            # 2. Query persisted row IDs for all observations (both existing duplicates and new rows)
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
            row_id = id_map[(obs.canonical_url, obs.content_hash)]
            ordered_obs_ids.append(row_id)
            output_observations.append(
                {
                    "observation_id": str(row_id),
                    "canonical_url": obs.canonical_url,
                    "publisher": obs.publisher,
                    "content_hash": obs.content_hash,
                    "retrieved_at": obs.retrieved_at.isoformat(),
                    "excerpt_text": obs.excerpt_text,
                    "extraction_method": obs.extraction_method,
                }
            )

        output_payload = {
            "observations": output_observations,
            "unavailable_sources": [],
            "browser_requests": [],
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
            # Fencing rejection, internal savepoint failure, or any database error inside complete_task:
            # roll back all staged observations so no uncommitted or orphaned snapshot rows persist.
            await self._session.rollback()
            raise

        return result_id, ordered_obs_ids

    async def execute_collection_task(
        self,
        envelope: TaskDispatchEnvelope,
        *,
        adapter: FixtureSearchAdapter | None = None,
        lease_seconds: int = 300,
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """Claims task, executes offline collection via fixture adapter, and completes atomically."""
        # 1. Fenced claim using task execution attempt (NOT outbox delivery attempts)
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

        # 2. Load parameters from the authoritative database payload_ref
        db_payload = claim_result.payload_ref or envelope.inner_payload
        queries = db_payload.get("queries", [])
        if isinstance(queries, str):
            queries = [queries]

        budget = db_payload.get("budget", {})
        remaining_tool_calls = budget.get("remaining_tool_calls", 5)
        remaining_cost_minor = budget.get("remaining_cost_minor", 500)
        currency = budget.get("currency", "INR")

        active_adapter = adapter or FixtureSearchAdapter()

        # 3. Local attempt loop bounded by in memory guards
        calls_made = 0
        cost_spent = 0
        budget_exhausted = False
        collected_observations: list[RawObservation] = []

        for q in queries:
            if calls_made >= remaining_tool_calls:
                budget_exhausted = True
                break

            results = await active_adapter.search(q, limit=5)
            collected_observations.extend(results)
            calls_made += 1
            cost_spent += _COST_PER_CALL_MINOR

        usage_summary = {
            "tool_calls_made": calls_made,
            "cost_minor": min(cost_spent, remaining_cost_minor),
            "currency": currency,
        }

        # 4. Stage observations and complete atomically
        return await self.stage_observations_and_complete(
            claim_result=claim_result,
            observations=collected_observations,
            usage=usage_summary,
            budget_exhausted=budget_exhausted,
        )
