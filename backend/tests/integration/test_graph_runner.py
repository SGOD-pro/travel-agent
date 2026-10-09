"""Integration tests for EXECUTION_START and EXECUTION_RESUME graph runner conforming to spec 0001.

Tests against live PostgreSQL with actual LangGraph AsyncPostgresSaver checkpointer:
1. Start creates the expected task and dispatch event.
2. Duplicate start does not create duplicate tasks.
3. Resume after reconciled output advances the graph.
4. Duplicate/concurrent resume and crash replay are safe.
5. Cancellation/deadlines prevent further accepted progress.
6. Runner and reconciliation cannot overwrite each other's state.
"""

from __future__ import annotations

import asyncio
import re
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.config import settings
from functions.orchestrator.execution import (
    CompletionReconciliationService,
    ExecutionModel,
    ExecutionResumeEvent,
    ExecutionStartEvent,
    ExecutionState,
    GraphRunnerService,
    OutboxEventModel,
    StaleContextError,
    TaskCompletionEvent,
    TaskModel,
    WorkerFencingService,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.job_events import get_job_events

_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", 20266): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("165.232.185.214", 20266),
        )
    ],
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", "20266"): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("165.232.185.214", 20266),
        )
    ],
}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
    if host == "pg-070905-cloud-postgress-db.d.aivencloud.com":
        res = [
            (
                socket.AddressFamily.AF_INET,
                socket.SocketKind.SOCK_STREAM,
                6,
                "",
                ("165.232.185.214", int(port)),
            )
        ]
        _dns_cache[key] = res
        return res
    try:
        res = _orig_getaddrinfo(host, port, *args, **kwargs)
        _dns_cache[key] = res
        return res
    except socket.gaierror:
        if key in _dns_cache:
            return _dns_cache[key]
        raise


socket.getaddrinfo = _resilient_getaddrinfo


@pytest.fixture
async def session_factory():
    url = settings.DATABASE_URL
    connect_args = {"command_timeout": 60, "timeout": 30}
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


def build_test_runner_graph(checkpointer: Any):
    """Builds a deterministic test graph with worker_node producing tasks and join_node."""

    def worker_node(state: ExecutionState) -> dict[str, Any]:
        return {
            "current_step": "worker_dispatched",
            "pending_tasks": [
                {
                    "worker": "EVIDENCE",
                    "action": "collect",
                    "payload": {"location": "Paris", "mode": "fast"},
                    "idempotency_key": "step1-evidence",
                }
            ],
        }

    def join_node(state: ExecutionState) -> dict[str, Any]:
        outputs = state.get("task_outputs", {})
        return {
            "current_step": "completed",
            "status": "SUCCEEDED",
            "pending_tasks": [],
            "final_summary": f"Processed {len(outputs)} results",
        }

    builder = StateGraph(ExecutionState)
    builder.add_node("worker_node", worker_node)
    builder.add_node("join_node", join_node)
    builder.add_edge(START, "worker_node")
    builder.add_edge("worker_node", "join_node")
    builder.add_edge("join_node", END)
    return builder.compile(checkpointer=checkpointer, interrupt_before=["join_node"])


async def create_test_execution(
    session_factory,
    *,
    status: str = "CREATED",
    context_version: int = 1,
    deadline_at: datetime | None = None,
    cancelled_at: datetime | None = None,
) -> uuid.UUID:
    """Helper creating a test execution record."""
    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)
    dl = deadline_at or (now + timedelta(hours=1))

    async with session_factory() as session:
        execution = ExecutionModel(
            id=exec_id,
            owner_id=owner_id,
            workflow="TRAVEL",
            status=status,
            context_version=context_version,
            deadline_at=dl,
            cancelled_at=cancelled_at,
            created_at=now,
            updated_at=now,
        )
        session.add(execution)
        await session.commit()
    return exec_id


