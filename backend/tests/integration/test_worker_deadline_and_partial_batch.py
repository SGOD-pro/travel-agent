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
import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from functions.orchestrator.execution.models import (
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


# =============================================================================
# 7. Cancellation-resistant cleanup bounded before hard invocation deadline
# =============================================================================
@pytest.mark.asyncio
async def test_cancellation_resistant_cleanup_bounded_before_hard_invocation_deadline(
    session_factory: Any,
) -> None:
    """Verifies that when a record's cancellation cleanup blocks or sleeps,

    the consumer bounds the cleanup window and returns before the hard invocation deadline.
    Successful sibling records in the same batch are preserved and committed.
    """
    eid_fast, tid_fast = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["fast query"], "budget": {"remaining_cost_minor": 100}},
    )
    rec_fast = build_sqs_message(
        message_id="msg-fast-sibling",
        execution_id=eid_fast,
        task_id=tid_fast,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["fast query"], "budget": {"remaining_cost_minor": 100}},
    )

    eid_slow, tid_slow = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["slow resistant query"], "budget": {"remaining_cost_minor": 100}},
    )
    rec_slow = build_sqs_message(
        message_id="msg-resistant-cleanup",
        execution_id=eid_slow,
        task_id=tid_slow,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["slow resistant query"], "budget": {"remaining_cost_minor": 100}},
    )

    # Simulated Lambda invocation: 1000ms total, reserve 400ms => usable time is 600ms
    ctx = SimulatedLambdaContext(total_duration_ms=1000)

    class ResistantCleanupAdapter:
        def __init__(self) -> None:
            self.cleanup_entered = False
            self.cleanup_completed = False

        async def search(self, query: str, limit: int = 5, extra_params: Any = None) -> list[RawObservation]:
            if "slow" in query:
                try:
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    self.cleanup_entered = True
                    # Simulate cancellation-resistant cleanup that blocks for 5.0 seconds
                    await asyncio.sleep(5.0)
                    self.cleanup_completed = True
                    raise
            await asyncio.sleep(0.02)
            return [
                RawObservation.create(
                    source_url="https://example.com/fast",
                    publisher="Fast",
                    excerpt_text="Fast result",
                )
            ]

    resistant_adapter = ResistantCleanupAdapter()
    event = {"Records": [rec_fast, rec_slow]}

    t0 = time.monotonic()
    failures = await evidence_process_batch(
        event,
        context=ctx,
        session_factory=session_factory,
        adapter=resistant_adapter,
        extraction_adapter=FixtureWebExtractionAdapter(),
        reserve_ms=400,
    )
    elapsed = time.monotonic() - t0

    # Strict deadline verification: must return before the 1000ms hard invocation deadline!
    assert elapsed < 0.95, (
        f"Consumer took {elapsed:.2f}s, failing to return before simulated 1.0s Lambda deadline"
    )
    assert resistant_adapter.cleanup_entered is True, "Cancellation cleanup was entered"
    assert resistant_adapter.cleanup_completed is False, "Blocking cleanup was bounded and abandoned"

    # Fast record succeeded; slow resistant record returned in batchItemFailures
    assert len(failures) == 1
    assert failures[0]["itemIdentifier"] == "msg-resistant-cleanup"

    async with session_factory() as session:
        fast_task = (
            await session.execute(select(TaskModel).where(TaskModel.id == tid_fast))
        ).scalar_one()
        assert fast_task.status == "COMPLETED"


# =============================================================================
# 8. Suppressed cancellation swallowed by worker returns partial batch failure
# =============================================================================
@pytest.mark.asyncio
async def test_suppressed_cancellation_swallowed_returns_partial_batch_failure(
    session_factory: Any,
) -> None:
    """Verifies that if an operation catches and swallows CancelledError,

    the consumer still treats the expired usable deadline as a failure and reports the message
    in batchItemFailures so SQS redrives it rather than dropping it.
    """
    eid, tid = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["swallow cancel query"], "budget": {"remaining_cost_minor": 100}},
    )
    rec = build_sqs_message(
        message_id="msg-swallow-cancel",
        execution_id=eid,
        task_id=tid,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["swallow cancel query"], "budget": {"remaining_cost_minor": 100}},
    )

    ctx = SimulatedLambdaContext(total_duration_ms=600)

    class SwallowingAdapter:
        async def search(self, query: str, limit: int = 5, extra_params: Any = None) -> list[RawObservation]:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                # Maliciously or accidentally swallows CancelledError and returns normally
                return []
            return []

    failures = await evidence_process_batch(
        {"Records": [rec]},
        context=ctx,
        session_factory=session_factory,
        adapter=SwallowingAdapter(),
        extraction_adapter=FixtureWebExtractionAdapter(),
        reserve_ms=250,
    )

    # Must be returned in failures! Swallowed cancellation does NOT count as a success
    assert len(failures) == 1
    assert failures[0]["itemIdentifier"] == "msg-swallow-cancel"


