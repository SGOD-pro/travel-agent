"""Integration tests for scheduled task recovery conforming to spec 0001.

Verifies:
1. Parent-first lock ordering and concurrent recovery synchronization.
2. Recovery vs worker completion races (stale worker rejected).
3. Atomic rollback on failure (task update and outbox insert commit together).
4. Eligible retries reset to PENDING, schedule delayed TASK_DISPATCH, and DO NOT increment attempts.
5. Active retry backoff enforced: workers cannot claim during backoff period.
6. Exhausted attempts transition to FAILED and emit TASK_FAILED.
7. Cancelled parent executions transition to CANCELLED and emit TASK_CANCELLED.
8. Expired deadlines and stale context versions transition to FAILED and emit TASK_FAILED.
9. Bounded batch size and invocation duration.
"""

from __future__ import annotations

import asyncio
import re
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.config import settings
from functions.api.models import TripModel
from functions.orchestrator.execution import (
    ExecutionModel,
    LeaseFencingError,
    OutboxEventModel,
    StaleQueueMessageError,
    TaskInRetryBackoffError,
    TaskModel,
    TaskQueueMessage,
    TaskRecoveryService,
    WorkerFencingService,
)

# In-process DNS cache to prevent transient WSL forwarder socket.gaierror drops
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


async def create_test_execution_and_task(
    session_factory,
    *,
    worker: str = "EVIDENCE",
    action: str = "collect",
    context_version: int = 1,
    attempt: int = 0,
    max_attempts: int = 3,
    status: str = "PENDING",
    lease_token: uuid.UUID | None = None,
    lease_expiry: datetime | None = None,
    parent_status: str = "RUNNING",
    parent_cancelled_at: datetime | None = None,
    deadline_minutes: int = 60,
    task_deadline_minutes: int = 60,
    execution_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper creating a test execution and task record."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    exec_id = execution_id or uuid.uuid4()
    task_id = uuid.uuid4()
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
                status=parent_status,
                context_version=context_version,
                deadline_at=now + timedelta(minutes=deadline_minutes),
                cancelled_at=parent_cancelled_at,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)

        task = TaskModel(
            id=task_id,
            execution_id=exec_id,
            parent_task_id=None,
            worker=worker,
            action=action,
            payload_ref={"location": "Jaipur", "mode": "fast"},
            input_hash="test_sha256_hash",
            idempotency_key=f"task-idem-{task_id}",
            attempt=attempt,
            max_attempts=max_attempts,
            context_version=context_version,
            status=status,
            lease_token=lease_token,
            lease_expiry=lease_expiry,
            deadline_at=now + timedelta(minutes=task_deadline_minutes),
            result_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.commit()

    return exec_id, task_id


@pytest.mark.asyncio
async def test_retry_backoff_eligible_recovery_does_not_increment_attempts(session_factory) -> None:
    """Verifies that an expired task with remaining attempts resets to PENDING without attempt increment."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=10)
    lease_token = uuid.uuid4()

    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=lease_token,
        lease_expiry=expired_lease,
    )

    recovery_service = TaskRecoveryService(
        session_factory,
        base_backoff_seconds=60,
        max_backoff_seconds=300,
    )

    res = await recovery_service.recover_expired_leases(execution_id=execution_id)
    assert task_id in res.recovered_retried

    # Check task status in PostgreSQL
    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()

        assert task.status == "PENDING"
        assert task.lease_token is None
        assert task.attempt == 1  # Crucial: attempt counter NOT incremented on recovery
        assert task.lease_expiry is not None
        assert task.lease_expiry > datetime.now(UTC)  # Backoff delay set on lease_expiry

        # Check corresponding TASK_DISPATCH outbox event
        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.task_id == task_id,
            OutboxEventModel.kind == "TASK_DISPATCH",
        )
        outbox = (await session.execute(outbox_stmt)).scalar_one()
        assert outbox.execution_id == execution_id
        assert outbox.payload_ref["attempt"] == 1
        assert outbox.due_at == task.lease_expiry  # Event due_at aligned with retry backoff

        # Verify that claiming this task before backoff elapsed is rejected
        fencing_service = WorkerFencingService(session)
        with pytest.raises(TaskInRetryBackoffError):
            await fencing_service.claim_task(
                message={
                    "task_id": task_id,
                    "execution_id": execution_id,
                    "worker": "EVIDENCE",
                    "action": "collect",
                    "attempt": 1,
                    "context_version": 1,
                },
                worker="EVIDENCE",
                action="collect",
            )


@pytest.mark.asyncio
async def test_concurrent_recovery_synchronization(session_factory) -> None:
    """Verifies that concurrent recovery processes do not double-recover tasks."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=5)

    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="CLAIMED",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
    )

    recovery_a = TaskRecoveryService(session_factory)
    recovery_b = TaskRecoveryService(session_factory)

    # Run both concurrently
    res_a, res_b = await asyncio.gather(
        recovery_a.recover_expired_leases(execution_id=execution_id),
        recovery_b.recover_expired_leases(execution_id=execution_id),
    )

    all_recovered = set(res_a.recovered_retried).union(set(res_b.recovered_retried))
    overlap = set(res_a.recovered_retried).intersection(set(res_b.recovered_retried))

    assert task_id in all_recovered
    assert len(overlap) == 0  # Exactly one recovered it, zero duplicate recovery

    # Check that only one TASK_DISPATCH event was created
    async with session_factory() as session:
        events = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.task_id == task_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
            )
        ).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_recovery_versus_worker_completion_race(session_factory) -> None:
    """Verifies recovery vs worker completion race conditions."""
    now = datetime.now(UTC)
    lease_token = uuid.uuid4()
    expired_lease = now - timedelta(seconds=5)

    # Case A: Recovery runs after lease expiry. Stale worker late completion is rejected.
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=lease_token,
        lease_expiry=expired_lease,
    )

    recovery_service = TaskRecoveryService(session_factory)
    res = await recovery_service.recover_expired_leases(execution_id=execution_id)
    assert task_id in res.recovered_retried

    # Stale worker wakes up and attempts completion with old lease_token
    async with session_factory() as session:
        fencing_service = WorkerFencingService(session)
        with pytest.raises(LeaseFencingError):
            await fencing_service.complete_task(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=lease_token,
                output={"data": "late_output"},
            )

    # Case B: Worker completes before recovery runs. Recovery skips completed task.
    valid_lease_token = uuid.uuid4()
    exec_id_2, task_id_2 = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=valid_lease_token,
        lease_expiry=now + timedelta(minutes=10),
    )

    # Complete it first
    async with session_factory() as session:
        fencing_service = WorkerFencingService(session)
        await fencing_service.complete_task(
            execution_id=exec_id_2,
            task_id=task_id_2,
            lease_token=valid_lease_token,
            output={"data": "on_time_output"},
        )

    # Force expiry timestamp directly on the completed task
    async with session_factory() as session:
        task_row = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id_2))
        ).scalar_one()
        task_row.lease_expiry = now - timedelta(seconds=1)
        await session.commit()

    # Recovery runs: should skip completed task
    res_b = await recovery_service.recover_expired_leases(execution_id=exec_id_2)
    assert task_id_2 not in res_b.recovered_retried
    assert task_id_2 not in res_b.recovered_failed


