"""Integration tests for worker invocation deadline and SQS partial batch handling.

Addresses audit finding 1:
- Lambda consumers enforce the remaining invocation deadline for running work,
  reserving time for cancellation, cleanup, and the partial batch response.
- Unfinished, cancelled, or failed records return their message IDs in batchItemFailures
  for SQS retry.
- Successful records are committed and omitted from batchItemFailures so SQS deletes them.
- Cancellation or timeout after provider dispatch preserves uncertain budget spend
  under executions.budget rather than refunding it.
- Benign fencing claim conflicts and duplicates are safely discarded without DLQ redrive.

Execution Environment Note:
- In AWS Lambda, Lambda passes a context object whose get_remaining_time_in_millis()
  reflects the hardware container lifetime. If Lambda's remaining time reaches 0, the
  runtime terminates the process via SIGKILL, preventing the handler from returning
  the partial batch response to SQS.
- In these integration tests, SimulatedLambdaContext reproduces that exact contract
  locally using monotonic wall clock time, proving that worker consumers proactively
  enforce the usable deadline, cancel pending coroutines, execute cleanup, and return
  batchItemFailures before the container deadline expires.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from contracts.browser import (
    BrowserExtractionPort,
    BrowserRenderResult,
    BrowserRenderStatus,
    BrowserResourceUsage,
    BrowserTaskPayload,
)
from functions.api.models import TripModel
from functions.browser.consumer import process_batch as browser_process_batch
from functions.evidence_collector.consumer import (
    process_batch as evidence_process_batch,
)
from functions.evidence_collector.fixtures import (
    FixtureSearchAdapter,
    FixtureWebExtractionAdapter,
    RawObservation,
)
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.execution.fencing import (
    ExecutionModel,
    TaskModel,
    WorkerResultModel,
)


class SimulatedLambdaContext:
    """Simulates AWS Lambda context with countdown get_remaining_time_in_millis."""

    def __init__(self, total_duration_ms: int) -> None:
        self._start = time.monotonic()
        self._total_duration_ms = total_duration_ms

    def get_remaining_time_in_millis(self) -> int:
        elapsed_ms = (time.monotonic() - self._start) * 1000.0
        remaining = self._total_duration_ms - elapsed_ms
        return max(0, int(remaining))


class HangingSearchAdapter:
    """Search adapter that simulates a hanging or slow third party provider call."""

    def __init__(self, hang_seconds: float = 3.0) -> None:
        self.hang_seconds = hang_seconds
        self.dispatched = False
        self.cancelled = False

    async def search(self, query: str, limit: int = 5, extra_params: Any = None) -> list[RawObservation]:
        self.dispatched = True
        try:
            await asyncio.sleep(self.hang_seconds)
            return [
                RawObservation.create(
                    source_url="https://example.com/slow-result",
                    publisher="Slow Provider",
                    excerpt_text=f"Slow search result for {query}",
                )
            ]
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class HangingBrowserAdapter(BrowserExtractionPort):
    """Browser adapter that simulates a hanging headless browser navigation."""

    def __init__(self, hang_seconds: float = 3.0) -> None:
        self.hang_seconds = hang_seconds
        self.called = False
        self.cancelled = False

    async def render_page(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
        self.called = True
        try:
            await asyncio.sleep(self.hang_seconds)
            return BrowserRenderResult(
                status=BrowserRenderStatus.SUCCESS,
                canonical_url=payload.canonical_url,
                effective_url=payload.url,
                content_hash="mock-hash",
                excerpt_text="Rendered content after delay",
                excerpt_locator=None,
                rendered_html_snippet="<html><body>Rendered</body></html>",
                render_duration_ms=int(self.hang_seconds * 1000),
                unavailable_reason=None,
                usage=BrowserResourceUsage(duration_ms=int(self.hang_seconds * 1000), memory_mb=256),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class FastBrowserAdapter(BrowserExtractionPort):
    """Fast browser adapter completing within milliseconds."""

    async def render_page(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
        await asyncio.sleep(0.02)
        valid_hash = "a" * 64
        return BrowserRenderResult(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url=payload.canonical_url,
            effective_url=payload.url,
            content_hash=valid_hash,
            excerpt_text="Fast rendered excerpt",
            excerpt_locator="main > article",
            rendered_html_snippet="<html><body>Fast content</body></html>",
            render_duration_ms=20,
            unavailable_reason=None,
            usage=BrowserResourceUsage(duration_ms=20, memory_mb=128),
            parsed_lodging_observations=[],
            parsed_transit_observations=[],
        )


async def create_db_task(
    session_factory: Any,
    *,
    worker: str,
    action: str,
    payload: dict[str, Any],
    initial_budget: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Sets up an execution and task on PostgreSQL for integration tests."""
    execution_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    task_id = uuid.uuid4()
    now = datetime.now(UTC)

    budget_data = initial_budget or {
        "remaining_tool_calls": 5,
        "remaining_cost_minor": 500,
        "currency": "INR",
        "reserved_tool_calls": 0,
        "reserved_cost_minor": 0,
        "spent_tool_calls": 0,
        "spent_cost_minor": 0,
        "uncertain_tool_calls": 0,
        "uncertain_cost_minor": 0,
        "reservations": {},
    }

    async with session_factory() as session:
        trip = TripModel(
            id=trip_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(trip)

        execution = ExecutionModel(
            id=execution_id,
            owner_id=owner_id,
            trip_id=trip_id,
            workflow="TRAVEL",
            status="RUNNING",
            context_version=1,
            budget=budget_data,
            deadline_at=now + timedelta(minutes=30),
            created_at=now,
            updated_at=now,
        )
        session.add(execution)

        task = TaskModel(
            id=task_id,
            execution_id=execution_id,
            parent_task_id=None,
            worker=worker,
            action=action,
            payload_ref=payload,
            input_hash="hash-" + str(task_id)[:8],
            idempotency_key="idem-" + str(task_id),
            attempt=0,
            max_attempts=3,
            context_version=1,
            status="PENDING",
            deadline_at=now + timedelta(minutes=15),
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.commit()

    return execution_id, task_id


def build_sqs_message(
    *,
    message_id: str,
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    worker: str,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Constructs a normalized SQS record."""
    return {
        "messageId": message_id,
        "receiptHandle": f"receipt-{message_id}",
        "body": {
            "kind": "TASK_DISPATCH",
            "task_id": str(task_id),
            "execution_id": str(execution_id),
            "worker": worker,
            "action": action,
            "attempt": 0,
            "context_version": 1,
            "payload": payload,
        },
    }


# =============================================================================
# 1. Audit reproduction and mixed fast/slow records test (Evidence Collector)
# =============================================================================
@pytest.mark.asyncio
async def test_evidence_collector_mixed_fast_and_slow_records_partial_batch(
    session_factory: Any,
) -> None:
    """Verifies that evidence collector consumer enforces usable deadline on running work.

    Fast record finishes and commits. Slow record times out during search, cleans up,
    preserves uncertain spend, and is returned in batchItemFailures for SQS retry.
    """
    # 1. Create fast task
    fast_payload = {"queries": ["fast query"], "budget": {"remaining_cost_minor": 100}}
    eid_fast, tid_fast = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload=fast_payload,
    )
    rec_fast = build_sqs_message(
        message_id="msg-fast-evidence",
        execution_id=eid_fast,
        task_id=tid_fast,
        worker="EVIDENCE",
        action="collect",
        payload=fast_payload,
    )

    # 2. Create slow task
    slow_payload = {"queries": ["slow query"], "budget": {"remaining_cost_minor": 100}}
    eid_slow, tid_slow = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload=slow_payload,
    )
    rec_slow = build_sqs_message(
        message_id="msg-slow-evidence",
        execution_id=eid_slow,
        task_id=tid_slow,
        worker="EVIDENCE",
        action="collect",
        payload=slow_payload,
    )

    # 3. Simulate Lambda context: 800ms total, reserve 300ms => usable time is 500ms
    ctx = SimulatedLambdaContext(total_duration_ms=800)
    slow_adapter = HangingSearchAdapter(hang_seconds=2.0)

    # Compound adapter dispatching fast query to fixture and slow query to hanging adapter
    class CompoundAdapter:
        def __init__(self) -> None:
            self.fixture = FixtureSearchAdapter()
            self.hanging = slow_adapter

        async def search(self, query: str, limit: int = 5, extra_params: Any = None) -> list[RawObservation]:
            if "slow" in query:
                return await self.hanging.search(query, limit=limit, extra_params=extra_params)
            await asyncio.sleep(0.02)
            return await self.fixture.search(query, limit=limit)

    event = {"Records": [rec_fast, rec_slow]}
    t0 = time.monotonic()
    failures = await evidence_process_batch(
        event,
        context=ctx,
        session_factory=session_factory,
        adapter=CompoundAdapter(),
        extraction_adapter=FixtureWebExtractionAdapter(),
        reserve_ms=300,
    )
    elapsed = time.monotonic() - t0

    # Bounded execution: must return before total duration (800ms) with reserve window intact
    assert elapsed < 0.75, f"Execution took {elapsed:.2f}s, did not respect deadline reserve"

    # Only slow record returned for SQS retry
    assert len(failures) == 1
    assert failures[0]["itemIdentifier"] == "msg-slow-evidence"
    assert slow_adapter.cancelled is True, "Hanging provider call must be cancelled on deadline"

    # Verify database state
    async with session_factory() as session:
        # Fast task committed successfully
        fast_task = (
            await session.execute(select(TaskModel).where(TaskModel.id == tid_fast))
        ).scalar_one()
        assert fast_task.status == "COMPLETED"

        fast_result = (
            await session.execute(select(WorkerResultModel).where(WorkerResultModel.task_id == tid_fast))
        ).scalar_one_or_none()
        assert fast_result is not None

        # Slow task was not marked COMPLETED
        slow_task = (
            await session.execute(select(TaskModel).where(TaskModel.id == tid_slow))
        ).scalar_one()
        assert slow_task.status != "COMPLETED"


# =============================================================================
# 2. Browser worker mixed fast and slow records test
# =============================================================================
@pytest.mark.asyncio
async def test_browser_worker_mixed_fast_and_slow_records_partial_batch(
    session_factory: Any,
) -> None:
    """Verifies that browser worker consumer enforces usable deadline on running render work.

    Fast render finishes and commits. Slow render times out during page navigation,
    is cancelled, and returned in batchItemFailures. Fast record is omitted.
    """
    eid_fast, tid_fast = await create_db_task(
        session_factory,
        worker="BROWSER",
        action="render",
        payload={
            "url": "https://example.com/fast-hotel",
            "canonical_url": "https://example.com/fast-hotel",
            "category": "lodging",
            "reason": "FAST_TEST",
        },
    )
    rec_fast = build_sqs_message(
        message_id="msg-fast-browser",
        execution_id=eid_fast,
        task_id=tid_fast,
        worker="BROWSER",
        action="render",
        payload={
            "url": "https://example.com/fast-hotel",
            "canonical_url": "https://example.com/fast-hotel",
            "category": "lodging",
            "reason": "FAST_TEST",
        },
    )

    eid_slow, tid_slow = await create_db_task(
        session_factory,
        worker="BROWSER",
        action="render",
        payload={
            "url": "https://example.com/slow-hotel",
            "canonical_url": "https://example.com/slow-hotel",
            "category": "lodging",
            "reason": "SLOW_TEST",
        },
    )
    rec_slow = build_sqs_message(
        message_id="msg-slow-browser",
        execution_id=eid_slow,
        task_id=tid_slow,
        worker="BROWSER",
        action="render",
        payload={
            "url": "https://example.com/slow-hotel",
            "canonical_url": "https://example.com/slow-hotel",
            "category": "lodging",
            "reason": "SLOW_TEST",
        },
    )

    ctx = SimulatedLambdaContext(total_duration_ms=700)
    slow_browser = HangingBrowserAdapter(hang_seconds=2.0)
    fast_browser = FastBrowserAdapter()

    class CompoundBrowserAdapter(BrowserExtractionPort):
        async def render_page(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
            if "slow" in payload.canonical_url:
                return await slow_browser.render_page(payload)
            return await fast_browser.render_page(payload)

    event = {"Records": [rec_fast, rec_slow]}
    t0 = time.monotonic()
    failures = await browser_process_batch(
        event,
        context=ctx,
        session_factory=session_factory,
        adapter=CompoundBrowserAdapter(),
        reserve_ms=250,
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 0.65, f"Browser execution took {elapsed:.2f}s, did not respect reserve"
    assert len(failures) == 1
    assert failures[0]["itemIdentifier"] == "msg-slow-browser"
    assert slow_browser.cancelled is True

    async with session_factory() as session:
        fast_task = (
            await session.execute(select(TaskModel).where(TaskModel.id == tid_fast))
        ).scalar_one()
        assert fast_task.status == "COMPLETED"

        obs = (
            await session.execute(
                select(SourceObservationModel).where(
                    SourceObservationModel.execution_id == eid_fast,
                    SourceObservationModel.source_url == "https://example.com/fast-hotel",
                )
            )
        ).scalar_one_or_none()
        assert obs is not None
        assert obs.extraction_method == "BROWSER_HTML"


# =============================================================================
# 3. Deadline reached during execution skips subsequent records
# =============================================================================
@pytest.mark.asyncio
async def test_deadline_during_sequential_execution_skips_unstarted_record(
    session_factory: Any,
) -> None:
    """Verifies that when prior records consume usable time, subsequent records are skipped.

    Unstarted records are failed immediately with zero DB or adapter operations,
    and returned in batchItemFailures for safe SQS retry.
    """
    eid_1, tid_1 = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["query 1"], "budget": {"remaining_cost_minor": 100}},
    )
    eid_2, tid_2 = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["query 2"], "budget": {"remaining_cost_minor": 100}},
    )

    rec_1 = build_sqs_message(
        message_id="msg-seq-1",
        execution_id=eid_1,
        task_id=tid_1,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["query 1"], "budget": {"remaining_cost_minor": 100}},
    )
    rec_2 = build_sqs_message(
        message_id="msg-seq-2",
        execution_id=eid_2,
        task_id=tid_2,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["query 2"], "budget": {"remaining_cost_minor": 100}},
    )

    # 500ms total, 200ms reserve => 300ms usable time
    ctx = SimulatedLambdaContext(total_duration_ms=500)
    adapter_call_counts = {"query 1": 0, "query 2": 0}

    class DelayingAdapter:
        async def search(self, query: str, limit: int = 5, extra_params: Any = None) -> list[RawObservation]:
            if "query 1" in query:
                adapter_call_counts["query 1"] += 1
                # Takes 350ms, exhausting the 300ms usable time
                await asyncio.sleep(0.35)
                return [RawObservation.create(source_url="https://example.com/1", publisher="P1", excerpt_text="1")]
            adapter_call_counts["query 2"] += 1
            return [RawObservation.create(source_url="https://example.com/2", publisher="P2", excerpt_text="2")]

    event = {"Records": [rec_1, rec_2]}
    failures = await evidence_process_batch(
        event,
        context=ctx,
        session_factory=session_factory,
        adapter=DelayingAdapter(),
        extraction_adapter=FixtureWebExtractionAdapter(),
        reserve_ms=200,
        max_concurrency=1,
    )

    # Both records failed: rec-1 timed out during execution, rec-2 skipped before starting
    assert len(failures) == 2
    failed_ids = {f["itemIdentifier"] for f in failures}
    assert failed_ids == {"msg-seq-1", "msg-seq-2"}

    # Query 2 was never dispatched to adapter!
    assert adapter_call_counts["query 2"] == 0


# =============================================================================
# 4. Budget spend preservation on invocation deadline timeout
# =============================================================================
@pytest.mark.asyncio
async def test_evidence_budget_preserves_uncertain_spend_on_invocation_timeout(
    session_factory: Any,
) -> None:
    """Verifies that cancellation after provider dispatch preserves uncertain spend.

    Under executions.budget:
    - reserved_cost_minor is decremented.
    - spent_cost_minor and uncertain_cost_minor are incremented.
    - remaining_cost_minor is decremented.
    - reservation settled_as is 'CANCELLED'.
    Fails closed: provider spend is not refunded.
    """
    initial_budget = {
        "remaining_tool_calls": 3,
        "remaining_cost_minor": 300,
        "currency": "INR",
        "reserved_tool_calls": 0,
        "reserved_cost_minor": 0,
        "spent_tool_calls": 0,
        "spent_cost_minor": 0,
        "uncertain_tool_calls": 0,
        "uncertain_cost_minor": 0,
        "reservations": {},
    }
    eid, tid = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["budget cancel query"], "budget": {"remaining_cost_minor": 300}},
        initial_budget=initial_budget,
    )
    rec = build_sqs_message(
        message_id="msg-budget-cancel",
        execution_id=eid,
        task_id=tid,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["budget cancel query"], "budget": {"remaining_cost_minor": 300}},
    )

    # 400ms total, 150ms reserve => usable time 250ms
    ctx = SimulatedLambdaContext(total_duration_ms=400)
    slow_adapter = HangingSearchAdapter(hang_seconds=2.0)

    failures = await evidence_process_batch(
        {"Records": [rec]},
        context=ctx,
        session_factory=session_factory,
        adapter=slow_adapter,
        extraction_adapter=FixtureWebExtractionAdapter(),
        reserve_ms=150,
    )

    assert len(failures) == 1
    assert failures[0]["itemIdentifier"] == "msg-budget-cancel"
    assert slow_adapter.dispatched is True
    assert slow_adapter.cancelled is True

    # Inspect durable PostgreSQL executions.budget
    async with session_factory() as session:
        exec_row = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == eid))
        ).scalar_one()
        b = exec_row.budget

        # Fails closed: uncertain spend is recorded and remaining balance reduced
        assert b["reserved_cost_minor"] == 0
        assert b["spent_cost_minor"] == 50
        assert b["uncertain_cost_minor"] == 50
        assert b["remaining_cost_minor"] == 250

        reservations = b.get("reservations", {})
        assert len(reservations) == 1
        res_entry = list(reservations.values())[0]
        assert res_entry["status"] == "RECONCILED"
        assert res_entry["settled_as"] == "CANCELLED"


# =============================================================================
# 5. Duplicate delivery and idempotency under fencing
# =============================================================================
@pytest.mark.asyncio
async def test_duplicate_delivery_under_fencing_discarded_without_dlq_redrive(
    session_factory: Any,
) -> None:
    """Verifies that duplicate SQS deliveries for completed tasks are safely discarded.

    Fencing raises TaskAlreadyTerminalError, consumer catches it and returns success (omits from
    batchItemFailures), allowing SQS to delete the duplicate message.
    """
    eid, tid = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["query"], "budget": {"remaining_cost_minor": 100}},
    )

    # Manually advance task to COMPLETED terminal state
    async with session_factory() as session:
        t = (await session.execute(select(TaskModel).where(TaskModel.id == tid))).scalar_one()
        t.status = "COMPLETED"
        await session.commit()

    rec = build_sqs_message(
        message_id="msg-duplicate-terminal",
        execution_id=eid,
        task_id=tid,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["query"], "budget": {"remaining_cost_minor": 100}},
    )

    ctx = SimulatedLambdaContext(total_duration_ms=5000)
    failures = await evidence_process_batch(
        {"Records": [rec]},
        context=ctx,
        session_factory=session_factory,
        adapter=FixtureSearchAdapter(),
        reserve_ms=500,
    )

    # Must be empty! Duplicate is acknowledged so SQS drops it
    assert failures == []


# =============================================================================
# 6. Pre-execution deadline check
# =============================================================================
@pytest.mark.asyncio
async def test_pre_execution_insufficient_time_fails_all_records_immediately(
    session_factory: Any,
) -> None:
    """Verifies that if invocation remaining time is already below reserve margin,

    all records in the batch are failed immediately without opening sessions or starting tasks.
    """
    rec1 = {"messageId": "msg-pre-1", "body": {}}
    rec2 = {"messageId": "msg-pre-2", "body": {}}

    mock_context = MagicMock()
    # Context remaining time is 200ms, which is below 300ms reserve
    mock_context.get_remaining_time_in_millis.return_value = 200

    failures = await evidence_process_batch(
        {"Records": [rec1, rec2]},
        context=mock_context,
        session_factory=session_factory,
        reserve_ms=300,
    )

    assert len(failures) == 2
    assert failures == [{"itemIdentifier": "msg-pre-1"}, {"itemIdentifier": "msg-pre-2"}]