# =============================================================================
# 9. Blocked budget reconciliation bounded and preserved for recovery sweep
# =============================================================================
@pytest.mark.asyncio
async def test_blocked_reconciliation_times_out_and_preserves_budget_as_uncertain(
    session_factory: Any,
) -> None:
    """Verifies that when database reconciliation blocks during cancellation cleanup:

    1. _safe_reconcile_budget times out cleanly without hanging the process past the deadline.
    2. The message is reported as a failure in batchItemFailures.
    3. The active reservation is left intact in executions.budget and safely recovered to
       ABANDONED_UNCERTAIN by TaskRecoveryService after lease expiry.
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
        payload={"queries": ["blocked recon query"], "budget": {"remaining_cost_minor": 300}},
        initial_budget=initial_budget,
    )
    rec = build_sqs_message(
        message_id="msg-blocked-recon",
        execution_id=eid,
        task_id=tid,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["blocked recon query"], "budget": {"remaining_cost_minor": 300}},
    )

    # Context: 800ms total, reserve 300ms => usable time is 500ms
    ctx = SimulatedLambdaContext(total_duration_ms=800)

    class HangingAdapterWithBlockedReconciliation:
        async def search(self, query: str, limit: int = 5, extra_params: Any = None) -> list[RawObservation]:
            await asyncio.sleep(5.0)
            return []

    # Monkeypatch reconcile_budget on EvidenceCollectorService to simulate database hang
    from functions.evidence_collector.service import EvidenceCollectorService
    original_reconcile = EvidenceCollectorService.reconcile_budget

    async def _blocked_reconcile(self: Any, *args: Any, **kwargs: Any) -> bool:
        # Simulate PostgreSQL hang or blocked row lock wait
        await asyncio.sleep(10.0)
        return False

    EvidenceCollectorService.reconcile_budget = _blocked_reconcile  # type: ignore[method-assign]

    try:
        t0 = time.monotonic()
        failures = await evidence_process_batch(
            {"Records": [rec]},
            context=ctx,
            session_factory=session_factory,
            adapter=HangingAdapterWithBlockedReconciliation(),
            extraction_adapter=FixtureWebExtractionAdapter(),
            reserve_ms=300,
        )
        elapsed = time.monotonic() - t0

        # Bounded execution: must return before simulated 800ms invocation deadline!
        assert elapsed < 0.78, f"Execution took {elapsed:.2f}s, did not respect deadline reserve"
        assert len(failures) == 1
        assert failures[0]["itemIdentifier"] == "msg-blocked-recon"
    finally:
        EvidenceCollectorService.reconcile_budget = original_reconcile  # type: ignore[method-assign]

    # Verify database state: reservation is preserved as ACTIVE because local reconciliation timed out
    async with session_factory() as session:
        exec_row = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == eid))
        ).scalar_one()
        b = exec_row.budget
        assert b["reserved_cost_minor"] == 50
        reservations = b.get("reservations", {})
        assert len(reservations) == 1
        res_entry = list(reservations.values())[0]
        assert res_entry["status"] == "ACTIVE"

        # Now simulate worker lease expiration and run background recovery sweep
        t_row = (await session.execute(select(TaskModel).where(TaskModel.id == tid))).scalar_one()
        t_row.lease_expiry = datetime.now(UTC) - timedelta(seconds=10)
        await session.commit()

    # Run recovery sweep on the abandoned reservation
    from functions.orchestrator.execution.recovery import TaskRecoveryService
    recovery_service = TaskRecoveryService(session_factory)
    recovered_items = await recovery_service.recover_abandoned_budget_reservations(eid, task_id=tid)

    assert len(recovered_items) == 1
    assert recovered_items[0]["status"] == "ABANDONED_UNCERTAIN"

    # Inspect execution and verify fail closed conservative cost accounting
    async with session_factory() as session:
        exec_row = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == eid))
        ).scalar_one()
        b_after = exec_row.budget
        assert b_after["reserved_cost_minor"] == 0
        assert b_after["spent_cost_minor"] == 50
        assert b_after["uncertain_cost_minor"] == 50
        assert b_after["remaining_cost_minor"] == 250


# =============================================================================
# 9. Lambda handler lifecycle and loop shutdown bounding (Subprocess isolation)
# =============================================================================
def get_test_db_url() -> str:
    import os

    from acceptance_database import validate_isolated_database_url
    from config.config import settings

    test_url = (
        os.environ.get("TEST_DATABASE_URL")
        or getattr(settings, "TEST_DATABASE_URL", None)
        or settings.ACCEPTANCE_DATABASE_URL
    )
    return validate_isolated_database_url(
        test_url,
        settings.DATABASE_URL,
        label="Integration test database",
    )


def run_lambda_handler_in_subprocess(
    *,
    worker_type: str,
    event: dict[str, Any],
    total_duration_ms: int,
    reserve_ms: int,
    adapter_code: str,
    db_url: str | None = None,
    parent_timeout_seconds: float = 6.0,
) -> tuple[dict[str, Any], float]:
    """Runs synchronous lambda_handler in an isolated child Python process under a parent-enforced timeout."""
    if db_url is None:
        target_db_url = get_test_db_url()
    else:
        target_db_url = db_url

    if target_db_url.startswith("postgres://"):
        target_db_url = "postgresql+asyncpg://" + target_db_url[len("postgres://") :]
    elif target_db_url.startswith("postgresql://") and not target_db_url.startswith(
        "postgresql+asyncpg://"
    ):
        target_db_url = "postgresql+asyncpg://" + target_db_url[len("postgresql://") :]

    if worker_type == "evidence":
        handler_import = "from functions.evidence_collector.consumer import lambda_handler"
        extra_kwargs = (
            "adapter=TestAdapter(), extraction_adapter=FixtureWebExtractionAdapter(),"
        )
    else:
        handler_import = "from functions.browser.consumer import lambda_handler"
        extra_kwargs = "adapter=TestAdapter(),"

    script = f"""