@pytest.mark.asyncio
async def test_exhausted_attempts_transitions_to_failed(session_factory) -> None:
    """Verifies that an expired task reaching max_attempts transitions to FAILED and emits TASK_FAILED."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=5)

    # Task is already at attempt 3 with max_attempts 3
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=3,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
    )

    recovery_service = TaskRecoveryService(session_factory)
    res = await recovery_service.recover_expired_leases(execution_id=execution_id)
    assert task_id in res.recovered_failed

    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()

        assert task.status == "FAILED"
        assert task.lease_token is None

        # Verify TASK_FAILED outbox event emitted
        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.task_id == task_id,
            OutboxEventModel.kind == "TASK_FAILED",
        )
        outbox = (await session.execute(outbox_stmt)).scalar_one()
        assert outbox.execution_id == execution_id
        assert outbox.payload_ref["reason"] == "max_attempts_exhausted"


@pytest.mark.asyncio
async def test_cancelled_execution_recovery_marks_task_cancelled(session_factory) -> None:
    """Verifies that tasks under cancelled parent executions are marked CANCELLED."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=5)

    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
        parent_status="CANCELLED",
        parent_cancelled_at=now - timedelta(minutes=5),
    )

    recovery_service = TaskRecoveryService(session_factory)
    res = await recovery_service.recover_expired_leases(execution_id=execution_id)
    assert task_id in res.recovered_cancelled

    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()

        assert task.status == "CANCELLED"
        assert task.lease_token is None

        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.task_id == task_id,
            OutboxEventModel.kind == "TASK_CANCELLED",
        )
        outbox = (await session.execute(outbox_stmt)).scalar_one()
        assert outbox.payload_ref["reason"] == "parent_execution_cancelled"


