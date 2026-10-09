import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from functions.orchestrator.execution import (
    CompletionReconciliationService,
    ExecutionModel,
    ExecutionStartEvent,
    GraphRunnerService,
    OutboxEventModel,
    TaskCompletionEvent,
    TaskModel,
    TaskRecoveryService,
    TransactionBoundCheckpointer,
    WorkerResultModel,
)
from functions.orchestrator.execution.job_events import get_job_events
from tests.integration.test_graph_runner import (
    build_test_runner_graph,
    create_test_execution,
)


@pytest.mark.asyncio
async def test_fresh_checkpointer_reads_persisted_state(session_factory, postgres_checkpointer) -> None:
    """Proves that a fresh checkpointer instance with zero in-memory state recovers state from PostgreSQL."""
    postgres_checkpointer.session_factory = session_factory
    thread_id = f"thread-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    checkpoint = {
        "id": "chk-001",
        "ts": "2026-10-09T00:00:00Z",
        "channel_values": {"status": "ACTIVE", "step": 1, "data": {"key": "val"}},
    }
    metadata = {"source": "test_fresh", "step": 1}

    # 1. Put and flush via first checkpointer
    postgres_checkpointer.put(config, checkpoint, metadata, {})
    postgres_checkpointer.put_writes(
        config,
        [("output_channel", {"task_data": "sample_output"})],
        "task-123",
    )

    async with session_factory() as session:
        async with session.begin():
            await postgres_checkpointer.flush(session, thread_id)

    # 2. Instantiate a completely fresh checkpointer with empty in-memory state
    fresh_cp = TransactionBoundCheckpointer(session_factory=session_factory)
    assert len(fresh_cp._checkpoints) == 0
    assert len(fresh_cp._writes) == 0
    assert len(fresh_cp._pending_flushes) == 0

    # 3. Read checkpoint tuple from PostgreSQL
    loaded = await fresh_cp.aget_tuple(config)
    assert loaded is not None
    assert loaded.checkpoint["id"] == "chk-001"
    assert loaded.checkpoint["channel_values"]["status"] == "ACTIVE"
    assert loaded.checkpoint["channel_values"]["data"]["key"] == "val"
    assert loaded.metadata.get("source") == "test_fresh"

    # Verify pending writes were loaded from checkpoint_writes table
    assert len(loaded.pending_writes) == 1
    task_id, channel, value = loaded.pending_writes[0]
    assert task_id == "task-123"
    assert channel == "output_channel"
    assert value == {"task_data": "sample_output"}

    # 4. Verify alist loads checkpoints from PostgreSQL
    listed = [item async for item in fresh_cp.alist(config)]
    assert len(listed) == 1
    assert listed[0].checkpoint["id"] == "chk-001"


@pytest.mark.asyncio
async def test_expired_runner_takeover_stale_writes_rejected(
    session_factory, postgres_checkpointer
) -> None:
    """Proves expired runner cannot commit late checkpoints or overwrite subsequent runner progress."""
    graph = build_test_runner_graph(postgres_checkpointer)
    original_ainvoke = graph.ainvoke

    pause_event = asyncio.Event()
    resume_event = asyncio.Event()

    runner_a = GraphRunnerService(
        session_factory,
        default_graph=graph,
        max_runtime_seconds=2,  # very short lease
    )

    runner_b = GraphRunnerService(
        session_factory,
        default_graph=graph,
        max_runtime_seconds=60,
    )

    exec_id = await create_test_execution(session_factory, status="CREATED")
    slow_ids = {exec_id}

    async def hooked_ainvoke(state, config, **kwargs):
        if config["configurable"]["thread_id"] == str(exec_id) and len(slow_ids) > 0:
            slow_ids.clear()
            pause_event.set()
            await resume_event.wait()
        return await original_ainvoke(state, config, **kwargs)

    graph.ainvoke = hooked_ainvoke

    event_a = ExecutionStartEvent(execution_id=exec_id)
    task_a = asyncio.create_task(
        runner_a.handle_execution_start(event=event_a, graph=graph)
    )

    # Wait for Runner A to reach Phase 2
    await pause_event.wait()

    # Wait for Runner A's lease to expire
    await asyncio.sleep(3)

    # Runner B takes over and completes Phase 3
    event_b = ExecutionStartEvent(execution_id=exec_id)
    res_b = await runner_b.handle_execution_start(event=event_b, graph=graph)

    config = {"configurable": {"thread_id": str(exec_id)}}
    state_b = await graph.aget_state(config)

    # Release Runner A
    resume_event.set()
    res_a = await task_a

    # Check state again
    state_a = await graph.aget_state(config)

    assert res_b.tasks_dispatched == 1
    assert res_a.tasks_dispatched == 0
    assert state_a.values.get("status") == state_b.values.get("status")
    # Verify Runner A's uncommitted buffer was discarded
    assert len(postgres_checkpointer._pending_flushes.get(str(exec_id), [])) == 0