import asyncio
import json
import os
import sys
import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
{handler_import}
from functions.evidence_collector.fixtures import FixtureWebExtractionAdapter, RawObservation

{adapter_code}

class SubprocessContext:
    def __init__(self, duration_ms: int) -> None:
        self._start = time.monotonic()
        self._total = duration_ms

    def get_remaining_time_in_millis(self) -> int:
        elapsed = (time.monotonic() - self._start) * 1000.0
        return max(0, int(self._total - elapsed))

engine = create_async_engine('{target_db_url}', poolclass=NullPool)
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
ctx = SubprocessContext({total_duration_ms})
event = json.loads(sys.stdin.read())

t0 = time.monotonic()
res = lambda_handler(
    event,
    context=ctx,
    session_factory=session_factory,
    reserve_ms={reserve_ms},
    {extra_kwargs}
)
elapsed = time.monotonic() - t0
print(json.dumps({{"res": res, "elapsed": elapsed}}))
sys.stdout.flush()
sys.exit(0)
"""

    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=parent_timeout_seconds,
    )
    parent_elapsed = time.monotonic() - t0
    assert parent_elapsed <= parent_timeout_seconds
    assert proc.returncode == 0, (
        f"Child process failed ({proc.returncode}):\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    )
    lines = [line.strip() for line in proc.stdout.strip().splitlines() if line.strip()]
    assert lines, f"Child process produced no JSON output.\nSTDERR: {proc.stderr}"
    out = json.loads(lines[-1])
    return out["res"], out["elapsed"]


def run_two_successive_invocations_in_subprocess(
    *,
    worker_type: str,
    event1: dict[str, Any],
    event2: dict[str, Any],
    duration_ms1: int,
    duration_ms2: int,
    reserve_ms: int,
    adapter_code: str,
    db_url: str | None = None,
    parent_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Runs two successive lambda_handler invocations in the SAME child Python process."""
    if db_url is None:
        target_db_url = get_test_db_url()
    else:
        target_db_url = db_url

    if target_db_url.startswith("postgres://"):
        target_db_url = "postgresql+asyncpg://" + target_db_url[len("postgres://") :]
    elif target_db_url.startswith("postgresql://") and not target_db_url.startswith(
        "postgresql+asyncpg://"
    ):
        target_db_url = "postgresql+asyncpg://" + target_db_url[len("postgresql://") :]

    if worker_type == "evidence":
        handler_import = "from functions.evidence_collector.consumer import lambda_handler"
        extra_kwargs1 = "adapter=adapter1, extraction_adapter=FixtureWebExtractionAdapter(),"
        extra_kwargs2 = "adapter=adapter2, extraction_adapter=FixtureWebExtractionAdapter(),"
    else:
        handler_import = "from functions.browser.consumer import lambda_handler"
        extra_kwargs1 = "adapter=adapter1,"
        extra_kwargs2 = "adapter=adapter2,"

    script = f"""
import asyncio
import json
import os
import signal
import sys
import threading
import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
{handler_import}
from functions.evidence_collector.fixtures import FixtureWebExtractionAdapter, RawObservation
from contracts.browser import BrowserRenderResult, BrowserRenderStatus, BrowserResourceUsage
from functions.browser.driver import get_descendant_pids

{adapter_code}

class SubprocessContext:
    def __init__(self, duration_ms: int) -> None:
        self._start = time.monotonic()
        self._total = duration_ms

    def get_remaining_time_in_millis(self) -> int:
        elapsed = (time.monotonic() - self._start) * 1000.0
        return max(0, int(self._total - elapsed))

engine = create_async_engine('{target_db_url}', poolclass=NullPool)
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

ctx1 = SubprocessContext({duration_ms1})
event1 = {json.dumps(event1)}
event2 = {json.dumps(event2)}

# --- INVOCATION 1 ---
adapter1 = TestAdapter(1)
t0 = time.monotonic()
res1 = lambda_handler(
    event1,
    context=ctx1,
    session_factory=session_factory,
    reserve_ms={reserve_ms},
    {extra_kwargs1}
)
elapsed1 = time.monotonic() - t0
threads_after1 = threading.active_count()

post_invoc1_check = {{}}
if hasattr(adapter1, "check_post_invocation"):
    post_invoc1_check = adapter1.check_post_invocation()

# --- INVOCATION 2 in same process ---
ctx2 = SubprocessContext({duration_ms2})
adapter2 = TestAdapter(2)
t1 = time.monotonic()
res2 = lambda_handler(
    event2,
    context=ctx2,
    session_factory=session_factory,
    reserve_ms={reserve_ms},
    {extra_kwargs2}
)
elapsed2 = time.monotonic() - t1
threads_after2 = threading.active_count()

post_invoc2_check = {{}}
if hasattr(adapter2, "check_post_invocation"):
    post_invoc2_check = adapter2.check_post_invocation()

out = {{
    "res1": res1,
    "elapsed1": elapsed1,
    "post_invoc1": post_invoc1_check,
    "threads_after1": threads_after1,
    "res2": res2,
    "elapsed2": elapsed2,
    "post_invoc2": post_invoc2_check,
    "threads_after2": threads_after2,
}}
print(json.dumps(out))
sys.stdout.flush()
sys.exit(0)
"""

    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=parent_timeout_seconds,
    )
    parent_elapsed = time.monotonic() - t0
    assert parent_elapsed <= parent_timeout_seconds, f"Subprocess timed out after {parent_elapsed:.2f}s"
    assert proc.returncode == 0, (
        f"Child process failed ({proc.returncode}):\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    )
    lines = [line.strip() for line in proc.stdout.strip().splitlines() if line.strip()]
    assert lines, f"Child process produced no JSON output.\nSTDERR: {proc.stderr}"
    res = json.loads(lines[-1])
    assert isinstance(res, dict)
    return res


def run_repeated_timed_out_invocations_in_subprocess(
    *,
    worker_type: str,
    events: list[dict[str, Any]],
    duration_ms: int,
    reserve_ms: int,
    adapter_code: str,
    db_url: str | None = None,
    parent_timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """Runs multiple repeated timed-out invocations in the SAME child Python process."""
    if db_url is None:
        target_db_url = get_test_db_url()
    else:
        target_db_url = db_url

    if target_db_url.startswith("postgres://"):
        target_db_url = "postgresql+asyncpg://" + target_db_url[len("postgres://") :]
    elif target_db_url.startswith("postgresql://") and not target_db_url.startswith(
        "postgresql+asyncpg://"
    ):
        target_db_url = "postgresql+asyncpg://" + target_db_url[len("postgresql://") :]

    if worker_type == "evidence":
        handler_import = (
            "from functions.evidence_collector.consumer import lambda_handler\n"
            "from functions.evidence_collector.fixtures import FixtureWebExtractionAdapter"
        )
        extra_kwargs = "adapter=adapter, extraction_adapter=FixtureWebExtractionAdapter(),"
    else:
        handler_import = "from functions.browser.consumer import lambda_handler"
        extra_kwargs = "adapter=adapter,"

    script = f"""
import asyncio
import json
import sys
import threading
import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
{handler_import}

{adapter_code}

class SubprocessContext:
    def __init__(self, duration_ms: int) -> None:
        self._start = time.monotonic()
        self._total = duration_ms

    def get_remaining_time_in_millis(self) -> int:
        elapsed = (time.monotonic() - self._start) * 1000.0
        return max(0, int(self._total - elapsed))

engine = create_async_engine('{target_db_url}', poolclass=NullPool)
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

events = {json.dumps(events)}
results = []
thread_counts = []
initial_threads = threading.active_count()

for idx, ev in enumerate(events):
    ctx = SubprocessContext({duration_ms})
    adapter = TestAdapter(idx + 1)
    res = lambda_handler(
        ev,
        context=ctx,
        session_factory=session_factory,
        reserve_ms={reserve_ms},
        {extra_kwargs}
    )
    results.append(res)
    thread_counts.append(threading.active_count())

post_check = post_loop_check() if "post_loop_check" in globals() else {{}}

out = {{
    "initial_threads": initial_threads,
    "results": results,
    "thread_counts": thread_counts,
    "post_check": post_check,
}}
print(json.dumps(out))
sys.stdout.flush()
sys.exit(0)
"""

    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=parent_timeout_seconds,
    )
    parent_elapsed = time.monotonic() - t0
    assert parent_elapsed <= parent_timeout_seconds, f"Subprocess timed out after {parent_elapsed:.2f}s"
    assert proc.returncode == 0, (
        f"Child process failed ({proc.returncode}):\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    )
    lines = [line.strip() for line in proc.stdout.strip().splitlines() if line.strip()]
    assert lines, f"Child process produced no JSON output.\nSTDERR: {proc.stderr}"
    res = json.loads(lines[-1])
    assert isinstance(res, dict)
    return res


@pytest.mark.asyncio
async def test_lambda_handler_cooperative_cancellation_returns_before_deadline(
    session_factory: Any,
) -> None:
    """Verifies that lambda_handler handles cooperative task cancellation cleanly before deadline."""
    eid, tid = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["cooperative slow query"]},
    )
    msg = build_sqs_message(
        message_id="msg-cooperative-handler",
        execution_id=eid,
        task_id=tid,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["cooperative slow query"]},
    )
    event = {"Records": [msg]}

    adapter_code = """
class TestAdapter:
    async def search(self, query: str, limit: int = 5, extra_params=None):
        await asyncio.sleep(10.0)
        return []
"""
    # 2500ms total, 1500ms reserve => usable time is 1000ms (1.0s)
    res, elapsed = run_lambda_handler_in_subprocess(
        worker_type="evidence",
        event=event,
        total_duration_ms=2500,
        reserve_ms=1500,
        adapter_code=adapter_code,
        parent_timeout_seconds=5.0,
    )
    assert elapsed < 2.3, f"Execution took {elapsed:.2f}s, did not respect deadline reserve"
    assert res == {"batchItemFailures": [{"itemIdentifier": "msg-cooperative-handler"}]}