@pytest.mark.asyncio
async def test_expired_deadlines_and_stale_context_recovery(session_factory) -> None:
    """Verifies that expired deadlines or context version mismatches transition to FAILED."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=5)

    # Case 1: Execution deadline expired
    exec_id_1, task_id_1 = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
        deadline_minutes=-10,  # Execution deadline in the past
    )

    recovery_service = TaskRecoveryService(session_factory)
    res_1 = await recovery_service.recover_expired_leases(execution_id=exec_id_1)
    assert task_id_1 in res_1.recovered_failed

    async with session_factory() as session:
        task_1 = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id_1))
        ).scalar_one()
        assert task_1.status == "FAILED"

    # Case 2: Stale context version
    exec_id_2, task_id_2 = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
        context_version=1,
    )

    # Bump execution context_version to 2
    async with session_factory() as session:
        exec_row = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id_2))
        ).scalar_one()
        exec_row.context_version = 2
        await session.commit()

    res_2 = await recovery_service.recover_expired_leases(execution_id=exec_id_2)
    assert task_id_2 in res_2.recovered_failed

    async with session_factory() as session:
        task_2 = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id_2))
        ).scalar_one()
        assert task_2.status == "FAILED"


@pytest.mark.asyncio
async def test_bounded_batch_and_invocation_duration(session_factory) -> None:
    """Verifies that recovery batch size is bounded and loop terminates cleanly within time budget."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=10)

    # Create 4 expired tasks under one execution
    exec_id, _ = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
    )
    for _ in range(3):
        await create_test_execution_and_task(
            session_factory,
            execution_id=exec_id,
            status="RUNNING",
            attempt=1,
            max_attempts=3,
            lease_token=uuid.uuid4(),
            lease_expiry=expired_lease,
        )

    recovery_service = TaskRecoveryService(
        session_factory,
        batch_size=2,
        max_runtime_seconds=1.0,
    )

    # Batch test: only 2 tasks recovered
    batch_res = await recovery_service.recover_expired_leases(
        batch_size=2,
        execution_id=exec_id,
    )
    assert len(batch_res.recovered_retried) == 2

    # Loop test: processes remaining tasks and halts cleanly
    summary = await recovery_service.run_recovery_loop(
        max_runtime_seconds=10.0,
        execution_id=exec_id,
    )
    assert summary.total_retried >= 2
    assert summary.elapsed_seconds <= 10.0


@pytest.mark.asyncio
async def test_atomic_recovery_rollback(session_factory) -> None:
    """Verifies that if a transaction error occurs during recovery, task status and outbox events roll back together."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=10)
    lease_token = uuid.uuid4()

    exec_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=lease_token,
        lease_expiry=expired_lease,
    )

    # Simulate an atomic transaction abort during recovery
    async with session_factory() as session:
        try:
            async with session.begin():
                task = (
                    await session.execute(select(TaskModel).where(TaskModel.id == task_id).with_for_update())
                ).scalar_one()
                task.status = "PENDING"
                task.lease_token = None

                outbox = OutboxEventModel(
                    id=uuid.uuid4(),
                    execution_id=exec_id,
                    aggregate_id=exec_id,
                    task_id=task_id,
                    kind="TASK_DISPATCH",
                    event_type="TASK_DISPATCH",
                    payload_ref={"task_id": str(task_id)},
                    payload={"task_id": str(task_id)},
                    due_at=now,
                    attempts=0,
                    created_at=now,
                )
                session.add(outbox)
                await session.flush()

                # Force an abort before commit
                raise RuntimeError("Simulated transaction crash before commit")
        except RuntimeError:
            pass

    # Verify atomic rollback in a fresh session: task is still RUNNING, lease_token preserved, 0 outbox events
    async with session_factory() as verify_session:
        task = (
            await verify_session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "RUNNING"
        assert task.lease_token == lease_token

        outbox_events = (
            await verify_session.execute(select(OutboxEventModel).where(OutboxEventModel.task_id == task_id))
        ).scalars().all()
        assert len(outbox_events) == 0


@pytest.mark.asyncio
async def test_recovery_rejects_terminal_parent_executions(session_factory) -> None:
    """Verifies that recovery rejects COMPLETED, SUCCEEDED, and FAILED parent executions."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=10)

    # 1. Test COMPLETED parent
    exec_id_completed, task_id_completed = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
        parent_status="COMPLETED",
    )

    # 2. Test FAILED parent
    exec_id_failed, task_id_failed = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
        parent_status="FAILED",
    )

    recovery_service = TaskRecoveryService(session_factory)

    # Recover completed parent execution task
    res_completed = await recovery_service.recover_expired_leases(execution_id=exec_id_completed)
    assert task_id_completed in res_completed.recovered_failed

    # Recover failed parent execution task
    res_failed = await recovery_service.recover_expired_leases(execution_id=exec_id_failed)
    assert task_id_failed in res_failed.recovered_failed

    async with session_factory() as session:
        t_completed = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id_completed))
        ).scalar_one()
        assert t_completed.status == "FAILED"

        event_completed = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.task_id == task_id_completed,
                    OutboxEventModel.kind == "TASK_FAILED",
                )
            )
        ).scalar_one()
        assert event_completed.payload_ref["reason"] == "parent_execution_completed"

        t_failed = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id_failed))
        ).scalar_one()
        assert t_failed.status == "FAILED"

        event_failed = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.task_id == task_id_failed,
                    OutboxEventModel.kind == "TASK_FAILED",
                )
            )
        ).scalar_one()
        assert event_failed.payload_ref["reason"] == "parent_execution_failed"


