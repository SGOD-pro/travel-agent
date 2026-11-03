"""Integration tests for LangGraph result application and checkpoint reconciliation.

Conforming to spec 0001:
1. Validates incoming task events against persisted task/results, execution status, attempt, and context version.
2. Deduplicates receipts; resumes pending/unapplied receipts rather than discarding duplicates.
3. Serializes and fences checkpoint writes per execution with parent-first row locks.
4. Merges outputs and accepted-result markers without overwriting previous results.
5. Injected crashes at each persistence boundary:
   - Crash before checkpoint update (unapplied receipt resumed).
   - Crash after checkpoint update before receipt confirmation (detects marker, bypasses duplicate update).
6. Persists durable graph-resume action (EXECUTION_RESUME outbox event); updating checkpoint state alone is not graph resume.
7. Handles failed/cancelled task outcomes so joins cannot wait forever.
8. Preserves cancellation and deadline barriers.
9. Bounded reconciliation batches and invocation time.
"""

from __future__ import annotations

import asyncio
import re
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.config import settings
from functions.api.models import TripModel
from functions.orchestrator.execution import (
    CompletionReceiptModel,
    CompletionReconciliationService,
    ExecutionModel,
    ExecutionState,
    OutboxEventModel,
    TaskCompletionEvent,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer

# Resilient DNS lookup for WSL/Aiven forwarder stability
_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
    last_err: Exception | None = None
    import time
    for _ in range(5):
        try:
            res = _orig_getaddrinfo(host, port, *args, **kwargs)
            _dns_cache[key] = res
            return res
        except socket.gaierror as e:
            last_err = e
            time.sleep(0.3)
    if key in _dns_cache:
        return _dns_cache[key]
    if last_err:
        raise last_err
    raise socket.gaierror(-5, "No address associated with hostname")


socket.getaddrinfo = _resilient_getaddrinfo


@pytest.fixture
async def session_factory():
    url = settings.DATABASE_URL
    connect_args = {}
    if (
        "sslmode=require" in url
        or "ssl=require" in url
        or "aivencloud.com" in url
        or settings.DB_SSL
    ):
        connect_args["ssl"] = "require"
    if "sslmode=" in url:
        url = re.sub(r"[?&]sslmode=[^&]+", "", url)
        if "?" not in url and "&" in url:
            url = url.replace("&", "?", 1)
    test_engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await test_engine.dispose()


@pytest.fixture
async def postgres_checkpointer(session_factory):
    async with session_factory() as session:
        await session.execute(text("DROP TABLE IF EXISTS checkpoint_writes;"))
        await session.execute(text("DROP TABLE IF EXISTS checkpoints;"))
        await session.execute(text("""
            CREATE TABLE checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BYTEA NOT NULL,
                metadata BYTEA NOT NULL,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            );
        """))
        await session.execute(text("""
            CREATE TABLE checkpoint_writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BYTEA NOT NULL,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            );
        """))
        await session.commit()
    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    await checkpointer.setup()
    yield checkpointer


def build_test_execution_graph(checkpointer: Any):
    """Builds a test state graph with worker and join nodes using checkpointer."""
    def worker_node(state: ExecutionState) -> dict[str, Any]:
        return {"current_step": "worker_processed"}

    def join_node(state: ExecutionState) -> dict[str, Any]:
        return {"current_step": "join_evaluated", "status": "completed"}

    builder = StateGraph(ExecutionState)
    builder.add_node("worker_node", worker_node)
    builder.add_node("join_node", join_node)
    builder.add_edge(START, "worker_node")
    builder.add_edge("worker_node", "join_node")
    builder.add_edge("join_node", END)
    return builder.compile(checkpointer=checkpointer)


async def create_execution_with_task_and_result(
    session_factory,
    *,
    execution_status: str = "WAITING_TASKS",
    task_status: str = "COMPLETED",
    context_version: int = 1,
    attempt: int = 1,
    deadline_minutes: int = 60,
    cancelled_at: datetime | None = None,
    output: dict[str, Any] | None = None,
    execution_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Creates execution, task, and worker_result records in PostgreSQL."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    exec_id = execution_id or uuid.uuid4()
    task_id = uuid.uuid4()
    result_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as session:
        # Check if execution already exists
        existing_exec = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one_or_none()

        if existing_exec is None:
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
                id=exec_id,
                owner_id=owner_id,
                trip_id=trip_id,
                workflow="TRAVEL",
                status=execution_status,
                context_version=context_version,
                deadline_at=now + timedelta(minutes=deadline_minutes),
                cancelled_at=cancelled_at,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)

        task = TaskModel(
            id=task_id,
            execution_id=exec_id,
            parent_task_id=None,
            worker="EVIDENCE",
            action="collect",
            payload_ref={"location": "Jaipur"},
            input_hash="hash_123",
            idempotency_key=f"task-idem-{task_id}",
            attempt=attempt,
            max_attempts=3,
            context_version=context_version,
            status=task_status,
            lease_token=None,
            lease_expiry=None,
            deadline_at=now + timedelta(minutes=deadline_minutes),
            result_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.flush()

        if task_status == "COMPLETED":
            worker_result = WorkerResultModel(
                id=result_id,
                task_id=task_id,
                execution_id=exec_id,
                attempt=attempt,
                context_version=context_version,
                status="SUCCESS",
                output_ref=output or {"hotels": ["H1", "H2"]},
                warnings=[],
                error=None,
                usage={},
                started_at=now,
                finished_at=now,
            )
            session.add(worker_result)
            await session.flush()
            task.result_id = result_id

        await session.commit()

    return exec_id, task_id, result_id


@pytest.mark.asyncio
async def test_successful_result_reconciliation_and_marker_recorded(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies successful reconciliation: receipt stored, checkpoint updated, and durable resume action emitted."""
    graph = build_test_execution_graph(postgres_checkpointer)
    exec_id, task_id, result_id = await create_execution_with_task_and_result(
        session_factory,
        output={"hotels": ["Palace Hotel"]},
    )

    # Initialize graph thread checkpoint
    config = {"configurable": {"thread_id": str(exec_id)}}
    await graph.aupdate_state(
        config=config,
        values={"accepted_result_ids": [], "task_outputs": {}},
        as_node="worker_node",
    )

    event_id = uuid.uuid4()
    event = TaskCompletionEvent(
        event_id=event_id,
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=result_id,
    )

    service = CompletionReconciliationService(session_factory, default_graph=graph)
    result = await service.reconcile_task_event(event=event)

    assert result.status == "APPLIED"
    assert result.checkpoint_updated is True
    assert result.graph_resumed is True
    assert result.resume_event_id is not None

    # Check PostgreSQL database state
    async with session_factory() as session:
        # Receipt marked applied
        receipt = (
            await session.execute(
                select(CompletionReceiptModel).where(CompletionReceiptModel.event_id == event_id)
            )
        ).scalar_one()
        assert receipt.applied_at is not None

        # Execution transitioned to RUNNING
        execution = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        assert execution.status == "RUNNING"

        # Durable EXECUTION_RESUME outbox event emitted
        outbox = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
            )
        ).scalar_one()
        assert outbox.id == result.resume_event_id
        assert outbox.payload_ref["action"] == "resume_graph"

    # Check LangGraph thread checkpoint state
    state = await graph.aget_state(config=config)
    assert str(result_id) in state.values["accepted_result_ids"]
    assert state.values["task_outputs"][str(task_id)] == {"hotels": ["Palace Hotel"]}


@pytest.mark.asyncio
async def test_receipt_deduplication_already_applied(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that an already applied receipt is deduplicated with zero secondary mutations."""
    graph = build_test_execution_graph(postgres_checkpointer)
    exec_id, task_id, result_id = await create_execution_with_task_and_result(session_factory)

    config = {"configurable": {"thread_id": str(exec_id)}}
    await graph.aupdate_state(
        config=config,
        values={"accepted_result_ids": [], "task_outputs": {}},
        as_node="worker_node",
    )

    event_id = uuid.uuid4()
    event = TaskCompletionEvent(
        event_id=event_id,
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=result_id,
    )

    service = CompletionReconciliationService(session_factory, default_graph=graph)

    # First reconciliation
    res_1 = await service.reconcile_task_event(event=event)
    assert res_1.status == "APPLIED"

    # Second reconciliation with identical event
    res_2 = await service.reconcile_task_event(event=event)
    assert res_2.status == "ALREADY_APPLIED"
    assert res_2.checkpoint_updated is False
    assert res_2.graph_resumed is False

    # Check outbox events count: exactly 1 EXECUTION_RESUME event
    async with session_factory() as session:
        events = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
            )
        ).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_crash_before_checkpoint_update_resumes_unapplied_receipt(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that a crash between receipt storage and checkpoint application resumes the unapplied receipt."""
    graph = build_test_execution_graph(postgres_checkpointer)
    exec_id, task_id, result_id = await create_execution_with_task_and_result(session_factory)

    config = {"configurable": {"thread_id": str(exec_id)}}
    await graph.aupdate_state(
        config=config,
        values={"accepted_result_ids": [], "task_outputs": {}},
        as_node="worker_node",
    )

    event_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Injected Crash: Insert completion_receipts record with applied_at = NULL (simulating crash before checkpoint update)
    async with session_factory() as session:
        receipt = CompletionReceiptModel(
            event_id=event_id,
            task_id=task_id,
            execution_id=exec_id,
            attempt=1,
            result_id=result_id,
            received_at=now,
            applied_at=None,  # Not yet applied
        )
        session.add(receipt)
        await session.commit()

    service = CompletionReconciliationService(session_factory, default_graph=graph)

    # Replay event (e.g. from queue redelivery)
    event = TaskCompletionEvent(
        event_id=event_id,
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=result_id,
    )
    res = await service.reconcile_task_event(event=event)

    # Verify receipt was resumed and applied rather than discarded!
    assert res.status == "APPLIED"
    assert res.checkpoint_updated is True
    assert res.graph_resumed is True

    # Check receipt now has applied_at
    async with session_factory() as session:
        r = (
            await session.execute(
                select(CompletionReceiptModel).where(CompletionReceiptModel.event_id == event_id)
            )
        ).scalar_one()
        assert r.applied_at is not None

    # Check checkpoint contains the result
    state = await graph.aget_state(config=config)
    assert str(result_id) in state.values["accepted_result_ids"]


@pytest.mark.asyncio
async def test_crash_after_checkpoint_update_before_applied_at(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that a crash after checkpoint update detects accepted marker and marks applied_at without re-updating."""
    graph = build_test_execution_graph(postgres_checkpointer)
    exec_id, task_id, result_id = await create_execution_with_task_and_result(session_factory)

    config = {"configurable": {"thread_id": str(exec_id)}}
    # Checkpoint already contains marker from crashed attempt
    await graph.aupdate_state(
        config=config,
        values={
            "accepted_result_ids": [str(result_id)],
            "task_outputs": {str(task_id): {"data": "cached"}},
        },
        as_node="worker_node",
    )

    event_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Injected Crash: receipt exists in DB with applied_at = NULL
    async with session_factory() as session:
        receipt = CompletionReceiptModel(
            event_id=event_id,
            task_id=task_id,
            execution_id=exec_id,
            attempt=1,
            result_id=result_id,
            received_at=now,
            applied_at=None,
        )
        session.add(receipt)
        await session.commit()

    service = CompletionReconciliationService(session_factory, default_graph=graph)

    event = TaskCompletionEvent(
        event_id=event_id,
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=result_id,
    )
    res = await service.reconcile_task_event(event=event)

    assert res.status == "APPLIED"
    assert res.checkpoint_updated is False  # Bypassed duplicate update!

    # Check receipt is now marked applied
    async with session_factory() as session:
        r = (
            await session.execute(
                select(CompletionReceiptModel).where(CompletionReceiptModel.event_id == event_id)
            )
        ).scalar_one()
        assert r.applied_at is not None


@pytest.mark.asyncio
async def test_concurrent_results_merge_outputs_without_overwriting(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that concurrent task completions merge outputs without overwriting each other."""
    graph = build_test_execution_graph(postgres_checkpointer)

    # Create parent execution with two completed tasks
    exec_id, task_1, result_1 = await create_execution_with_task_and_result(
        session_factory,
        output={"worker_1": "flight_details"},
    )
    _, task_2, result_2 = await create_execution_with_task_and_result(
        session_factory,
        execution_id=exec_id,
        output={"worker_2": "hotel_details"},
    )

    config = {"configurable": {"thread_id": str(exec_id)}}
    await graph.aupdate_state(
        config=config,
        values={"accepted_result_ids": [], "task_outputs": {}},
        as_node="worker_node",
    )

    event_1 = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_1,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=result_1,
    )
    event_2 = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_2,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=result_2,
    )

    service = CompletionReconciliationService(session_factory, default_graph=graph)

    # Reconcile concurrently
    res_1, res_2 = await asyncio.gather(
        service.reconcile_task_event(event=event_1),
        service.reconcile_task_event(event=event_2),
    )

    assert res_1.status == "APPLIED"
    assert res_2.status == "APPLIED"

    # Check merged LangGraph thread state
    state = await graph.aget_state(config=config)
    accepted = state.values["accepted_result_ids"]
    assert str(result_1) in accepted
    assert str(result_2) in accepted

    outputs = state.values["task_outputs"]
    assert str(task_1) in outputs
    assert str(task_2) in outputs
    assert outputs[str(task_1)] == {"worker_1": "flight_details"}
    assert outputs[str(task_2)] == {"worker_2": "hotel_details"}

    # Exactly one resume outbox event emitted
    async with session_factory() as session:
        events = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
            )
        ).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_cancellation_barrier_skips_checkpoint_update(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that cancelled execution rejects checkpoint updates and graph resume."""
    graph = build_test_execution_graph(postgres_checkpointer)
    now = datetime.now(UTC)
    exec_id, task_id, result_id = await create_execution_with_task_and_result(
        session_factory,
        execution_status="CANCELLED",
        cancelled_at=now,
    )

    config = {"configurable": {"thread_id": str(exec_id)}}
    await graph.aupdate_state(
        config=config,
        values={"accepted_result_ids": [], "task_outputs": {}},
        as_node="worker_node",
    )

    event_id = uuid.uuid4()
    event = TaskCompletionEvent(
        event_id=event_id,
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=result_id,
    )

    service = CompletionReconciliationService(session_factory, default_graph=graph)
    res = await service.reconcile_task_event(event=event)

    assert res.status == "CANCELLED_BARRIER"
    assert res.checkpoint_updated is False
    assert res.graph_resumed is False

    # Check receipt marked applied to clear pending backlog
    async with session_factory() as session:
        r = (
            await session.execute(
                select(CompletionReceiptModel).where(CompletionReceiptModel.event_id == event_id)
            )
        ).scalar_one()
        assert r.applied_at is not None

        # Zero EXECUTION_RESUME events
        resume_events = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
            )
        ).scalars().all()
        assert len(resume_events) == 0

    # Checkpoint was not modified
    state = await graph.aget_state(config=config)
    assert len(state.values.get("accepted_result_ids", [])) == 0


@pytest.mark.asyncio
async def test_deadline_barrier_blocks_graph_resume(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that an expired deadline blocks graph resume and marks execution FAILED."""
    graph = build_test_execution_graph(postgres_checkpointer)
    exec_id, task_id, result_id = await create_execution_with_task_and_result(
        session_factory,
        deadline_minutes=-10,  # Expired deadline
    )

    event_id = uuid.uuid4()
    event = TaskCompletionEvent(
        event_id=event_id,
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=result_id,
    )

    service = CompletionReconciliationService(session_factory, default_graph=graph)
    res = await service.reconcile_task_event(event=event)

    assert res.status == "DEADLINE_EXCEEDED"
    assert res.graph_resumed is False

    async with session_factory() as session:
        execution = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        assert execution.status == "FAILED"


@pytest.mark.asyncio
async def test_stale_context_version_rejected(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that mismatched context version is rejected from checkpoint update."""
    graph = build_test_execution_graph(postgres_checkpointer)
    exec_id, task_id, result_id = await create_execution_with_task_and_result(
        session_factory,
        context_version=1,
    )

    # Bump execution context_version to 2
    async with session_factory() as session:
        ex = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        ex.context_version = 2
        await session.commit()

    event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=result_id,
    )

    service = CompletionReconciliationService(session_factory, default_graph=graph)
    res = await service.reconcile_task_event(event=event)

    assert res.status == "STALE_CONTEXT"
    assert res.checkpoint_updated is False
    assert res.graph_resumed is False


@pytest.mark.asyncio
async def test_stale_attempt_rejected(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that an older attempt number is rejected as stale."""
    graph = build_test_execution_graph(postgres_checkpointer)
    exec_id, task_id, result_id = await create_execution_with_task_and_result(
        session_factory,
        attempt=2,
    )

    event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=result_id,
    )

    service = CompletionReconciliationService(session_factory, default_graph=graph)
    res = await service.reconcile_task_event(event=event)

    assert res.status == "STALE_ATTEMPT"
    assert res.checkpoint_updated is False
    assert res.graph_resumed is False


@pytest.mark.asyncio
async def test_failed_task_outcome_unblocks_join_prevents_infinite_wait(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that failed tasks record outcome in checkpoint and fail execution, preventing deadlock."""
    graph = build_test_execution_graph(postgres_checkpointer)
    exec_id, task_id, _ = await create_execution_with_task_and_result(
        session_factory,
        task_status="FAILED",
    )

    config = {"configurable": {"thread_id": str(exec_id)}}
    await graph.aupdate_state(
        config=config,
        values={"accepted_result_ids": [], "task_outputs": {}},
        as_node="worker_node",
    )

    event_id = uuid.uuid4()
    event = TaskCompletionEvent(
        event_id=event_id,
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_FAILED",
        error={"code": "API_RATE_LIMIT", "message": "Failed"},
    )

    service = CompletionReconciliationService(session_factory, default_graph=graph)
    res = await service.reconcile_task_event(event=event)

    assert res.status == "FAILED_TASK_APPLIED"
    assert res.checkpoint_updated is True
    assert res.graph_resumed is False

    # Execution transitioned to FAILED and EXECUTION_FAILED outbox event emitted
    async with session_factory() as session:
        execution = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        assert execution.status == "FAILED"

        fail_event = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_FAILED",
                )
            )
        ).scalar_one()
        assert fail_event.payload_ref["reason"] == "tasks_failed_or_cancelled"