@pytest.mark.asyncio
async def test_lambda_handler_repeated_cancellation_suppression_bounded_and_returns(
    session_factory: Any,
) -> None:
    """Verifies that lambda_handler bounds shutdown and returns partial batch when a record suppresses cancellation."""
    eid, tid = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["stubborn cancellation resistant query"]},
    )
    msg = build_sqs_message(
        message_id="msg-stubborn-handler",
        execution_id=eid,
        task_id=tid,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["stubborn cancellation resistant query"]},
    )
    event = {"Records": [msg]}

    adapter_code = """
class TestAdapter:
    async def search(self, query: str, limit: int = 5, extra_params=None):
        while True:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                pass
"""
    # 2500ms total, 1500ms reserve => usable time is 1000ms
    res, elapsed = run_lambda_handler_in_subprocess(
        worker_type="evidence",
        event=event,
        total_duration_ms=2500,
        reserve_ms=1500,
        adapter_code=adapter_code,
        parent_timeout_seconds=5.0,
    )
    assert elapsed < 2.48, f"Execution took {elapsed:.2f}s, exceeded invocation deadline"
    assert res == {"batchItemFailures": [{"itemIdentifier": "msg-stubborn-handler"}]}

    # Verify reservation remained in DB as ACTIVE (not refunded)
    async with session_factory() as session:
        exec_row = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == eid))
        ).scalar_one()
        reservations = exec_row.budget.get("reservations", {})
        assert len(reservations) == 1
        res_entry = list(reservations.values())[0]
        assert res_entry["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_lambda_handler_blocked_cleanup_abandoned_and_returns_partial_batch(
    session_factory: Any,
) -> None:
    """Verifies that when task cleanup blocks, lambda_handler abandons it and returns batchItemFailures before timeout."""
    eid, tid = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["blocked cleanup query"]},
    )
    msg = build_sqs_message(
        message_id="msg-blocked-cleanup-handler",
        execution_id=eid,
        task_id=tid,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["blocked cleanup query"]},
    )
    event = {"Records": [msg]}

    adapter_code = """
class TestAdapter:
    async def search(self, query: str, limit: int = 5, extra_params=None):
        try:
            await asyncio.sleep(5.0)
        finally:
            while True:
                try:
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    pass
"""
    res, elapsed = run_lambda_handler_in_subprocess(
        worker_type="evidence",
        event=event,
        total_duration_ms=2500,
        reserve_ms=1500,
        adapter_code=adapter_code,
        parent_timeout_seconds=5.0,
    )
    assert elapsed < 2.48, f"Execution took {elapsed:.2f}s, exceeded invocation deadline"
    assert res == {"batchItemFailures": [{"itemIdentifier": "msg-blocked-cleanup-handler"}]}