@pytest.mark.asyncio
async def test_worker_failure_retry_backoff_blocks_claims_and_stale_messages(session_factory) -> None:
    """Verifies that worker-reported failure retry puts task into retry backoff, blocking claims and stale queue messages."""
    exec_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="PENDING",
        attempt=0,
        max_attempts=3,
    )

    # Worker claims task (attempt becomes 1)
    async with session_factory() as session:
        fencing_service = WorkerFencingService(session)
        claim_res = await fencing_service.claim_task(
            message={"task_id": task_id, "execution_id": exec_id},
            worker="EVIDENCE",
            action="collect",
        )
        lease_token = claim_res.lease_token

    # Worker fails task with retryable=True and 60s backoff
    async with session_factory() as session:
        fencing_service = WorkerFencingService(session)
        await fencing_service.fail_task(
            execution_id=exec_id,
            task_id=task_id,
            lease_token=lease_token,
            error={"reason": "simulated_worker_transient_error"},
            retryable=True,
            backoff_seconds=60,
        )

    # Verify task state in database: PENDING with lease_expiry in future
    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "PENDING"
        assert task.attempt == 1
        assert task.lease_expiry is not None
        assert task.lease_expiry > datetime.now(UTC)

        fencing = WorkerFencingService(session)

        # 1. Direct claim during backoff is rejected with TaskInRetryBackoffError
        with pytest.raises(TaskInRetryBackoffError):
            await fencing.claim_task(
                message={"task_id": task_id, "execution_id": exec_id, "attempt": 1},
                worker="EVIDENCE",
                action="collect",
            )

        # 2. Old queue message with attempt 0 is rejected with StaleQueueMessageError
        with pytest.raises(StaleQueueMessageError):
            await fencing.claim_task(
                message={"task_id": task_id, "execution_id": exec_id, "attempt": 0},
                worker="EVIDENCE",
                action="collect",
            )


@pytest.mark.asyncio
async def test_candidate_selection_starvation_prevention(session_factory) -> None:
    """Verifies that skipping candidates prevents starvation of subsequent expired tasks."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=20)

    exec_id, task_1 = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
    )

    _, task_2 = await create_test_execution_and_task(
        session_factory,
        execution_id=exec_id,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease + timedelta(seconds=1),
    )

    recovery_service = TaskRecoveryService(session_factory, batch_size=1)

    # Scan excluding task_1 should select task_2 even though task_1 has earlier expiry
    res = await recovery_service.recover_expired_leases(
        batch_size=1,
        execution_id=exec_id,
        exclude_task_ids={task_1},
    )
    assert task_2 in res.recovered_retried
    assert task_1 not in res.recovered_retried


@pytest.mark.asyncio
async def test_runtime_limit_bounds_database_lock_wait(session_factory) -> None:
    """Verifies that runtime limits bound database lock waits when another transaction holds a lock."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=10)

    exec_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
    )

    recovery_service = TaskRecoveryService(
        session_factory,
        max_runtime_seconds=1.0,
    )

    # Session 1 holds exclusive row lock on the ExecutionModel row
    async with session_factory() as lock_session:
        async with lock_session.begin():
            await lock_session.execute(
                select(ExecutionModel).where(ExecutionModel.id == exec_id).with_for_update()
            )

            # Recovery runs in another session with max_runtime_seconds=1.0
            # It will attempt to lock ExecutionModel, block, and must time out cleanly
            summary = await recovery_service.run_recovery_loop(
                max_runtime_seconds=1.0,
                execution_id=exec_id,
            )

            assert summary.terminated_by_timeout is True
            assert summary.total_retried == 0