@pytest.mark.asyncio
async def test_stalled_receipts_recovery_and_bounded_loop(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that scheduled reconciliation loop processes stalled unapplied receipts within time budget."""
    graph = build_test_execution_graph(postgres_checkpointer)
    exec_id, task_id, result_id = await create_execution_with_task_and_result(session_factory)

    config = {"configurable": {"thread_id": str(exec_id)}}
    await graph.aupdate_state(
        config=config,
        values={"accepted_result_ids": [], "task_outputs": {}},
        as_node="worker_node",
    )

    event_id = uuid.uuid4()
    old_time = datetime.now(UTC) - timedelta(seconds=120)

    # Insert stalled receipt (received 120 seconds ago, applied_at IS NULL)
    async with session_factory() as session:
        receipt = CompletionReceiptModel(
            event_id=event_id,
            task_id=task_id,
            execution_id=exec_id,
            attempt=1,
            result_id=result_id,
            received_at=old_time,
            applied_at=None,
        )
        session.add(receipt)
        await session.commit()

    service = CompletionReconciliationService(
        session_factory,
        default_graph=graph,
        stalled_interval_seconds=60,
    )

    summary = await service.run_reconciliation_loop(
        max_runtime_seconds=10.0,
        execution_id=exec_id,
    )

    assert summary.total_scanned >= 1
    assert summary.total_applied >= 1

    # Verify receipt is now applied
    async with session_factory() as session:
        r = (
            await session.execute(
                select(CompletionReceiptModel).where(CompletionReceiptModel.event_id == event_id)
            )
        ).scalar_one()
        assert r.applied_at is not None