@pytest.mark.asyncio
async def test_lambda_handler_successful_siblings_preserved_with_stubborn_record(
    session_factory: Any,
) -> None:
    """Verifies that successful sibling records are committed and omitted from batchItemFailures while stubborn sibling is failed."""
    eid_fast, tid_fast = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["fast sibling query"]},
    )
    eid_slow, tid_slow = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["stubborn sibling query"]},
    )
    msg_fast = build_sqs_message(
        message_id="msg-fast-sibling",
        execution_id=eid_fast,
        task_id=tid_fast,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["fast sibling query"]},
    )
    msg_slow = build_sqs_message(
        message_id="msg-stubborn-sibling",
        execution_id=eid_slow,
        task_id=tid_slow,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["stubborn sibling query"]},
    )
    event = {"Records": [msg_fast, msg_slow]}

    adapter_code = """
class TestAdapter:
    async def search(self, query: str, limit: int = 5, extra_params=None):
        if "fast" in query:
            await asyncio.sleep(0.01)
            return [RawObservation.create(source_url="https://example.com/ok", publisher="Gov", excerpt_text="Fast success")]
        while True:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                pass
"""
    res, elapsed = run_lambda_handler_in_subprocess(
        worker_type="evidence",
        event=event,
        total_duration_ms=2500,
        reserve_ms=1500,
        adapter_code=adapter_code,
        parent_timeout_seconds=5.0,
    )
    assert elapsed < 2.48, f"Execution took {elapsed:.2f}s, exceeded invocation deadline"
    assert res == {"batchItemFailures": [{"itemIdentifier": "msg-stubborn-sibling"}]}

    # Verify fast task is completed in DB
    async with session_factory() as session:
        t_fast_row = (
            await session.execute(select(TaskModel).where(TaskModel.id == tid_fast))
        ).scalar_one()
        assert t_fast_row.status == "COMPLETED"