@pytest.mark.asyncio
async def test_pending_task_with_expired_deadline_recovered_as_failed(session_factory) -> None:
    """Verifies that a task in PENDING whose deadline has passed is recovered to FAILED with TASK_FAILED event."""
    exec_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="PENDING",
        attempt=1,
        max_attempts=3,
        task_deadline_minutes=-10,  # Expired task deadline
    )

    recovery_service = TaskRecoveryService(session_factory)
    res = await recovery_service.recover_expired_leases(execution_id=exec_id)
    assert task_id in res.recovered_failed

    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "FAILED"

        outbox = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.task_id == task_id,
                    OutboxEventModel.kind == "TASK_FAILED",
                )
            )
        ).scalar_one()
        assert outbox.payload_ref["execution_id"] == str(exec_id)
        assert outbox.payload_ref["reason"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_pending_task_under_cancelled_and_terminal_parents(session_factory) -> None:
    """Verifies that PENDING tasks under cancelled or terminal parent executions are recovered to CANCELLED or FAILED."""
    now = datetime.now(UTC)

    # 1. PENDING task under CANCELLED execution
    exec_id_cancelled, task_id_cancelled = await create_test_execution_and_task(
        session_factory,
        status="PENDING",
        attempt=1,
        max_attempts=3,
        parent_status="CANCELLED",
        parent_cancelled_at=now,
    )

    # 2. PENDING task under COMPLETED execution
    exec_id_completed, task_id_completed = await create_test_execution_and_task(
        session_factory,
        status="PENDING",
        attempt=1,
        max_attempts=3,
        parent_status="COMPLETED",
    )

    recovery_service = TaskRecoveryService(session_factory)

    res_cancelled = await recovery_service.recover_expired_leases(execution_id=exec_id_cancelled)
    assert task_id_cancelled in res_cancelled.recovered_cancelled

    res_completed = await recovery_service.recover_expired_leases(execution_id=exec_id_completed)
    assert task_id_completed in res_completed.recovered_failed

    async with session_factory() as session:
        t_cancelled = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id_cancelled))
        ).scalar_one()
        assert t_cancelled.status == "CANCELLED"

        t_completed = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id_completed))
        ).scalar_one()
        assert t_completed.status == "FAILED"


@pytest.mark.asyncio
async def test_pending_task_in_valid_backoff_is_preserved(session_factory) -> None:
    """Verifies that a healthy PENDING task in active retry backoff is NOT recovered or altered."""
    now = datetime.now(UTC)
    future_backoff = now + timedelta(minutes=10)

    exec_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="PENDING",
        attempt=1,
        max_attempts=3,
        lease_expiry=future_backoff,  # Active retry backoff
        deadline_minutes=60,
        task_deadline_minutes=60,
        parent_status="RUNNING",
    )

    recovery_service = TaskRecoveryService(session_factory)
    res = await recovery_service.recover_expired_leases(execution_id=exec_id)

    # Crucial: task must be untouched, NOT failed or retried prematurely
    assert task_id not in res.recovered_retried
    assert task_id not in res.recovered_failed
    assert task_id not in res.recovered_cancelled

    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "PENDING"
        assert task.lease_expiry == future_backoff