@pytest.mark.asyncio
async def test_rejected_buffer_does_not_leak_into_subsequent_invocation(
    session_factory, postgres_checkpointer
) -> None:
    """Proves discarded checkpointer buffers never leak into subsequent execution transactions."""
    postgres_checkpointer.session_factory = session_factory
    thread_aborted = f"thread-abort-{uuid.uuid4()}"
    thread_valid = f"thread-valid-{uuid.uuid4()}"

    config_aborted = {"configurable": {"thread_id": thread_aborted, "checkpoint_ns": ""}}
    config_valid = {"configurable": {"thread_id": thread_valid, "checkpoint_ns": ""}}

    # 1. Simulate aborted runner buffering state
    postgres_checkpointer.put(
        config_aborted,
        {"id": "bad-chk", "channel_values": {"stale": True}},
        {"meta": "bad"},
        {},
    )
    postgres_checkpointer.put_writes(
        config_aborted,
        [("channel_bad", "bad_val")],
        "task-bad",
    )

    # Abort/discard buffer
    postgres_checkpointer.discard(thread_aborted)

    assert thread_aborted not in postgres_checkpointer._checkpoints
    assert thread_aborted not in postgres_checkpointer._pending_flushes

    # 2. Perform valid flush for thread_valid
    postgres_checkpointer.put(
        config_valid,
        {"id": "good-chk", "channel_values": {"good": True}},
        {"meta": "good"},
        {},
    )

    async with session_factory() as session:
        async with session.begin():
            await postgres_checkpointer.flush(session, thread_valid)

    # 3. Assert PostgreSQL contains thread_valid, but zero rows for thread_aborted
    async with session_factory() as session:
        aborted_chk = await session.execute(
            text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
            {"t": thread_aborted},
        )
        assert aborted_chk.scalar() == 0

        aborted_writes = await session.execute(
            text("SELECT count(*) FROM checkpoint_writes WHERE thread_id = :t"),
            {"t": thread_aborted},
        )
        assert aborted_writes.scalar() == 0

        valid_chk = await session.execute(
            text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
            {"t": thread_valid},
        )
        assert valid_chk.scalar() == 1


@pytest.mark.asyncio
async def test_concurrent_execution_recovery_produces_one_redispatch(
    session_factory,
) -> None:
    """Proves concurrent recovery workers recover an expired execution exactly once without duplicate outbox events."""
    now = datetime.now(UTC)
    exec_id = await create_test_execution(session_factory, status="CREATED")

    # Manually transition to RUNNING with expired lease
    async with session_factory() as session:
        async with session.begin():
            exec_row = await session.get(ExecutionModel, exec_id)
            exec_row.status = "RUNNING"
            exec_row.lease_token = uuid.uuid4()
            exec_row.lease_expiry = now - timedelta(seconds=15)
            exec_row.updated_at = now - timedelta(seconds=15)

    service_a = TaskRecoveryService(session_factory)
    service_b = TaskRecoveryService(session_factory)

    # Run concurrent recovery
    res_a, res_b = await asyncio.gather(
        service_a.recover_expired_execution_leases(batch_size=10),
        service_b.recover_expired_execution_leases(batch_size=10),
    )

    total_recovered = res_a.recovered_resumed + res_b.recovered_resumed
    assert exec_id in total_recovered
    assert total_recovered.count(exec_id) == 1  # Exactly one worker recovered it

    assert (exec_id in res_a.recovered_resumed) ^ (exec_id in res_b.recovered_resumed)

    # Check database state
    async with session_factory() as session:
        exec_after = await session.get(ExecutionModel, exec_id)
        assert exec_after.status == "CREATED"
        assert exec_after.lease_token is None
        assert exec_after.lease_expiry is None

        # Verify exactly one EXECUTION_START outbox event was created
        events_res = await session.execute(
            select(OutboxEventModel).where(
                OutboxEventModel.execution_id == exec_id,
                OutboxEventModel.kind == "EXECUTION_START",
            )
        )
        events = events_res.scalars().all()
        assert len(events) == 1

        # Verify job timeline event
        timeline, _ = await get_job_events(session, execution_id=exec_id)
        recovery_events = [e for e in timeline if e.event_type == "EXECUTION_LEASE_RECOVERED"]
        assert len(recovery_events) == 1