@pytest.mark.asyncio
async def test_lambda_handler_two_successive_invocations_same_process_evidence_residual_thread_isolation(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    """Verifies that two successive evidence collector invocations in the same process remain isolated.

    Invocation 1 starts finite blocking work in an owned terminable child process that sleeps past
    Invocation 1 full duration (2.7s > 2.5s) before attempting genuine cross-process shared state mutations
    (a disk marker file and multiprocessing.Value). Invocation 1 times out, calls
    adapter.terminate_residual_processes() in finally, and returns its partial batch response.
    Invocation 2 runs in the same process across the window where the child would have mutated state,
    completes its task successfully, and verifies that the child PID is dead and shared state is untouched.
    """
    marker_file = tmp_path / "residual_mutation.marker"

    eid1, tid1 = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["slow stubborn query"]},
    )
    eid2, tid2 = await create_db_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["fast sibling query"]},
    )
    msg1 = build_sqs_message(
        message_id="msg-slow-invoc1",
        execution_id=eid1,
        task_id=tid1,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["slow stubborn query"]},
    )
    msg2 = build_sqs_message(
        message_id="msg-fast-invoc2",
        execution_id=eid2,
        task_id=tid2,
        worker="EVIDENCE",
        action="collect",
        payload={"queries": ["fast sibling query"]},
    )

    adapter_code = f"""
import multiprocessing as mp
from functions.browser.driver import is_pid_alive

MARKER_PATH = {json.dumps(str(marker_file))}
_MP_CTX = mp.get_context("fork")
SHARED_VAL = _MP_CTX.Value("i", 0)
INVOC1_STARTED = False

class TerminableProcessWork:
    def __init__(self) -> None:
        self.proc = None
        self.pid = None

    def start(self) -> None:
        started_event = _MP_CTX.Event()

        def _target():
            started_event.set()
            # Sleep past Invocation 1 full duration (2.5s) so mutation fires during Invocation 2 if not killed
            time.sleep(2.7)
            SHARED_VAL.value = 1
            with open(MARKER_PATH, "w", encoding="utf-8") as fh:
                fh.write("MUTATED")

        self.proc = _MP_CTX.Process(target=_target)
        self.proc.start()
        self.pid = self.proc.pid
        started_event.wait(timeout=2.0)

    def terminate(self) -> bool:
        was_alive = bool(self.proc and self.proc.is_alive())
        if was_alive and self.proc:
            self.proc.kill()
            self.proc.join(timeout=2.0)
        return was_alive

class TestAdapter:
    def __init__(self, invoc_num: int) -> None:
        self.invoc_num = invoc_num
        self.work = None
        self.terminate_called = False
        self.was_alive_at_terminate = False

    async def search(self, query: str, limit: int = 5, extra_params=None):
        global INVOC1_STARTED
        if self.invoc_num == 1:
            INVOC1_STARTED = True
            self.work = TerminableProcessWork()
            self.work.start()
            while True:
                try:
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    pass
        else:
            # Wait past the 2.7s mark from child start so an unkilled child would have written shared state
            await asyncio.sleep(0.5)
            return [RawObservation.create(source_url="https://example.com/invoc2", publisher="Gov", excerpt_text="Invoc2 success")]

    def terminate_residual_processes(self) -> None:
        self.terminate_called = True
        if self.work:
            self.was_alive_at_terminate = self.work.terminate()

    def check_post_invocation(self):
        child_pid = self.work.pid if self.work else None
        child_alive = is_pid_alive(child_pid) if child_pid is not None else False
        return {{
            "invoc1_started": INVOC1_STARTED,
            "terminate_called": self.terminate_called,
            "was_alive_at_terminate": self.was_alive_at_terminate,
            "child_pid": child_pid,
            "child_alive_after_invoc1": child_alive,
            "shared_val": SHARED_VAL.value,
            "marker_exists": os.path.exists(MARKER_PATH),
        }}
"""

    out = run_two_successive_invocations_in_subprocess(
        worker_type="evidence",
        event1={"Records": [msg1]},
        event2={"Records": [msg2]},
        duration_ms1=2500,
        duration_ms2=2500,
        reserve_ms=1500,
        adapter_code=adapter_code,
        parent_timeout_seconds=10.0,
    )

    # Invocation 1: timed out, returned partial failure, and handler killed the live child in finally
    assert out["elapsed1"] < 2.48, f"Invocation 1 took {out['elapsed1']:.2f}s"
    assert out["res1"] == {"batchItemFailures": [{"itemIdentifier": "msg-slow-invoc1"}]}
    assert out["post_invoc1"]["invoc1_started"] is True
    assert out["post_invoc1"]["terminate_called"] is True, "lambda_handler did not invoke terminate_residual_processes()"
    assert out["post_invoc1"]["was_alive_at_terminate"] is True, "Child already exited before terminate_residual_processes() ran!"
    assert out["post_invoc1"]["child_pid"] is not None
    assert out["post_invoc1"]["child_alive_after_invoc1"] is False, "Child process survived Invocation 1 exit!"
    assert out["post_invoc1"]["shared_val"] == 0

    # Invocation 2: ran in same process across the mutation window and succeeded completely
    assert out["res2"] == {"batchItemFailures": []}
    assert out["elapsed2"] < 1.5, f"Invocation 2 took {out['elapsed2']:.2f}s"

    # Assert required isolation directly: genuine cross-process shared state remained untouched
    assert out["post_invoc2"]["shared_val"] == 0, "Cross-process shared memory was mutated by residual work!"
    assert out["post_invoc2"]["marker_exists"] is False, "Shared marker file was created by residual work!"
    assert not marker_file.exists(), "Shared marker file exists on disk!"

    # Database verification
    async with session_factory() as session:
        t1_row = (await session.execute(select(TaskModel).where(TaskModel.id == tid1))).scalar_one()
        assert t1_row.status != "COMPLETED"

        t2_row = (await session.execute(select(TaskModel).where(TaskModel.id == tid2))).scalar_one()
        assert t2_row.status == "COMPLETED"


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_type", ["evidence", "browser"])
async def test_lambda_handler_repeated_timed_out_invocations_bounded_thread_measurement(
    session_factory: Any,
    worker_type: str,
) -> None:
    """Verifies bounded thread counts and queued future cancellation across repeated timed-out invocations.

    Parameterized across both evidence and browser consumers:
    - ThreadPoolExecutor threads plateau at initial_threads + max_workers (2) without unbounded linear growth.
    - An unstarted 3rd queued blocking callback is cancelled by the handler drain loop and never executes
      even after earlier worker threads finish and return to the work queue.
    - The process exits cleanly via normal sys.exit(0) without os._exit(0).
    """
    events = []
    for i in range(3):
        if worker_type == "evidence":
            eid, tid = await create_db_task(
                session_factory,
                worker="EVIDENCE",
                action="collect",
                payload={"queries": [f"query {i}"]},
            )
            msg = build_sqs_message(
                message_id=f"msg-timeout-{i}",
                execution_id=eid,
                task_id=tid,
                worker="EVIDENCE",
                action="collect",
                payload={"queries": [f"query {i}"]},
            )
        else:
            eid, tid = await create_db_task(
                session_factory,
                worker="BROWSER",
                action="render",
                payload={
                    "url": f"http://example.com/slow-{i}",
                    "canonical_url": f"http://example.com/slow-{i}",
                    "category": "lodging",
                    "reason": "TEST_REASON",
                },
            )
            msg = build_sqs_message(
                message_id=f"msg-timeout-{i}",
                execution_id=eid,
                task_id=tid,
                worker="BROWSER",
                action="render",
                payload={
                    "url": f"http://example.com/slow-{i}",
                    "canonical_url": f"http://example.com/slow-{i}",
                    "category": "lodging",
                    "reason": "TEST_REASON",
                },
            )
        events.append({"Records": [msg]})

    if worker_type == "evidence":
        adapter_code = """
QUEUED_CALLBACK_EXECUTED = False

class TestAdapter:
    def __init__(self, invoc_num: int) -> None:
        self.invoc_num = invoc_num

    async def search(self, query: str, limit: int = 5, extra_params=None):
        if self.invoc_num in (1, 2):
            def blocking_sleep():
                time.sleep(3.0)
            await asyncio.to_thread(blocking_sleep)
        else:
            def queued_callback():
                global QUEUED_CALLBACK_EXECUTED
                QUEUED_CALLBACK_EXECUTED = True
            await asyncio.to_thread(queued_callback)
        return []

def post_loop_check():
    # Wait past Thread 1 completion (3.0s from start) so Thread 1 re-enters the queue loop
    time.sleep(1.1)
    return {"queued_callback_executed": QUEUED_CALLBACK_EXECUTED}
"""
    else:
        adapter_code = """
QUEUED_CALLBACK_EXECUTED = False

class TestAdapter:
    def __init__(self, invoc_num: int) -> None:
        self.invoc_num = invoc_num

    async def render_page(self, payload):
        if self.invoc_num in (1, 2):
            def blocking_sleep():
                time.sleep(3.0)
            await asyncio.to_thread(blocking_sleep)
        else:
            def queued_callback():
                global QUEUED_CALLBACK_EXECUTED
                QUEUED_CALLBACK_EXECUTED = True
            await asyncio.to_thread(queued_callback)
        return None

def post_loop_check():
    # Wait past Thread 1 completion (3.0s from start) so Thread 1 re-enters the queue loop
    time.sleep(1.1)
    return {"queued_callback_executed": QUEUED_CALLBACK_EXECUTED}
"""

    out = run_repeated_timed_out_invocations_in_subprocess(
        worker_type=worker_type,
        events=events,
        duration_ms=2200,
        reserve_ms=1500,
        adapter_code=adapter_code,
        parent_timeout_seconds=12.0,
    )

    # All 3 invocations returned partial batch failure for their timed-out record
    for i, res in enumerate(out["results"]):
        assert res == {"batchItemFailures": [{"itemIdentifier": f"msg-timeout-{i}"}]}

    # Thread measurement: verify active threads do NOT grow unbounded across invocations
    thread_counts = out["thread_counts"]
    assert len(thread_counts) == 3
    # Initial thread is MainThread (1). Bounded container executor has max_workers=2.
    # Thread counts must stay bounded by (initial_threads + max_workers) and not accumulate indefinitely.
    for count in thread_counts:
        assert count <= out["initial_threads"] + 2, f"Thread count {count} exceeded bound ({thread_counts})"

    # Growth between consecutive invocations must plateau (no unbounded linear growth)
    growth_after_warmup = thread_counts[-1] - thread_counts[1]
    assert growth_after_warmup == 0, f"Thread count continued growing after warmup: {thread_counts}"

    # Unstarted queued future in _work_queue was cancelled during drain and never ran
    assert out["post_check"]["queued_callback_executed"] is False, (
        "Queued 3rd callback executed despite container executor work queue draining!"
    )