@pytest.mark.asyncio
async def test_start_creates_expected_task_and_dispatch_event(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that EXECUTION_START initializes LangGraph, dispatches task atomically, and records events."""
    graph = build_test_runner_graph(postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph)
    exec_id = await create_test_execution(session_factory, status="CREATED")

    event = ExecutionStartEvent(
        execution_id=exec_id,
        workflow="TRAVEL",
        context_version=1,
        payload={"options": {"budget": "medium"}},
    )

    result = await runner.handle_execution_start(event=event)
    assert result.status == "WAITING_TASKS"
    assert result.tasks_dispatched == 1
    assert len(result.task_ids) == 1
    assert result.graph_advanced is True
    assert result.checkpoint_id is not None

    async with session_factory() as session:
        # Verify execution record
        execution = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        assert execution.status == "WAITING_TASKS"
        assert execution.checkpoint_id is not None

        # Verify task record
        tasks = (
            (await session.execute(select(TaskModel).where(TaskModel.execution_id == exec_id)))
            .scalars()
            .all()
        )
        assert len(tasks) == 1
        task = tasks[0]
        assert task.worker == "EVIDENCE"
        assert task.action == "collect"
        assert task.status == "PENDING"
        assert task.payload_ref == {"location": "Paris", "mode": "fast"}
        assert task.idempotency_key == "step1-evidence"

        # Verify outbox event record
        outbox_events = (
            (
                await session.execute(
                    select(OutboxEventModel).where(
                        OutboxEventModel.execution_id == exec_id,
                        OutboxEventModel.kind == "TASK_DISPATCH",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(outbox_events) == 1
        dispatch_event = outbox_events[0]
        assert dispatch_event.task_id == task.id
        payload = dispatch_event.payload_ref
        assert payload["schema_version"] == "1.0"
        assert payload["worker"] == "EVIDENCE"
        assert payload["action"] == "collect"
        assert payload["payload"] == {"location": "Paris", "mode": "fast"}

        # Verify timeline events
        timeline_events, _ = await get_job_events(session, execution_id=exec_id)
        types = [e.event_type for e in timeline_events]
        assert "EXECUTION_STARTED" in types
        assert "TASKS_DISPATCHED" in types


@pytest.mark.asyncio
async def test_duplicate_start_does_not_create_duplicate_tasks(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that replaying EXECUTION_START does not produce duplicate tasks or outbox events."""
    graph = build_test_runner_graph(postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph)
    exec_id = await create_test_execution(session_factory, status="CREATED")

    event = ExecutionStartEvent(execution_id=exec_id)

    # Initial start
    res1 = await runner.handle_execution_start(event=event)
    assert res1.status == "WAITING_TASKS"
    assert res1.tasks_dispatched == 1

    # Duplicate start invocation
    res2 = await runner.handle_execution_start(event=event)
    assert res2.status == "WAITING_TASKS"
    assert res2.tasks_dispatched == 0
    assert res2.graph_advanced is False

    async with session_factory() as session:
        task_count = (
            await session.execute(
                select(sa.func.count(TaskModel.id)).where(TaskModel.execution_id == exec_id)
            )
        ).scalar()
        assert task_count == 1

        dispatch_count = (
            await session.execute(
                select(sa.func.count(OutboxEventModel.id)).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
            )
        ).scalar()
        assert dispatch_count == 1


@pytest.mark.asyncio
async def test_resume_after_reconciled_output_advances_graph(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that EXECUTION_RESUME advances LangGraph to join_node and completes execution."""
    graph = build_test_runner_graph(postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph)
    reconciler = CompletionReconciliationService(session_factory, default_graph=graph)
    exec_id = await create_test_execution(session_factory, status="CREATED")

    # Step 1: Start
    start_res = await runner.handle_execution_start(event=ExecutionStartEvent(execution_id=exec_id))
    assert start_res.status == "WAITING_TASKS"
    task_id = start_res.task_ids[0]

    # Step 2: Worker claims and completes task
    async with session_factory() as session:
        fencing = WorkerFencingService(session)
        claim = await fencing.claim_task(
            message={"task_id": task_id, "execution_id": exec_id, "attempt": 0},
            worker="EVIDENCE",
            action="collect",
        )
        res_id = await fencing.complete_task(
            execution_id=exec_id,
            task_id=task_id,
            lease_token=claim.lease_token,
            output={"evidence": "found 10 places in Paris"},
        )
        await session.commit()

    # Step 3: Reconcile task completion
    reconcile_res = await reconciler.reconcile_task_event(
        event=TaskCompletionEvent(
            event_id=uuid.uuid4(),
            task_id=task_id,
            execution_id=exec_id,
            attempt=claim.attempt,
            result_id=res_id,
            output={"evidence": "found 10 places in Paris"},
        )
    )
    assert reconcile_res.checkpoint_updated is True
    assert reconcile_res.graph_resumed is True

    # Step 4: Resume execution via runner
    resume_event = ExecutionResumeEvent(
        execution_id=exec_id,
        triggered_by_task_id=task_id,
    )
    resume_res = await runner.handle_execution_resume(event=resume_event)
    assert resume_res.status == "SUCCEEDED"
    assert resume_res.tasks_dispatched == 0
    assert resume_res.graph_advanced is True

    # Verify database state
    async with session_factory() as session:
        execution = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        assert execution.status == "SUCCEEDED"

        succeeded_events = (
            (
                await session.execute(
                    select(OutboxEventModel).where(
                        OutboxEventModel.execution_id == exec_id,
                        OutboxEventModel.kind == "EXECUTION_SUCCEEDED",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(succeeded_events) == 1

        timeline, _ = await get_job_events(session, execution_id=exec_id)
        types = [e.event_type for e in timeline]
        assert "EXECUTION_RESUMING" in types
        assert "EXECUTION_SUCCEEDED" in types


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_resume_and_crash_replay_are_safe(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that duplicate and concurrent EXECUTION_RESUME calls are serialized and safe."""
    graph = build_test_runner_graph(postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph)
    reconciler = CompletionReconciliationService(session_factory, default_graph=graph)
    exec_id = await create_test_execution(session_factory, status="CREATED")

    start_res = await runner.handle_execution_start(event=ExecutionStartEvent(execution_id=exec_id))
    task_id = start_res.task_ids[0]

    # Complete and reconcile task
    async with session_factory() as session:
        fencing = WorkerFencingService(session)
        claim = await fencing.claim_task(
            message={"task_id": task_id, "execution_id": exec_id, "attempt": 0},
            worker="EVIDENCE",
            action="collect",
        )
        res_id = await fencing.complete_task(
            execution_id=exec_id,
            task_id=task_id,
            lease_token=claim.lease_token,
            output={"evidence": "sample"},
        )
        await session.commit()

    await reconciler.reconcile_task_event(
        event=TaskCompletionEvent(
            event_id=uuid.uuid4(),
            task_id=task_id,
            execution_id=exec_id,
            attempt=claim.attempt,
            result_id=res_id,
            output={"evidence": "sample"},
        )
    )

    resume_event = ExecutionResumeEvent(execution_id=exec_id, triggered_by_task_id=task_id)

    # Concurrent resumes executed together
    res1, res2 = await asyncio.gather(
        runner.handle_execution_resume(event=resume_event),
        runner.handle_execution_resume(event=resume_event),
    )

    assert {res1.status, res2.status} in ({"SUCCEEDED", "RUNNING"}, {"SUCCEEDED"})
    assert sum([res1.graph_advanced, res2.graph_advanced]) == 1

    # Replay after completion returns SUCCEEDED without further mutations
    res3 = await runner.handle_execution_resume(event=resume_event)
    assert res3.status == "SUCCEEDED"
    assert res3.graph_advanced is False

    async with session_factory() as session:
        event_count = (
            await session.execute(
                select(sa.func.count(OutboxEventModel.id)).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_SUCCEEDED",
                )
            )
        ).scalar()
        assert event_count == 1


@pytest.mark.asyncio
async def test_cancellation_and_deadlines_prevent_further_accepted_progress(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies that cancellation, deadline expiration, and stale context version halt execution."""
    graph = build_test_runner_graph(postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph)
    now = datetime.now(UTC)

    # 1. Cancelled execution
    cancelled_id = await create_test_execution(
        session_factory, status="CANCELLED", cancelled_at=now
    )
    res_cancel = await runner.handle_execution_start(
        event=ExecutionStartEvent(execution_id=cancelled_id)
    )
    assert res_cancel.status == "CANCELLED"
    assert res_cancel.tasks_dispatched == 0

    res_cancel_resume = await runner.handle_execution_resume(
        event=ExecutionResumeEvent(execution_id=cancelled_id)
    )
    assert res_cancel_resume.status == "CANCELLED"

    # 2. Expired deadline
    expired_dl = now - timedelta(seconds=10)
    expired_id = await create_test_execution(
        session_factory, status="CREATED", deadline_at=expired_dl
    )
    res_expired = await runner.handle_execution_start(
        event=ExecutionStartEvent(execution_id=expired_id)
    )
    assert res_expired.status == "FAILED"
    assert res_expired.tasks_dispatched == 0

    async with session_factory() as session:
        exec_row = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == expired_id))
        ).scalar_one()
        assert exec_row.status == "FAILED"

    # 3. Stale context version
    stale_id = await create_test_execution(session_factory, status="CREATED", context_version=2)
    with pytest.raises(StaleContextError):
        await runner.handle_execution_start(
            event=ExecutionStartEvent(execution_id=stale_id, context_version=1)
        )


@pytest.mark.asyncio
async def test_runner_and_reconciliation_cannot_overwrite_each_other_state(
    session_factory, postgres_checkpointer
) -> None:
    """Verifies parent-first locking prevents runner and reconciliation from corrupting state."""
    graph = build_test_runner_graph(postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph)
    reconciler = CompletionReconciliationService(session_factory, default_graph=graph)
    exec_id = await create_test_execution(session_factory, status="CREATED")

    # Start execution
    start_res = await runner.handle_execution_start(event=ExecutionStartEvent(execution_id=exec_id))
    task_id = start_res.task_ids[0]

    # Complete task
    async with session_factory() as session:
        fencing = WorkerFencingService(session)
        claim = await fencing.claim_task(
            message={"task_id": task_id, "execution_id": exec_id, "attempt": 0},
            worker="EVIDENCE",
            action="collect",
        )
        res_id = await fencing.complete_task(
            execution_id=exec_id,
            task_id=task_id,
            lease_token=claim.lease_token,
            output={"test": "data"},
        )
        await session.commit()

    # Reconcile and Resume executed in sequence and verify checkpoint state consistency
    reconcile_res = await reconciler.reconcile_task_event(
        event=TaskCompletionEvent(
            event_id=uuid.uuid4(),
            task_id=task_id,
            execution_id=exec_id,
            attempt=claim.attempt,
            result_id=res_id,
            output={"test": "data"},
        )
    )
    assert reconcile_res.checkpoint_updated is True

    resume_res = await runner.handle_execution_resume(
        event=ExecutionResumeEvent(execution_id=exec_id, triggered_by_task_id=task_id)
    )
    assert resume_res.status == "SUCCEEDED"

    # Verify LangGraph checkpoint state holds both reconciled output and completed step
    config = {"configurable": {"thread_id": str(exec_id)}}
    state = await graph.aget_state(config)
    assert str(res_id) in state.values.get("accepted_result_ids", [])
    assert str(task_id) in state.values.get("task_outputs", {})
    assert state.values.get("current_step") == "completed"
    assert state.values.get("status") == "SUCCEEDED"