@pytest.mark.asyncio
async def test_deadline_failure_events_and_rollback_consistency(
    session_factory, postgres_checkpointer
) -> None:
    """Proves deadline expiration in reconciliation atomically commits FAILED status, EXECUTION_FAILED outbox event, and job event without duplicates."""
    now = datetime.now(UTC)
    exec_id = await create_test_execution(
        session_factory,
        status="RUNNING",
        deadline_at=now - timedelta(seconds=10),
    )
    task_id = uuid.uuid4()
    result_id = uuid.uuid4()

    async with session_factory() as session:
        async with session.begin():
            task = TaskModel(
                id=task_id,
                execution_id=exec_id,
                worker="EVIDENCE",
                action="collect",
                payload_ref={},
                input_hash="hash",
                idempotency_key=f"task-{task_id}",
                status="RUNNING",
                attempt=0,
                max_attempts=3,
                context_version=1,
                lease_token=uuid.uuid4(),
                lease_expiry=now + timedelta(seconds=60),
                deadline_at=now - timedelta(seconds=10),
                created_at=now,
                updated_at=now,
            )
            session.add(task)
            worker_res = WorkerResultModel(
                id=result_id,
                task_id=task_id,
                execution_id=exec_id,
                status="SUCCEEDED",
                output_ref={"key": "val"},
                attempt=0,
                context_version=1,
                warnings=[],
                usage={},
                started_at=now,
                finished_at=now,
            )
            session.add(worker_res)

    graph = build_test_runner_graph(postgres_checkpointer)
    reconciler = CompletionReconciliationService(session_factory, default_graph=graph)

    # First reconciliation: should trigger deadline barrier
    event_1 = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_id,
        execution_id=exec_id,
        attempt=0,
        result_id=result_id,
        output={"key": "val"},
    )
    res_1 = await reconciler.reconcile_task_event(event=event_1)
    assert res_1.status == "DEADLINE_EXCEEDED"

    async with session_factory() as session:
        exec_row = await session.get(ExecutionModel, exec_id)
        assert exec_row.status == "FAILED"
        assert exec_row.lease_token is None

        # Verify EXECUTION_FAILED outbox event exists
        outbox_res = await session.execute(
            select(OutboxEventModel).where(
                OutboxEventModel.execution_id == exec_id,
                OutboxEventModel.kind == "EXECUTION_FAILED",
            )
        )
        outbox_events = outbox_res.scalars().all()
        assert len(outbox_events) == 1
        assert outbox_events[0].payload_ref["reason"] == "Deadline exceeded during reconciliation"

        # Verify job event exists
        timeline, _ = await get_job_events(session, execution_id=exec_id)
        failed_timeline = [e for e in timeline if e.event_type == "EXECUTION_FAILED"]
        assert len(failed_timeline) == 1

    # Second reconciliation on the deadlined execution: verify no duplicate outbox event
    event_2 = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_id,
        execution_id=exec_id,
        attempt=0,
        result_id=result_id,
        output={"key": "val"},
    )
    res_2 = await reconciler.reconcile_task_event(event=event_2)
    assert res_2.status in ("DEADLINE_EXCEEDED", "PARENT_TERMINAL")

    async with session_factory() as session:
        outbox_res_2 = await session.execute(
            select(OutboxEventModel).where(
                OutboxEventModel.execution_id == exec_id,
                OutboxEventModel.kind == "EXECUTION_FAILED",
            )
        )
        assert len(outbox_res_2.scalars().all()) == 1
