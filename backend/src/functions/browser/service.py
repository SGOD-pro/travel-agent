"""Browser worker service conforming to spec 0003 Slice 3.

Implements fenced task claims (AC-3), browser rendering execution via extraction port (AC-5),
Scrapling entity parsing and immutable observation persistence (AC-6), and graceful
error degradation for timeouts and bot challenges (AC-7).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.browser import (
    BrowserExtractionPort,
    BrowserRenderResult,
    BrowserRenderStatus,
    BrowserResourceUsage,
    BrowserTaskPayload,
    BrowserWorkerOutput,
)
from functions.browser.models import SourceObservationModel
from functions.browser.parser import parse_rendered_entities
from functions.orchestrator.execution.fencing import (
    TaskClaimResult,
    TaskQueueMessage,
    WorkerFencingService,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrowserDispatchEnvelope:
    """Parsed dispatch envelope for browser worker tasks."""

    task_id: uuid.UUID
    execution_id: uuid.UUID
    worker: str
    action: str
    payload: dict[str, Any]
    attempt: int
    context_version: int

    def to_task_queue_message(self) -> TaskQueueMessage:
        """Converts to TaskQueueMessage for fencing claim evaluation."""
        return TaskQueueMessage(
            task_id=self.task_id,
            execution_id=self.execution_id,
            attempt=self.attempt,
        )


def parse_browser_dispatch_message(
    record_or_body: dict[str, Any] | str,
) -> BrowserDispatchEnvelope:
    """Parses an SQS record or body into a normalized BrowserDispatchEnvelope.

    Supports both direct task dispatch structures and wrapped outbox event envelopes.
    """
    if isinstance(record_or_body, str):
        body = json.loads(record_or_body)
    elif "body" in record_or_body:
        raw = record_or_body["body"]
        body = json.loads(raw) if isinstance(raw, str) else raw
    else:
        body = record_or_body

    payload_container = body.get("payload") or body.get("payload_ref") or {}
    task_id_raw = body.get("task_id") or payload_container.get("task_id")
    execution_id_raw = body.get("execution_id") or payload_container.get("execution_id")

    if not task_id_raw or not execution_id_raw:
        raise ValueError("Missing required task_id or execution_id in task message")

    task_id = uuid.UUID(str(task_id_raw))
    execution_id = uuid.UUID(str(execution_id_raw))
    attempt = int(payload_container.get("attempt", body.get("attempt", 0)))
    context_version = int(payload_container.get("context_version", body.get("context_version", 1)))
    worker = str(payload_container.get("worker", body.get("worker", "BROWSER")))
    action = str(payload_container.get("action", body.get("action", "render")))

    inner_payload = payload_container.get("payload") or payload_container

    return BrowserDispatchEnvelope(
        task_id=task_id,
        execution_id=execution_id,
        worker=worker,
        action=action,
        payload=inner_payload,
        attempt=attempt,
        context_version=context_version,
    )


class BrowserWorkerService:
    """Fenced worker service executing browser rendering and observation persistence."""

    def __init__(
        self,
        session: AsyncSession,
        extraction_port: BrowserExtractionPort,
        fencing_service: WorkerFencingService | None = None,
    ) -> None:
        self._session = session
        self._port = extraction_port
        self._fencing_service = fencing_service or WorkerFencingService(session)

    async def execute_render_task(
        self,
        message: BrowserDispatchEnvelope | TaskQueueMessage | dict[str, Any],
    ) -> tuple[uuid.UUID, BrowserWorkerOutput]:
        """Claims and executes a browser rendering task under lease fencing.

        Conforms to spec 0003:
        - AC-3: Validates parent execution status, context version, and attempt counter under fencing.
        - AC-5: Executes rendering via abstract BrowserExtractionPort.
        - AC-6: Parsed content persisted as immutable SourceObservationModel (BROWSER_HTML).
        - AC-7: Degraded outcomes (TIMED_OUT, BLOCKED, UNAVAILABLE) complete gracefully as success.
        """
        # 1. Normalize message and claim task under parent-first lease fencing (AC-3)
        if isinstance(message, BrowserDispatchEnvelope):
            task_msg = message.to_task_queue_message()
            context_version = message.context_version
            worker = message.worker
            action = message.action
        elif isinstance(message, dict):
            envelope = parse_browser_dispatch_message(message)
            task_msg = envelope.to_task_queue_message()
            context_version = envelope.context_version
            worker = envelope.worker
            action = envelope.action
        else:
            task_msg = message
            context_version = 1
            worker = "BROWSER"
            action = "render"

        claim_result: TaskClaimResult = await self._fencing_service.claim_task(
            message=task_msg,
            worker=worker,
            action=action,
            expected_context_version=context_version,
            lease_seconds=120,
        )

        # 2. Extract and validate BrowserTaskPayload
        payload_data = claim_result.payload_ref
        if "payload" in payload_data and isinstance(payload_data["payload"], dict):
            payload_data = payload_data["payload"]

        payload = BrowserTaskPayload.model_validate(payload_data)

        # 3. Execute page navigation via extraction port with error protection (AC-5, AC-7)
        render_result = await self._safe_render_page(payload)

        # 4. Handle SUCCESS outcome with immutable observation persistence (AC-6)
        if render_result.status == BrowserRenderStatus.SUCCESS:
            return await self._persist_success_and_complete(claim_result, payload, render_result)

        # 5. Handle degraded outcome (UNAVAILABLE, TIMED_OUT, BLOCKED) (AC-7)
        return await self._persist_degraded_and_complete(claim_result, payload, render_result)

    async def _safe_render_page(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
        """Executes render_page catching unexpected exceptions and converting to degraded outcome."""
        try:
            return await self._port.render_page(payload)
        except Exception as exc:
            logger.warning("Browser rendering encountered exception for %s: %s", payload.canonical_url, exc)
            exc_str = str(exc)
            is_timeout = "timeout" in exc_str.lower()
            is_blocked = "blocked" in exc_str.lower() or "403" in exc_str or "captcha" in exc_str.lower()

            status = BrowserRenderStatus.TIMED_OUT if is_timeout else (
                BrowserRenderStatus.BLOCKED if is_blocked else BrowserRenderStatus.UNAVAILABLE
            )
            reason = exc_str[:500] if exc_str.strip() else f"Browser render failed: {status}"

            return BrowserRenderResult(
                status=status,
                canonical_url=payload.canonical_url,
                effective_url=payload.url,
                content_hash=None,
                excerpt_text=None,
                excerpt_locator=None,
                rendered_html_snippet=None,
                render_duration_ms=payload.timeout_seconds * 1000,
                unavailable_reason=reason,
                usage=BrowserResourceUsage(duration_ms=payload.timeout_seconds * 1000, memory_mb=512),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )

    async def _persist_success_and_complete(
        self,
        claim_result: TaskClaimResult,
        payload: BrowserTaskPayload,
        render_result: BrowserRenderResult,
    ) -> tuple[uuid.UUID, BrowserWorkerOutput]:
        """Persists clean immutable observation and completes task atomically under lease fencing."""
        now = datetime.now(UTC)

        # Parse entities from HTML using Scrapling if not already supplied by adapter (AC-6)
        parsed_observations: list[dict[str, Any]] = []
        if render_result.parsed_lodging_observations:
            parsed_observations.extend(
                [obs.model_dump(mode="json") for obs in render_result.parsed_lodging_observations]
            )
        elif render_result.parsed_transit_observations:
            parsed_observations.extend(
                [obs.model_dump(mode="json") for obs in render_result.parsed_transit_observations]
            )
        elif render_result.rendered_html_snippet:
            parsed_observations = parse_rendered_entities(
                html_content=render_result.rendered_html_snippet,
                category=payload.category,
                canonical_url=payload.canonical_url,
            )

        # Assemble immutable source observation record
        publisher = urlparse(payload.canonical_url).netloc.lower() or "unknown"
        artifact_ref: dict[str, Any] = {
            "status": "SUCCESS",
            "render_duration_ms": render_result.render_duration_ms,
            "wait_until": payload.wait_until.value,
        }
        if payload.placeholder_observation_id:
            artifact_ref["placeholder_observation_id"] = str(payload.placeholder_observation_id)
            artifact_ref["original_placeholder_observation_id"] = str(payload.placeholder_observation_id)
        if render_result.rendered_html_snippet:
            artifact_ref["rendered_html_snippet"] = render_result.rendered_html_snippet
        if parsed_observations:
            artifact_ref["parsed_observations"] = parsed_observations

        obs_id = uuid.uuid4()
        obs_row = {
            "id": obs_id,
            "execution_id": claim_result.execution_id,
            "source_url": payload.url,
            "canonical_url": payload.canonical_url,
            "publisher": publisher,
            "content_hash": render_result.content_hash,
            "retrieved_at": now,
            "published_at": None,
            "artifact_ref": artifact_ref,
            "excerpt_text": render_result.excerpt_text,
            "excerpt_locator": render_result.excerpt_locator or "body",
            "extraction_method": "BROWSER_HTML",
            "created_at": now,
        }

        # Insert observation with ON CONFLICT DO NOTHING (AC-6 write-once immutability)
        insert_stmt = pg_insert(SourceObservationModel).values(obs_row)
        insert_stmt = insert_stmt.on_conflict_do_nothing(
            index_elements=["execution_id", "canonical_url", "content_hash"]
        )
        await self._session.execute(insert_stmt)

        # Query back persisted row ID
        lookup_stmt = select(SourceObservationModel.id).where(
            SourceObservationModel.execution_id == claim_result.execution_id,
            SourceObservationModel.canonical_url == payload.canonical_url,
            SourceObservationModel.content_hash == render_result.content_hash,
        )
        persisted_obs_id = (await self._session.execute(lookup_stmt)).scalar_one_or_none() or obs_id

        rendered_html_bytes = (
            len(render_result.rendered_html_snippet.encode("utf-8"))
            if render_result.rendered_html_snippet
            else None
        )

        worker_output = BrowserWorkerOutput(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url=payload.canonical_url,
            effective_url=render_result.effective_url or payload.url,
            observation_id=persisted_obs_id,
            content_hash=render_result.content_hash,
            rendered_html_bytes=rendered_html_bytes,
            render_duration_ms=render_result.render_duration_ms,
            unavailable_sources=[],
            parsed_observations=parsed_observations,
            usage=render_result.usage,
        )

        # Complete task under lease fencing atomically
        result_id = await self._fencing_service.complete_task(
            execution_id=claim_result.execution_id,
            task_id=claim_result.task_id,
            lease_token=claim_result.lease_token,
            output=worker_output.model_dump(mode="json"),
            expected_context_version=claim_result.context_version,
            usage=render_result.usage.model_dump(mode="json"),
        )

        return result_id, worker_output

    async def _persist_degraded_and_complete(
        self,
        claim_result: TaskClaimResult,
        payload: BrowserTaskPayload,
        render_result: BrowserRenderResult,
    ) -> tuple[uuid.UUID, BrowserWorkerOutput]:
        """Completes task under lease fencing recording unavailable source entry without fabricating content."""
        reason = render_result.unavailable_reason or f"Browser render ended with status {render_result.status}"
        unavailable_sources = [
            {
                "url": payload.canonical_url,
                "reason": reason,
            }
        ]

        worker_output = BrowserWorkerOutput(
            status=render_result.status,
            canonical_url=payload.canonical_url,
            effective_url=render_result.effective_url or payload.url,
            observation_id=None,
            content_hash=None,
            rendered_html_bytes=0,
            render_duration_ms=render_result.render_duration_ms,
            unavailable_sources=unavailable_sources,
            parsed_observations=[],
            usage=render_result.usage,
        )

        # Complete task as SUCCESS in worker_results so parent workflow continues smoothly (AC-7)
        result_id = await self._fencing_service.complete_task(
            execution_id=claim_result.execution_id,
            task_id=claim_result.task_id,
            lease_token=claim_result.lease_token,
            output=worker_output.model_dump(mode="json"),
            expected_context_version=claim_result.context_version,
            warnings=[reason],
            usage=render_result.usage.model_dump(mode="json"),
        )

        return result_id, worker_output
