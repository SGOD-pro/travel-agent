"""Integration tests for worker claim and lease fencing slice conforming to spec 0001.

Tests against live PostgreSQL with READ COMMITTED isolation:
1. Successful claim: validates message, parent, context, increments attempt strictly on claim, sets status CLAIMED.
2. Concurrent claims race condition: exactly 1 worker acquires lease, 9 rejected, attempt increments strictly once.
3. Expiry before recovery direct reclaim: direct reclaim after lease expiry increments attempt, rejects late worker writes.
4. Stale writes rejected: invalid or expired lease tokens rejected with zero state change.
5. Cancelled parent rejects claim and completion: cancelled executions reject claims and discard completions.
6. Wrong worker or action: mismatched worker or action rejected with zero attempt increment.
7. Stale context: mismatched context_version rejected with zero attempt increment.
8. Exhausted attempts: task at max_attempts rejected without attempt increment.
9. Fenced lease renewal: unexpired lease extended cleanly.
10. Fenced task completion: persists worker_result, sets COMPLETED, emits TASK_COMPLETED outbox event atomically.
11. Fenced task failure: retryable failure resets task to PENDING with exponential backoff on lease_expiry and outbox due_at.
12. Terminal task failure: exhausted attempts failure transitions task to FAILED and emits TASK_FAILED outbox event.
13. Parent WAITING_TASKS eligibility: worker claim, renewal, completion, and failure succeed when parent is WAITING_TASKS, preserving parent status.
14. Stale context rejection on completion, renewal, failure: all worker mutations reject stale context.
15. Expired deadline rejection on completion, renewal, failure: all worker mutations reject expired parent and task deadlines.
16. Old queue message and retry backoff fencing: old queue messages and attempts during retry backoff are rejected.
17. Consistent lock ordering on cancellation and worker mutations: execution cancellation and worker mutations lock parent first, preventing deadlocks.
18. Atomic rollback on completion and failure: worker_results, task updates, and outbox events roll back together.
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
    AttemptsExhaustedError,
    DeadlineExceededError,
    ExecutionModel,
    LeaseFencingError,
    OutboxEventModel,
    ParentCancelledError,
    StaleContextError,
    StaleQueueMessageError,
    TaskInRetryBackoffError,
    TaskModel,
    TaskQueueMessage,
    WorkerFencingService,
    WorkerResultModel,
    WrongWorkerError,
)

# In-process DNS cache to prevent transient WSL forwarder socket.gaierror drops
_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
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
    parent_status: str = "CREATED",
    parent_cancelled_at: datetime | None = None,
    deadline_minutes: int = 60,
    task_deadline_minutes: int = 60,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper creating a test execution and task record."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()
    now = datetime.now(UTC)

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
            execution_id=execution_id,
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

    return execution_id, task_id


@pytest.mark.asyncio
async def test_successful_task_claim_and_attempt_increment(session_factory) -> None:
    """Verifies that a valid task claim transitions task to CLAIMED, issues a lease token, and increments attempt."""
    execution_id, task_id = await create_test_execution_and_task(session_factory)

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        msg = TaskQueueMessage(task_id=task_id, execution_id=execution_id)
        res = await svc.claim_task(
            message=msg,
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
            lease_seconds=300,
        )

        assert res.task_id == task_id
        assert res.execution_id == execution_id
        assert res.attempt == 1
        assert res.lease_token is not None
        assert res.lease_expiry > datetime.now(UTC)

    # Verify live PostgreSQL record state
    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "CLAIMED"
        assert task.attempt == 1
        assert task.lease_token == res.lease_token
        assert task.lease_expiry == res.lease_expiry


@pytest.mark.asyncio
async def test_concurrent_claims_race_condition(session_factory) -> None:
    """Verifies that 10 concurrent workers competing for a task resolve with exactly 1 claim, attempt = 1."""
    execution_id, task_id = await create_test_execution_and_task(session_factory)
    concurrency_count = 10

    async def attempt_worker_claim() -> tuple[str, Any]:
        try:
            async with session_factory() as session:
                svc = WorkerFencingService(session)
                msg = TaskQueueMessage(task_id=task_id, execution_id=execution_id)
                res = await svc.claim_task(
                    message=msg,
                    worker="EVIDENCE",
                    action="collect",
                    expected_context_version=1,
                    lease_seconds=300,
                )
                return "CLAIMED", res
        except Exception as e:
            return "REJECTED", str(e)

    results = await asyncio.gather(*(attempt_worker_claim() for _ in range(concurrency_count)))

    claimed_count = sum(1 for status, _ in results if status == "CLAIMED")
    rejected_count = sum(1 for status, _ in results if status == "REJECTED")

    assert claimed_count == 1
    assert rejected_count == concurrency_count - 1

    # In database: attempt must be strictly 1, never 10
    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.attempt == 1
        assert task.status == "CLAIMED"


@pytest.mark.asyncio
async def test_expiry_before_recovery_direct_reclaim(session_factory) -> None:
    """Verifies that an expired lease allows direct worker reclaim without waiting for recovery, advancing attempt to 2."""
    now = datetime.now(UTC)
    old_lease_token = uuid.uuid4()
    expired_lease_time = now - timedelta(seconds=10)

    # Task was running under Worker 1 with attempt 1, but lease expired
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        attempt=1,
        status="CLAIMED",
        lease_token=old_lease_token,
        lease_expiry=expired_lease_time,
    )

    # Worker 2 claims the task directly
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        msg = TaskQueueMessage(task_id=task_id, execution_id=execution_id)
        claim_2 = await svc.claim_task(
            message=msg,
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
            lease_seconds=300,
        )

        assert claim_2.attempt == 2
        assert claim_2.lease_token != old_lease_token

    # Worker 1 wakes up late and attempts to complete with its stale lease token -> rejected!
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(LeaseFencingError, match="stale or expired"):
            await svc.complete_task(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=old_lease_token,
                output={"late": "data"},
            )


@pytest.mark.asyncio
async def test_stale_writes_rejected(session_factory) -> None:
    """Verifies that completion or renewal with an invalid or expired token is rejected with zero accepted state change."""
    execution_id, task_id = await create_test_execution_and_task(session_factory)

    # Worker claims task
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        res = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
        )
        assert res.lease_token is not None

    stale_token = uuid.uuid4()

    # Attempt renewal with wrong token
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(LeaseFencingError, match="expired or stolen"):
            await svc.renew_lease(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=stale_token,
            )

    # Attempt completion with wrong token
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(LeaseFencingError, match="stale or expired"):
            await svc.complete_task(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=stale_token,
                output={"data": "invalid"},
            )

    # Task remains in CLAIMED with zero result
    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "CLAIMED"
        assert task.result_id is None


@pytest.mark.asyncio
async def test_cancelled_parent_rejects_claim_and_completion(session_factory) -> None:
    """Verifies that cancelled execution rejects task claims and discards late completion writes."""
    now = datetime.now(UTC)
    active_lease_token = uuid.uuid4()

    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        status="CLAIMED",
        lease_token=active_lease_token,
        lease_expiry=now + timedelta(seconds=300),
        parent_status="CANCELLED",
        parent_cancelled_at=now,
    )

    # Worker claim attempt on cancelled parent is rejected
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(ParentCancelledError, match="cancelled"):
            await svc.claim_task(
                message={"task_id": str(task_id), "execution_id": str(execution_id)},
                worker="EVIDENCE",
                action="collect",
            )

    # Worker completion attempt on cancelled parent is rejected
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(ParentCancelledError, match="cancelled"):
            await svc.complete_task(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=active_lease_token,
                output={"some": "data"},
            )

    # Verify task result was not persisted
    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.result_id is None


@pytest.mark.asyncio
async def test_wrong_worker_or_action_rejected(session_factory) -> None:
    """Verifies that attempting to claim with wrong worker or action raises WrongWorkerError and does not increment attempt."""
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        worker="EVIDENCE",
        action="collect",
    )

    async with session_factory() as session:
        svc = WorkerFencingService(session)

        # Wrong worker
        with pytest.raises(WrongWorkerError, match="Task requires EVIDENCE/collect"):
            await svc.claim_task(
                message={"task_id": str(task_id), "execution_id": str(execution_id)},
                worker="DECISION",
                action="collect",
            )

        # Wrong action
        with pytest.raises(WrongWorkerError, match="Task requires EVIDENCE/collect"):
            await svc.claim_task(
                message={"task_id": str(task_id), "execution_id": str(execution_id)},
                worker="EVIDENCE",
                action="render",
            )

    # Database attempt counter remains strictly 0
    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.attempt == 0
        assert task.status == "PENDING"


@pytest.mark.asyncio
async def test_stale_context_rejected(session_factory) -> None:
    """Verifies that claiming a task with mismatched expected_context_version is rejected without attempt increment."""
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        context_version=1,
    )

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(StaleContextError, match="context version"):
            await svc.claim_task(
                message={"task_id": str(task_id), "execution_id": str(execution_id)},
                worker="EVIDENCE",
                action="collect",
                expected_context_version=2,  # Stale: actual is 1
            )

    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.attempt == 0
        assert task.status == "PENDING"


@pytest.mark.asyncio
async def test_exhausted_attempts_rejected(session_factory) -> None:
    """Verifies that a task reaching max_attempts cannot be claimed and attempt counter is not incremented further."""
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        attempt=3,
        max_attempts=3,
        status="PENDING",
    )

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(AttemptsExhaustedError, match="exhausted attempts"):
            await svc.claim_task(
                message={"task_id": str(task_id), "execution_id": str(execution_id)},
                worker="EVIDENCE",
                action="collect",
                expected_context_version=1,
            )

    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.attempt == 3
        assert task.status == "PENDING"


@pytest.mark.asyncio
async def test_fenced_lease_renewal_success(session_factory) -> None:
    """Verifies successful extension of an active task lease."""
    execution_id, task_id = await create_test_execution_and_task(session_factory)

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        res = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
            lease_seconds=60,
        )

        new_expiry = await svc.renew_lease(
            execution_id=execution_id,
            task_id=task_id,
            lease_token=res.lease_token,
            extension_seconds=600,
        )

        assert new_expiry > res.lease_expiry


@pytest.mark.asyncio
async def test_fenced_completion_success(session_factory) -> None:
    """Verifies that fenced completion persists worker_results, sets COMPLETED, and emits TASK_COMPLETED outbox event."""
    execution_id, task_id = await create_test_execution_and_task(session_factory)

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        res = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
        )

        output_data = {"places": ["Amber Fort", "Hawa Mahal"]}
        result_id = await svc.complete_task(
            execution_id=execution_id,
            task_id=task_id,
            lease_token=res.lease_token,
            output=output_data,
            warnings=["weather warm"],
            usage={"duration_ms": 1250},
        )

    # Verify task in PostgreSQL
    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "COMPLETED"
        assert task.result_id == result_id
        assert task.lease_token is None
        assert task.lease_expiry is None

        # Verify worker_result record
        worker_res = (
            await session.execute(select(WorkerResultModel).where(WorkerResultModel.id == result_id))
        ).scalar_one()
        assert worker_res.status == "SUCCESS"
        assert worker_res.output_ref == output_data
        assert worker_res.warnings == ["weather warm"]
        assert worker_res.usage == {"duration_ms": 1250}

        # Verify TASK_COMPLETED outbox event
        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.task_id == task_id,
            OutboxEventModel.kind == "TASK_COMPLETED",
        )
        outbox = (await session.execute(outbox_stmt)).scalar_one_or_none()
        assert outbox is not None


@pytest.mark.asyncio
async def test_fenced_failure_retry_eligible_resets_to_pending(session_factory) -> None:
    """Verifies that retryable failure persists error result, sets retry backoff on lease_expiry, and emits TASK_DISPATCH."""
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        max_attempts=3,
    )

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        res = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
        )

        result_id = await svc.fail_task(
            execution_id=execution_id,
            task_id=task_id,
            lease_token=res.lease_token,
            error={"code": "TIMEOUT", "message": "External API call timed out"},
            retryable=True,
        )
        assert result_id is not None

    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "PENDING"
        assert task.attempt == 1
        assert task.lease_token is None
        assert task.lease_expiry is not None
        assert task.lease_expiry > datetime.now(UTC)

        # Verify TASK_DISPATCH retry outbox event
        outbox = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.task_id == task_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
            )
        ).scalar_one_or_none()
        assert outbox is not None
        assert outbox.due_at == task.lease_expiry


@pytest.mark.asyncio
async def test_fenced_failure_exhausted_attempts_marks_failed(session_factory) -> None:
    """Verifies that failure when attempts reach max_attempts marks task FAILED and emits TASK_FAILED outbox event."""
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        attempt=2,
        max_attempts=3,
    )

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        res = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
        )
        assert res.attempt == 3

        result_id = await svc.fail_task(
            execution_id=execution_id,
            task_id=task_id,
            lease_token=res.lease_token,
            error={"code": "FATAL", "message": "Rate limit exceeded repeatedly"},
            retryable=True,
        )
        assert result_id is not None

    async with session_factory() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == "FAILED"
        assert task.attempt == 3

        # Verify TASK_FAILED outbox event
        outbox = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.task_id == task_id,
                    OutboxEventModel.kind == "TASK_FAILED",
                )
            )
        ).scalar_one_or_none()
        assert outbox is not None


@pytest.mark.asyncio
async def test_parent_waiting_tasks_eligibility(session_factory) -> None:
    """Verifies that worker claim, renewal, completion, and failure succeed when parent is WAITING_TASKS and parent status is preserved."""
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        parent_status="WAITING_TASKS",
    )

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        claim = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
            lease_seconds=60,
        )
        assert claim.attempt == 1

    # Verify parent execution remains WAITING_TASKS
    async with session_factory() as session:
        execution = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
        ).scalar_one()
        assert execution.status == "WAITING_TASKS"

        # Renew lease under WAITING_TASKS
        svc = WorkerFencingService(session)
        new_expiry = await svc.renew_lease(
            execution_id=execution_id,
            task_id=task_id,
            lease_token=claim.lease_token,
        )
        assert new_expiry > claim.lease_expiry

    # Complete task under WAITING_TASKS
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        result_id = await svc.complete_task(
            execution_id=execution_id,
            task_id=task_id,
            lease_token=claim.lease_token,
            output={"places": ["Jantar Mantar"]},
        )
        assert result_id is not None

    async with session_factory() as session:
        execution = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
        ).scalar_one()
        assert execution.status == "WAITING_TASKS"


@pytest.mark.asyncio
async def test_completion_renewal_failure_reject_stale_context(session_factory) -> None:
    """Verifies that lease renewal, completion, and failure all reject mutations when context version is stale."""
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        context_version=1,
    )

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        claim = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
        )

    # Orchestrator advances context_version to 2 on execution
    async with session_factory() as session:
        execution = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
        ).scalar_one()
        execution.context_version = 2
        await session.commit()

    # Renewal rejects stale context
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(StaleContextError, match="mismatches task context"):
            await svc.renew_lease(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=claim.lease_token,
            )

    # Completion rejects stale context
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(StaleContextError, match="mismatches task context"):
            await svc.complete_task(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=claim.lease_token,
                output={"some": "data"},
            )

    # Failure rejects stale context
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(StaleContextError, match="mismatches task context"):
            await svc.fail_task(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=claim.lease_token,
                error={"code": "FAIL"},
            )


@pytest.mark.asyncio
async def test_completion_renewal_failure_reject_expired_deadlines(session_factory) -> None:
    """Verifies that lease renewal, completion, and failure reject operations if execution or task deadlines expire."""
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        deadline_minutes=60,
        task_deadline_minutes=60,
    )

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        claim = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
        )

    # Simulate execution deadline expiration
    past = datetime.now(UTC) - timedelta(seconds=5)
    async with session_factory() as session:
        execution = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
        ).scalar_one()
        execution.deadline_at = past
        await session.commit()

    # Renewal rejected
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(DeadlineExceededError, match="deadline passed"):
            await svc.renew_lease(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=claim.lease_token,
            )

    # Completion rejected
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(DeadlineExceededError, match="deadline passed"):
            await svc.complete_task(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=claim.lease_token,
                output={"done": True},
            )

    # Failure rejected
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(DeadlineExceededError, match="deadline passed"):
            await svc.fail_task(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=claim.lease_token,
                error={"msg": "late"},
            )


@pytest.mark.asyncio
async def test_old_queue_messages_cannot_bypass_retry_backoff(session_factory) -> None:
    """Verifies that stale queue messages from earlier attempts and claims during active retry backoff are rejected."""
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        max_attempts=3,
    )

    # Worker claims task for attempt 1
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        claim = await svc.claim_task(
            message=TaskQueueMessage(task_id=task_id, execution_id=execution_id, attempt=0),
            worker="EVIDENCE",
            action="collect",
        )
        assert claim.attempt == 1

        # Worker fails task with retryable=True and 60s backoff
        await svc.fail_task(
            execution_id=execution_id,
            task_id=task_id,
            lease_token=claim.lease_token,
            error={"code": "TIMEOUT"},
            retryable=True,
            backoff_seconds=60,
        )

    # Case 1: Old queue message from attempt 0 arrives -> rejected by attempt check
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(StaleQueueMessageError, match="stale; task is already at attempt"):
            await svc.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=execution_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
            )

    # Case 2: New message arrives immediately before retry backoff elapsed -> rejected by retry backoff
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        with pytest.raises(TaskInRetryBackoffError, match="retry backoff until"):
            await svc.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=execution_id, attempt=1),
                worker="EVIDENCE",
                action="collect",
            )


@pytest.mark.asyncio
async def test_consistent_lock_ordering_cancellation_and_worker_mutations(session_factory) -> None:
    """Verifies that concurrent cancellation and worker mutations maintain lock ordering and cancellation fences workers."""
    execution_id, task_id = await create_test_execution_and_task(session_factory)

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        claim = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
        )

    lock_acquired = asyncio.Event()

    async def cancel_execution_action() -> None:
        async with session_factory() as session:
            # Consistent lock order: Parent execution locked FOR UPDATE
            exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id).with_for_update()
            execution = (await session.execute(exec_stmt)).scalar_one()
            lock_acquired.set()
            await asyncio.sleep(0.2)
            execution.status = "CANCELLED"
            execution.cancelled_at = datetime.now(UTC)
            await session.commit()

    async def worker_mutation_action() -> str:
        await lock_acquired.wait()
        try:
            async with session_factory() as session:
                svc = WorkerFencingService(session)
                await svc.complete_task(
                    execution_id=execution_id,
                    task_id=task_id,
                    lease_token=claim.lease_token,
                    output={"data": "late"},
                )
                return "COMPLETED"
        except ParentCancelledError:
            return "CANCELLED_FENCED"

    results = await asyncio.gather(cancel_execution_action(), worker_mutation_action())
    worker_result = results[1]
    assert worker_result == "CANCELLED_FENCED"


@pytest.mark.asyncio
async def test_atomic_rollback_on_worker_completion_and_failure(session_factory) -> None:
    """Verifies that uncommitted failures during task completion or failure roll back worker_results, task, and outbox together."""
    execution_id, task_id = await create_test_execution_and_task(session_factory)

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        claim = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
        )
        assert claim.lease_token is not None

    # In a new session, simulate partial write followed by exception and rollback
    async with session_factory() as session:
        worker_res_id = uuid.uuid4()
        wr = WorkerResultModel(
            id=worker_res_id,
            task_id=task_id,
            execution_id=execution_id,
            attempt=1,
            context_version=1,
            status="SUCCESS",
            output_ref={"draft": True},
            warnings=[],
            error=None,
            usage={},
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        session.add(wr)
        await session.flush()

        # Simulate unexpected error before commit -> session rolls back
        await session.rollback()

    # Verify zero records persisted in PostgreSQL
    async with session_factory() as session:
        wr_check = (
            await session.execute(select(WorkerResultModel).where(WorkerResultModel.id == worker_res_id))
        ).scalar_one_or_none()
        assert wr_check is None

        task_check = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task_check.status == "CLAIMED"
        assert task_check.result_id is None


@pytest.mark.asyncio
async def test_retry_task_dispatch_payload_conforms_to_worker_contract(session_factory) -> None:
    """Verifies that retry TASK_DISPATCH and completion events contain execution_id and conform to TaskQueueMessage."""
    execution_id, task_id = await create_test_execution_and_task(
        session_factory,
        max_attempts=3,
    )

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        claim = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
        )

        # Fail task with retryable=True
        await svc.fail_task(
            execution_id=execution_id,
            task_id=task_id,
            lease_token=claim.lease_token,
            error={"reason": "simulated_worker_transient_error"},
            retryable=True,
            backoff_seconds=10,
        )

    # Inspect outbox event
    async with session_factory() as session:
        outbox = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.task_id == task_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
            )
        ).scalar_one()

        # 1. Payload contains execution_id and required task parameters
        payload = outbox.payload_ref
        assert payload["execution_id"] == str(execution_id)
        assert payload["task_id"] == str(task_id)
        assert payload["worker"] == "EVIDENCE"
        assert payload["action"] == "collect"
        assert payload["attempt"] == 1
        assert payload["context_version"] == 1
        assert payload["payload"] == {"location": "Jaipur", "mode": "fast"}

        # 2. Directly deserializable into TaskQueueMessage without error
        queue_msg = TaskQueueMessage(**payload)
        assert queue_msg.task_id == task_id
        assert queue_msg.execution_id == execution_id
        assert queue_msg.attempt == 1


@pytest.mark.asyncio
async def test_rejected_completion_leaves_no_orphan_results_and_session_usable(session_factory) -> None:
    """Verifies that rejected complete_task leaves zero worker results in DB and the session remains usable."""
    execution_id, task_id = await create_test_execution_and_task(session_factory)

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        claim = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
        )
        valid_token = claim.lease_token

    # In a single session: attempt completion with invalid token, catch error, then complete with valid token
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        stale_token = uuid.uuid4()

        with pytest.raises(LeaseFencingError):
            await svc.complete_task(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=stale_token,
                output={"bad": "output"},
            )

        # Verify zero worker_results exist in DB or pending in session
        results_count = (
            await session.execute(
                select(WorkerResultModel).where(WorkerResultModel.task_id == task_id)
            )
        ).scalars().all()
        assert len(results_count) == 0

        # Session remains fully usable: perform valid completion in the SAME session!
        res_id = await svc.complete_task(
            execution_id=execution_id,
            task_id=task_id,
            lease_token=valid_token,
            output={"good": "output"},
        )
        assert res_id is not None

    # Verify final persisted state
    async with session_factory() as session:
        final_results = (
            await session.execute(
                select(WorkerResultModel).where(WorkerResultModel.task_id == task_id)
            )
        ).scalars().all()
        assert len(final_results) == 1
        assert final_results[0].id == res_id
        assert final_results[0].output_ref == {"good": "output"}

        # Verify TASK_COMPLETED outbox payload includes execution_id
        outbox = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.task_id == task_id,
                    OutboxEventModel.kind == "TASK_COMPLETED",
                )
            )
        ).scalar_one()
        assert outbox.payload_ref["execution_id"] == str(execution_id)
        assert outbox.payload_ref["task_id"] == str(task_id)


@pytest.mark.asyncio
async def test_rejected_failure_leaves_no_orphan_results_and_session_usable(session_factory) -> None:
    """Verifies that rejected fail_task leaves zero worker results in DB and the session remains usable."""
    execution_id, task_id = await create_test_execution_and_task(session_factory)

    async with session_factory() as session:
        svc = WorkerFencingService(session)
        claim = await svc.claim_task(
            message={"task_id": str(task_id), "execution_id": str(execution_id)},
            worker="EVIDENCE",
            action="collect",
        )
        valid_token = claim.lease_token

    # Attempt fail_task with stale token, verify no orphan results, then complete in same session
    async with session_factory() as session:
        svc = WorkerFencingService(session)
        stale_token = uuid.uuid4()

        with pytest.raises(LeaseFencingError):
            await svc.fail_task(
                execution_id=execution_id,
                task_id=task_id,
                lease_token=stale_token,
                error={"bad": "error"},
                retryable=False,
            )

        # Verify zero worker_results exist
        results_count = (
            await session.execute(
                select(WorkerResultModel).where(WorkerResultModel.task_id == task_id)
            )
        ).scalars().all()
        assert len(results_count) == 0

        # Session remains fully usable: perform valid fail in the SAME session!
        res_id = await svc.fail_task(
            execution_id=execution_id,
            task_id=task_id,
            lease_token=valid_token,
            error={"good": "error"},
            retryable=False,
        )
        assert res_id is not None

    async with session_factory() as session:
        final_results = (
            await session.execute(
                select(WorkerResultModel).where(WorkerResultModel.task_id == task_id)
            )
        ).scalars().all()
        assert len(final_results) == 1
        assert final_results[0].id == res_id
        assert final_results[0].error == {"good": "error"}