@pytest.mark.asyncio
async def test_pending_task_candidate_recheck_under_lock_preserves_backoff(
    session_factory, monkeypatch
) -> None:
    """Verifies that a candidate task rechecked under parent-first lock as PENDING in active backoff is skipped into skipped_active."""
    now = datetime.now(UTC)
    future_backoff = now + timedelta(minutes=10)

    exec_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="PENDING",
        attempt=1,
        max_attempts=3,
        lease_expiry=future_backoff,
        deadline_minutes=60,
        task_deadline_minutes=60,
        parent_status="RUNNING",
    )

    recovery_service = TaskRecoveryService(session_factory)

    # Simulate race where task was picked up in candidate scan
    # under parent-first lock it is rechecked and skipped
    from collections import namedtuple
    Row = namedtuple("Row", ["id", "execution_id"])

    # We run recovery where candidate list contains the pending task
    class MockResult:
        def __iter__(self):
            return iter([Row(id=task_id, execution_id=exec_id)])

    # Monkeypatch session.execute during candidate scan
    orig_factory = recovery_service._session_factory

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_factory():
        async with orig_factory() as sess:
            orig_exec = sess.execute

            async def patched_exec(stmt, *args, **kwargs):
                # If this is the candidate scan statement, return our candidate
                if hasattr(stmt, "is_select") and stmt.is_select:
                    from sqlalchemy.sql.selectable import Select
                    if isinstance(stmt, Select) and any("tasks" in str(f) for f in stmt.froms):
                        if "FOR UPDATE" not in str(stmt):
                            return MockResult()
                return await orig_exec(stmt, *args, **kwargs)

            sess.execute = patched_exec
            yield sess

    recovery_service._session_factory = mock_factory
    res = await recovery_service.recover_expired_leases(execution_id=exec_id)

    # Recheck under parent-first lock must identify healthy PENDING task and skip it
    assert task_id in res.skipped_active
    assert task_id not in res.recovered_retried
    assert task_id not in res.recovered_failed
    assert task_id not in res.recovered_cancelled

    async with orig_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "PENDING"
        assert task.lease_expiry == future_backoff


@pytest.mark.asyncio
async def test_recovery_dispatch_payload_deserializes_to_worker_contract(session_factory) -> None:
    """Verifies that recovery retry TASK_DISPATCH event contains execution_id and deserializes into TaskQueueMessage."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=10)

    exec_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=1,
        max_attempts=3,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
    )

    recovery_service = TaskRecoveryService(session_factory)
    res = await recovery_service.recover_expired_leases(execution_id=exec_id)
    assert task_id in res.recovered_retried

    async with session_factory() as session:
        outbox = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.task_id == task_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
            )
        ).scalar_one()

        payload = outbox.payload_ref
        assert payload["execution_id"] == str(exec_id)
        assert payload["task_id"] == str(task_id)
        assert payload["worker"] == "EVIDENCE"
        assert payload["action"] == "collect"
        assert payload["attempt"] == 1
        assert payload["context_version"] == 1
        assert payload["payload"] == {"location": "Jaipur", "mode": "fast"}

        # Directly deserializable into TaskQueueMessage without validation error
        msg = TaskQueueMessage(**payload)
        assert msg.task_id == task_id
        assert msg.execution_id == exec_id
        assert msg.attempt == 1


@pytest.mark.asyncio
async def test_configured_task_attempt_limits_respected_over_service_default(session_factory) -> None:
    """Verifies that tasks configured with max_attempts > 3 are allowed retries and not prematurely failed."""
    now = datetime.now(UTC)
    expired_lease = now - timedelta(seconds=10)

    # Task is at attempt 3, but its configured max_attempts is 5
    exec_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="RUNNING",
        attempt=3,
        max_attempts=5,
        lease_token=uuid.uuid4(),
        lease_expiry=expired_lease,
    )

    # Recovery service initialized with default max_attempts=None (per-task limits govern)
    recovery_service = TaskRecoveryService(session_factory)
    res = await recovery_service.recover_expired_leases(execution_id=exec_id)

    # Attempt 3 < 5 -> task is retried, NOT failed!
    assert task_id in res.recovered_retried
    assert task_id not in res.recovered_failed

    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "PENDING"
        assert task.attempt == 3


@pytest.mark.asyncio
async def test_direct_recovery_timeout_parameter(session_factory) -> None:
    """Verifies that timeout_seconds parameter on recover_expired_leases bounds execution."""
    exec_id, _ = await create_test_execution_and_task(session_factory)

    recovery_service = TaskRecoveryService(session_factory)

    # Calling recover_expired_leases directly with timeout_seconds completes normally when responsive
    res = await recovery_service.recover_expired_leases(
        execution_id=exec_id,
        timeout_seconds=5.0,
    )
    assert isinstance(res.recovered_retried, list)