@pytest.mark.asyncio
async def test_lambda_handler_two_successive_invocations_same_process_browser_residual_process_cleanup(
    session_factory: Any,
) -> None:
    """Verifies that two successive browser worker invocations in the same process clean up child processes.

    Invocation 1 launches Playwright/Chromium and blocks past usable time; the handler shuts down,
    force-kills all descendant Playwright and Chromium processes, and returns partial batch failure.
    Invocation 2 executes a fast browser task in the same process, completes successfully, and verifies zero residual processes.
    """
    eid1, tid1 = await create_db_task(
        session_factory,
        worker="BROWSER",
        action="render",
        payload={
            "url": "http://127.0.0.1:9/stubborn",
            "canonical_url": "http://127.0.0.1:9/stubborn",
            "category": "lodging",
            "reason": "TEST_REASON",
        },
    )
    eid2, tid2 = await create_db_task(
        session_factory,
        worker="BROWSER",
        action="render",
        payload={
            "url": "http://example.com/fast",
            "canonical_url": "http://example.com/fast",
            "category": "lodging",
            "reason": "TEST_REASON",
        },
    )
    msg1 = build_sqs_message(
        message_id="msg-browser-stubborn-1",
        execution_id=eid1,
        task_id=tid1,
        worker="BROWSER",
        action="render",
        payload={
            "url": "http://127.0.0.1:9/stubborn",
            "canonical_url": "http://127.0.0.1:9/stubborn",
            "category": "lodging",
            "reason": "TEST_REASON",
        },
    )
    msg2 = build_sqs_message(
        message_id="msg-browser-fast-2",
        execution_id=eid2,
        task_id=tid2,
        worker="BROWSER",
        action="render",
        payload={
            "url": "http://example.com/fast",
            "canonical_url": "http://example.com/fast",
            "category": "lodging",
            "reason": "TEST_REASON",
        },
    )

    adapter_code = """
from functions.browser.driver import PlaywrightBrowserAdapter, get_descendant_pids, is_pid_alive
from contracts.browser import BrowserRenderResult, BrowserRenderStatus, BrowserResourceUsage

class TestAdapter(PlaywrightBrowserAdapter):
    def __init__(self, invoc_num: int) -> None:
        super().__init__()
        self.invoc_num = invoc_num
        self.tracked_driver_pid = None

    async def render_page(self, payload):
        if self.invoc_num == 1:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            impl = getattr(pw, "_impl_obj", None)
            conn = getattr(impl, "_connection", None)
            transport = getattr(conn, "_transport", None)
            proc = getattr(transport, "_proc", None)
            if proc and hasattr(proc, "pid"):
                self.tracked_driver_pid = proc.pid
                self._active_driver_pids.add(proc.pid)
            browser = await pw.chromium.launch(headless=True)
            while True:
                try:
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    pass
        else:
            return BrowserRenderResult(
                status=BrowserRenderStatus.SUCCESS,
                canonical_url=payload.canonical_url,
                effective_url=payload.url,
                content_hash="mock-browser-hash",
                excerpt_text="Fast browser excerpt",
                excerpt_locator="body",
                rendered_html_snippet="<html><body>Fast browser content</body></html>",
                render_duration_ms=10,
                unavailable_reason=None,
                usage=BrowserResourceUsage(duration_ms=10, memory_mb=128),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )

    def check_post_invocation(self):
        alive_driver = False
        alive_descendants = []
        if self.tracked_driver_pid is not None:
            alive_driver = is_pid_alive(self.tracked_driver_pid)
            descendants = get_descendant_pids(self.tracked_driver_pid)
            alive_descendants = [d for d in descendants if is_pid_alive(d)]
        return {
            "driver_pid": self.tracked_driver_pid,
            "alive_driver": alive_driver,
            "alive_descendants": alive_descendants,
        }
"""

    out = run_two_successive_invocations_in_subprocess(
        worker_type="browser",
        event1={"Records": [msg1]},
        event2={"Records": [msg2]},
        duration_ms1=3500,
        duration_ms2=2500,
        reserve_ms=1500,
        adapter_code=adapter_code,
        parent_timeout_seconds=10.0,
    )

    # Invocation 1: timed out, returned partial failure strictly under 3.50s deadline
    assert out["elapsed1"] < 3.48, f"Invocation 1 took {out['elapsed1']:.2f}s"
    assert out["res1"] == {"batchItemFailures": [{"itemIdentifier": "msg-browser-stubborn-1"}]}

    # Verification: ALL child processes from Invocation 1 were force killed
    post1 = out["post_invoc1"]
    assert post1["driver_pid"] is not None
    assert post1["alive_driver"] is False, f"Driver PID {post1['driver_pid']} is still alive!"
    assert post1["alive_descendants"] == [], f"Chromium descendant PIDs still alive: {post1['alive_descendants']}"

    # Invocation 2: succeeded cleanly in the same process
    assert out["res2"] == {"batchItemFailures": []}
    assert out["elapsed2"] < 1.0, f"Invocation 2 took {out['elapsed2']:.2f}s"

    # Database verification
    async with session_factory() as session:
        t1_row = (await session.execute(select(TaskModel).where(TaskModel.id == tid1))).scalar_one()
        assert t1_row.status != "COMPLETED"

        t2_row = (await session.execute(select(TaskModel).where(TaskModel.id == tid2))).scalar_one()
        assert t2_row.status == "COMPLETED"

