"""Integration tests for durable evidence budget reservation crash recovery.

Conforms to spec 0001, spec 0002 (AC-14), and backend audit finding 2:
1. Makes reservations identifiable by execution, task, attempt, and lease token.
2. Recovers abandoned reservations safely after lease expiry or worker crash.
3. Preserves uncertain provider spend: does not assume interrupted calls were free,
   preventing retries from exceeding configured budget limits.
4. Prevents stale workers from reconciling or spending after lease loss.
5. Guarantees idempotent recovery and repeated reconciliation.
6. Verifies serialized row locking across concurrent workers.
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
from functions.evidence_collector.service import EvidenceCollectorService
from functions.orchestrator.execution import (
    ExecutionModel,
    TaskModel,
    TaskQueueMessage,
    TaskRecoveryService,
    WorkerFencingService,
)
from functions.orchestrator.execution.budget import BudgetReservationStatus

# In-process DNS cache to prevent transient WSL forwarder socket drops
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
    connect_args: dict[str, Any] = {}
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


async def _create_test_execution_and_task(
    session_factory: Any,
    *,
    execution_id: uuid.UUID | None = None,
    budget: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper creating execution and task records on isolated PostgreSQL."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    task_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as session:
        if execution_id is None:
            execution_id = uuid.uuid4()
            trip = TripModel(
                id=trip_id,
                owner_id=owner_id,
                current_version=1,
                state="draft",
                created_at=now,
                updated_at=now,
            )
            session.add(trip)

            initial_budget = budget or {
                "remaining_tool_calls": 5,
                "remaining_cost_minor": 500,
                "currency": "INR",
                "reserved_tool_calls": 0,
                "reserved_cost_minor": 0,
                "spent_tool_calls": 0,
                "spent_cost_minor": 0,
            }

            execution = ExecutionModel(
                id=execution_id,
                owner_id=owner_id,
                trip_id=trip_id,
                workflow="TRAVEL",
                status="RUNNING",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                budget=initial_budget,
                cancelled_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)

        task = TaskModel(
            id=task_id,
            execution_id=execution_id,
            parent_task_id=None,
            worker="EVIDENCE",
            action="collect",
            payload_ref=payload or {},
            input_hash="hash_input",
            idempotency_key=f"idem-{task_id}",
            attempt=0,
            max_attempts=max_attempts,
            context_version=1,
            status="PENDING",
            deadline_at=now + timedelta(hours=1),
            result_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.commit()

    return execution_id, task_id


@pytest.mark.asyncio
class TestBudgetCrashRecovery:
    """Verifies evidence budget reservation crash recovery and lease fencing."""

    async def test_reservations_identifiable_by_execution_task_attempt_and_lease(
        self, session_factory: Any
    ) -> None:
        """1. Reservations are identifiable by execution, task, attempt, and lease token."""
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory,
            budget={"remaining_tool_calls": 4, "remaining_cost_minor": 200},
        )

        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=60,
            )
            collector = EvidenceCollectorService(session)
            res_id = await collector.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert res_id is not None

        assert res_id is not None

        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            cur_exec = res.scalar_one()
            b = cur_exec.budget
            assert b["reserved_tool_calls"] == 1
            assert b["reserved_cost_minor"] == 50
            assert "reservations" in b
            assert res_id in b["reservations"]
            entry = b["reservations"][res_id]
            assert entry["reservation_id"] == res_id
            assert entry["task_id"] == str(task_id)
            assert entry["attempt"] == claim.attempt
            assert entry["lease_token"] == str(claim.lease_token)
            assert entry["cost_minor"] == 50
            assert entry["status"] == BudgetReservationStatus.ACTIVE

    async def test_abandoned_reservation_recovery_after_worker_crash_and_lease_expiry(
        self, session_factory: Any
    ) -> None:
        """2 & 3. Recovers abandoned reservations after lease expiry, preserving uncertain spend."""
        initial_budget = {
            "remaining_tool_calls": 2,
            "remaining_cost_minor": 100,
            "currency": "INR",
            "reserved_tool_calls": 0,
            "reserved_cost_minor": 0,
            "spent_tool_calls": 0,
            "spent_cost_minor": 0,
        }
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory, budget=initial_budget
        )

        # Worker claims with 1-second short lease and commits reservation
        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=1,
            )
            collector = EvidenceCollectorService(session)
            res_id = await collector.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert res_id is not None

        # Worker process terminates without executing finally block
        await asyncio.sleep(1.8)

        # Scheduled task recovery sweeps expired leases
        recovery_svc = TaskRecoveryService(session_factory)
        rec_result = await recovery_svc.recover_expired_leases(execution_id=exec_id)
        assert task_id in rec_result.recovered_retried

        # Verify budget after recovery
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            cur_exec = res.scalar_one()
            b = cur_exec.budget

            # Reserved counter has been safely released
            assert b["reserved_tool_calls"] == 0
            assert b["reserved_cost_minor"] == 0

            # Uncertain spend is preserved (interrupted call not assumed free)
            assert b["uncertain_tool_calls"] == 1
            assert b["uncertain_cost_minor"] == 50
            assert b["spent_tool_calls"] == 1
            assert b["spent_cost_minor"] == 50

            # Remaining balance reflects consumed uncertain spend
            assert b["remaining_tool_calls"] == 1
            assert b["remaining_cost_minor"] == 50

            # Reservation record preserves full auditability
            entry = b["reservations"][res_id]
            assert entry["status"] == BudgetReservationStatus.ABANDONED_UNCERTAIN
            assert entry["settled_as"] == "UNCERTAIN"
            assert entry["recovery_reason"] == "task_lease_expired_retry"
            assert entry["recovered_at"] is not None

    async def test_retries_cannot_exceed_configured_budget_after_crash(
        self, session_factory: Any
    ) -> None:
        """3. Retries cannot exceed configured budget when uncertain spend consumes limit."""
        # Initial budget allows only 1 tool call (50 minor units)
        initial_budget = {
            "remaining_tool_calls": 1,
            "remaining_cost_minor": 50,
            "currency": "INR",
            "reserved_tool_calls": 0,
            "reserved_cost_minor": 0,
            "spent_tool_calls": 0,
            "spent_cost_minor": 0,
        }
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory, budget=initial_budget
        )

        # Attempt 1: Claims, reserves 50 minor units, crashes
        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim1 = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=1,
            )
            collector1 = EvidenceCollectorService(session)
            await collector1.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim1.attempt,
                lease_token=claim1.lease_token,
            )

        # Worker 1 terminates; lease expires
        await asyncio.sleep(1.8)

        # Task recovery runs
        recovery_svc = TaskRecoveryService(session_factory)
        await recovery_svc.recover_expired_leases(execution_id=exec_id)

        # Clear retry backoff to allow immediate test claim
        async with session_factory() as session:
            t_res = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
            t_model = t_res.scalar_one()
            t_model.lease_expiry = None
            await session.commit()

        # Attempt 2: Task is retried and claimed by Worker 2
        async with session_factory() as session:
            fencing2 = WorkerFencingService(session)
            claim2 = await fencing2.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=1),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=60,
            )
            collector2 = EvidenceCollectorService(session)
            # Worker 2 attempts to reserve budget: MUST FAIL because budget is exhausted!
            retry_res_id = await collector2.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim2.attempt,
                lease_token=claim2.lease_token,
            )
            assert retry_res_id is None, "Retry must not be allowed to exceed configured budget"

        # Verify budget remains capped at 50 minor units spent
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            cur_exec = res.scalar_one()
            b = cur_exec.budget
            assert b["spent_cost_minor"] == 50
            assert b["spent_tool_calls"] == 1
            assert b["remaining_cost_minor"] == 0
            assert b["remaining_tool_calls"] == 0

    async def test_stale_worker_cannot_reconcile_or_spend_after_lease_loss(
        self, session_factory: Any
    ) -> None:
        """4. Stale workers cannot reconcile or spend after lease loss."""
        initial_budget = {
            "remaining_tool_calls": 3,
            "remaining_cost_minor": 150,
            "currency": "INR",
            "reserved_tool_calls": 0,
            "reserved_cost_minor": 0,
            "spent_tool_calls": 0,
            "spent_cost_minor": 0,
        }
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory, budget=initial_budget
        )

        # Attempt 1: Worker 1 claims with 1-second lease and reserves
        async with session_factory() as session:
            fencing1 = WorkerFencingService(session)
            claim1 = await fencing1.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=1,
            )
            collector1 = EvidenceCollectorService(session)
            res1_id = await collector1.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim1.attempt,
                lease_token=claim1.lease_token,
            )

        # Worker 1 pauses; lease expires
        await asyncio.sleep(1.8)

        # Recovery reclaims task and transitions reservation to ABANDONED_UNCERTAIN
        recovery_svc = TaskRecoveryService(session_factory)
        await recovery_svc.recover_expired_leases(execution_id=exec_id)

        # Clear retry backoff to allow immediate test claim
        async with session_factory() as session:
            t_res = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
            t_model = t_res.scalar_one()
            t_model.lease_expiry = None
            await session.commit()

        # Worker 2 claims the task under attempt 2
        async with session_factory() as session:
            fencing2 = WorkerFencingService(session)
            await fencing2.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=1),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=60,
            )

        # Worker 1 now wakes up and tries to reconcile or reserve with stale lease token 1
        async with session_factory() as session:
            collector_stale = EvidenceCollectorService(session)

            # a) Stale worker tries to reconcile old reservation: REJECTED
            reconcile_res = await collector_stale.reconcile_budget(
                exec_id,
                50,
                success=True,
                reservation_id=res1_id,
                task_id=task_id,
                attempt=claim1.attempt,
                lease_token=claim1.lease_token,
            )
            assert reconcile_res is False, "Stale worker reconciliation must be rejected"

            # b) Stale worker tries to reserve a new tool call with stale lease token: REJECTED
            new_res_id = await collector_stale.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim1.attempt,
                lease_token=claim1.lease_token,
            )
            assert new_res_id is None, "Stale worker new reservation must be rejected"

        # Verify budget was not mutated by stale worker
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            cur_exec = res.scalar_one()
            b = cur_exec.budget
            assert b["reservations"][res1_id]["status"] == BudgetReservationStatus.ABANDONED_UNCERTAIN
            assert b["spent_cost_minor"] == 50
            assert b["reserved_cost_minor"] == 0

    async def test_duplicate_recovery_is_idempotent(
        self, session_factory: Any
    ) -> None:
        """5. Duplicate recovery runs are strictly idempotent."""
        initial_budget = {
            "remaining_tool_calls": 2,
            "remaining_cost_minor": 100,
            "currency": "INR",
            "reserved_tool_calls": 0,
            "reserved_cost_minor": 0,
            "spent_tool_calls": 0,
            "spent_cost_minor": 0,
        }
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory, budget=initial_budget
        )

        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=1,
            )
            collector = EvidenceCollectorService(session)
            res_id = await collector.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert res_id is not None

        await asyncio.sleep(1.8)

        # First recovery run
        recovery_svc = TaskRecoveryService(session_factory)
        rec1 = await recovery_svc.recover_expired_leases(execution_id=exec_id)
        assert task_id in rec1.recovered_retried

        # Snapshot budget after first recovery
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            budget_after_rec1 = dict(res.scalar_one().budget)

        # Second recovery run (e.g. manual or subsequent cron sweep)
        rec2 = await recovery_svc.recover_abandoned_budget_reservations(exec_id, task_id)
        assert len(rec2) == 0, "Second recovery must find zero active reservations"

        # Verify budget is identical
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            budget_after_rec2 = dict(res.scalar_one().budget)

        assert budget_after_rec1 == budget_after_rec2

    async def test_repeated_reconciliation_is_idempotent(
        self, session_factory: Any
    ) -> None:
        """5. Repeated reconciliation calls for the same reservation are idempotent."""
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory,
            budget={"remaining_tool_calls": 3, "remaining_cost_minor": 150},
        )

        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=60,
            )
            collector = EvidenceCollectorService(session)
            res_id = await collector.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert res_id is not None

            # First reconciliation
            ok1 = await collector.reconcile_budget(
                exec_id,
                50,
                success=True,
                reservation_id=res_id,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert ok1 is True

        # Capture budget
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            budget_1 = dict(res.scalar_one().budget)
            assert budget_1["spent_tool_calls"] == 1
            assert budget_1["spent_cost_minor"] == 50
            assert budget_1["reserved_tool_calls"] == 0

        # Second reconciliation (duplicate delivery / network retry)
        async with session_factory() as session:
            collector2 = EvidenceCollectorService(session)
            ok2 = await collector2.reconcile_budget(
                exec_id,
                50,
                success=True,
                reservation_id=res_id,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert ok2 is True

        # Budget must not have double-counted
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            budget_2 = dict(res.scalar_one().budget)
            assert budget_1 == budget_2

    async def test_concurrent_workers_budget_reservation_serialized_safely(
        self, session_factory: Any
    ) -> None:
        """6. Concurrent workers contending for budget are safely serialized via row locks."""
        # Total budget for 2 calls (100 minor units)
        exec_id, task_id1 = await _create_test_execution_and_task(
            session_factory,
            budget={"remaining_tool_calls": 2, "remaining_cost_minor": 100},
        )
        # Create second and third tasks under same execution
        now = datetime.now(UTC)
        task_id2 = uuid.uuid4()
        task_id3 = uuid.uuid4()
        async with session_factory() as session:
            t2 = TaskModel(
                id=task_id2,
                execution_id=exec_id,
                parent_task_id=None,
                worker="EVIDENCE",
                action="collect",
                payload_ref={},
                input_hash="hash2",
                idempotency_key=f"idem-{task_id2}",
                attempt=0,
                max_attempts=3,
                context_version=1,
                status="PENDING",
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
            t3 = TaskModel(
                id=task_id3,
                execution_id=exec_id,
                parent_task_id=None,
                worker="EVIDENCE",
                action="collect",
                payload_ref={},
                input_hash="hash3",
                idempotency_key=f"idem-{task_id3}",
                attempt=0,
                max_attempts=3,
                context_version=1,
                status="PENDING",
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
            session.add(t2)
            session.add(t3)
            await session.commit()

        # Claim all 3 tasks
        claims: dict[uuid.UUID, Any] = {}
        for tid in [task_id1, task_id2, task_id3]:
            async with session_factory() as session:
                f = WorkerFencingService(session)
                c = await f.claim_task(
                    message=TaskQueueMessage(task_id=tid, execution_id=exec_id, attempt=0),
                    worker="EVIDENCE",
                    action="collect",
                    lease_seconds=60,
                )
                claims[tid] = c

        # 3 workers simultaneously attempt to reserve 50 minor units each
        async def _attempt_reserve(tid: uuid.UUID) -> str | None:
            async with session_factory() as session:
                collector = EvidenceCollectorService(session)
                return await collector.reserve_budget(
                    exec_id,
                    50,
                    task_id=tid,
                    attempt=claims[tid].attempt,
                    lease_token=claims[tid].lease_token,
                )

        results = await asyncio.gather(
            _attempt_reserve(task_id1),
            _attempt_reserve(task_id2),
            _attempt_reserve(task_id3),
        )

        # Exactly 2 must succeed, 1 must fail due to budget limit
        successes = [r for r in results if r is not None]
        failures = [r for r in results if r is None]
        assert len(successes) == 2
        assert len(failures) == 1

        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["reserved_tool_calls"] == 2
            assert b["reserved_cost_minor"] == 100

    async def test_parent_cancelled_recovers_budget_reservations(
        self, session_factory: Any
    ) -> None:
        """7. Cancellation of parent execution triggers recovery of active reservations."""
        initial_budget = {
            "remaining_tool_calls": 2,
            "remaining_cost_minor": 100,
            "currency": "INR",
            "reserved_tool_calls": 0,
            "reserved_cost_minor": 0,
            "spent_tool_calls": 0,
            "spent_cost_minor": 0,
        }
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory, budget=initial_budget
        )

        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=1,
            )
            collector = EvidenceCollectorService(session)
            res_id = await collector.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert res_id is not None

        # Parent execution cancelled and worker lease expires
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            exec_model = res.scalar_one()
            exec_model.status = "CANCELLED"
            exec_model.cancelled_at = datetime.now(UTC)
            await session.commit()

        await asyncio.sleep(1.8)

        # Recovery runs
        recovery_svc = TaskRecoveryService(session_factory)
        rec_res = await recovery_svc.recover_expired_leases(execution_id=exec_id)
        assert task_id in rec_res.recovered_cancelled

        # Verify budget
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["reserved_cost_minor"] == 0
            assert b["reservations"][res_id]["status"] == BudgetReservationStatus.ABANDONED_UNCERTAIN
            assert b["reservations"][res_id]["recovery_reason"] == "parent_execution_cancelled"

    async def test_lock_wait_lease_expiry_rejects_reservation(
        self, session_factory: Any
    ) -> None:
        """Lock wait re-evaluation: an expired lease must not reserve budget after waiting for row lock."""
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory,
            budget={"remaining_tool_calls": 3, "remaining_cost_minor": 150},
        )

        # Worker claims task with a short lease of 1 second
        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=1,
            )

        # We simulate a lock wait: another session holds ExecutionModel lock for 1.5 seconds.
        # Worker attempts to reserve budget while the lock is held.
        # By the time worker acquires lock, the 1-second lease has expired.
        async def hold_lock():
            async with session_factory() as s_lock:
                async with s_lock.begin():
                    stmt = select(ExecutionModel).where(ExecutionModel.id == exec_id).with_for_update()
                    await s_lock.execute(stmt)
                    await asyncio.sleep(1.5)

        async def worker_reserve():
            await asyncio.sleep(0.2)  # ensure hold_lock acquires first
            async with session_factory() as s_worker:
                collector = EvidenceCollectorService(s_worker)
                return await collector.reserve_budget(
                    exec_id,
                    50,
                    task_id=task_id,
                    attempt=claim.attempt,
                    lease_token=claim.lease_token,
                )

        _, res_id = await asyncio.gather(hold_lock(), worker_reserve())
        assert res_id is None, "Reservation must be rejected after lease expires during lock wait"

        # Verify budget was not reserved
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b.get("reserved_cost_minor", 0) == 0
            assert b.get("reserved_tool_calls", 0) == 0

    async def test_lock_wait_lease_expiry_rejects_reconciliation(
        self, session_factory: Any
    ) -> None:
        """Lock wait re-evaluation: an expired lease must not reconcile budget after waiting for row lock."""
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory,
            budget={"remaining_tool_calls": 3, "remaining_cost_minor": 150},
        )

        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=1,
            )
            collector = EvidenceCollectorService(session)
            res_id = await collector.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert res_id is not None

        # Lock ExecutionModel for 1.5s while worker attempts to reconcile
        async def hold_lock():
            async with session_factory() as s_lock:
                async with s_lock.begin():
                    stmt = select(ExecutionModel).where(ExecutionModel.id == exec_id).with_for_update()
                    await s_lock.execute(stmt)
                    await asyncio.sleep(1.5)

        async def worker_reconcile():
            await asyncio.sleep(0.2)
            async with session_factory() as s_worker:
                collector = EvidenceCollectorService(s_worker)
                return await collector.reconcile_budget(
                    exec_id,
                    50,
                    success=True,
                    reservation_id=res_id,
                    task_id=task_id,
                    attempt=claim.attempt,
                    lease_token=claim.lease_token,
                )

        _, ok_reconcile = await asyncio.gather(hold_lock(), worker_reconcile())
        assert ok_reconcile is False, "Reconciliation must be rejected when lease expires during lock wait"

        # Verify budget was not spent by stale worker
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["spent_cost_minor"] == 0
            assert b["spent_tool_calls"] == 0

    async def test_mismatched_or_missing_reservation_leaves_counters_unmodified(
        self, session_factory: Any
    ) -> None:
        """Mismatched or missing reservations must not change budget counters."""
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory,
            budget={"remaining_tool_calls": 3, "remaining_cost_minor": 150},
        )

        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=60,
            )
            collector = EvidenceCollectorService(session)
            res_id = await collector.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert res_id is not None

        # Verify reservation active
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["reserved_cost_minor"] == 50
            assert b["reserved_tool_calls"] == 1

        async with session_factory() as session:
            collector = EvidenceCollectorService(session)

            # Case A: Missing reservation ID
            ok_missing = await collector.reconcile_budget(
                exec_id,
                50,
                success=True,
                reservation_id="nonexistent-res-id",
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert ok_missing is False

            # Case B: Mismatched task ID
            other_task_id = uuid.uuid4()
            ok_task_mismatch = await collector.reconcile_budget(
                exec_id,
                50,
                success=True,
                reservation_id=res_id,
                task_id=other_task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert ok_task_mismatch is False

            # Case C: Mismatched attempt
            ok_attempt_mismatch = await collector.reconcile_budget(
                exec_id,
                50,
                success=True,
                reservation_id=res_id,
                task_id=task_id,
                attempt=99,
                lease_token=claim.lease_token,
            )
            assert ok_attempt_mismatch is False

            # Case D: Mismatched lease token
            other_token = uuid.uuid4()
            ok_token_mismatch = await collector.reconcile_budget(
                exec_id,
                50,
                success=True,
                reservation_id=res_id,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=other_token,
            )
            assert ok_token_mismatch is False

            # Case E: Mismatched reserved cost (reserving 50 but reconciling 100)
            ok_cost_mismatch = await collector.reconcile_budget(
                exec_id,
                100,
                success=True,
                reservation_id=res_id,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert ok_cost_mismatch is False

        # Counters MUST NOT have changed through all mismatched attempts
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["reserved_cost_minor"] == 50
            assert b["reserved_tool_calls"] == 1
            assert b["spent_cost_minor"] == 0
            assert b["spent_tool_calls"] == 0

        # Finally, valid reconciliation with matching parameters succeeds
        async with session_factory() as session:
            collector = EvidenceCollectorService(session)
            ok_valid = await collector.reconcile_budget(
                exec_id,
                50,
                success=True,
                reservation_id=res_id,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert ok_valid is True

        # Now counters are updated
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["reserved_cost_minor"] == 0
            assert b["reserved_tool_calls"] == 0
            assert b["spent_cost_minor"] == 50
            assert b["spent_tool_calls"] == 1

    async def test_recovery_racing_active_worker_preserves_live_reservation(
        self, session_factory: Any
    ) -> None:
        """A manual or concurrent recovery sweep must verify abandonment and not recover an active worker's reservation."""
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory,
            budget={"remaining_tool_calls": 3, "remaining_cost_minor": 150},
        )

        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=60,
            )
            collector = EvidenceCollectorService(session)
            res_id = await collector.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert res_id is not None

        # Manual recovery sweep while worker is live and active
        recovery_svc = TaskRecoveryService(session_factory)
        recovered = await recovery_svc.recover_abandoned_budget_reservations(exec_id, task_id)
        assert len(recovered) == 0, "Recovery must not steal active reservation from live task"

        # Verify budget still has active reservation
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["reservations"][res_id]["status"] == BudgetReservationStatus.ACTIVE
            assert b["reserved_cost_minor"] == 50
            assert b["uncertain_cost_minor"] == 0

        # Worker finishes and reconciles successfully
        async with session_factory() as session:
            collector = EvidenceCollectorService(session)
            ok = await collector.reconcile_budget(
                exec_id,
                50,
                success=True,
                reservation_id=res_id,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert ok is True

        # Counters show clean reconciliation
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["reservations"][res_id]["status"] == BudgetReservationStatus.RECONCILED
            assert b["spent_cost_minor"] == 50
            assert b["uncertain_cost_minor"] == 0
            assert b["reserved_cost_minor"] == 0

    async def test_timeout_after_dispatch_preserves_uncertain_spend(
        self, session_factory: Any
    ) -> None:
        """When an external call times out or is cancelled after dispatch, preserve uncertain spend instead of refunding."""
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory,
            budget={"remaining_tool_calls": 3, "remaining_cost_minor": 150},
        )

        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=60,
            )
            collector = EvidenceCollectorService(session)
            res_id = await collector.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
            )
            assert res_id is not None

            # Dispatched call timed out
            ok = await collector.reconcile_budget(
                exec_id,
                50,
                success=False,
                reservation_id=res_id,
                task_id=task_id,
                attempt=claim.attempt,
                lease_token=claim.lease_token,
                dispatched=True,
                outcome="TIMEOUT",
            )
            assert ok is True

        # Verify uncertain spend is PRESERVED, not refunded!
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["reservations"][res_id]["status"] == BudgetReservationStatus.RECONCILED
            assert b["reservations"][res_id]["settled_as"] == "TIMEOUT"
            assert b["reserved_cost_minor"] == 0
            assert b["spent_cost_minor"] == 50
            assert b["uncertain_cost_minor"] == 50
            assert b["remaining_cost_minor"] == 100

    async def test_budget_ceilings_enforced_across_timeouts_and_crashes(
        self, session_factory: Any
    ) -> None:
        """Budget ceilings are strictly preserved across timeouts and crashes, preventing retries from exceeding budget."""
        # Initial budget allows exactly 2 calls of 50 minor
        exec_id, task_id = await _create_test_execution_and_task(
            session_factory,
            budget={"remaining_tool_calls": 2, "remaining_cost_minor": 100},
        )

        # Call 1: Reserves 50 minor, dispatches, and times out
        async with session_factory() as session:
            fencing = WorkerFencingService(session)
            claim1 = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=0),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=60,
            )
            collector1 = EvidenceCollectorService(session)
            res1 = await collector1.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim1.attempt,
                lease_token=claim1.lease_token,
            )
            assert res1 is not None
            # Reconcile timeout
            await collector1.reconcile_budget(
                exec_id,
                50,
                success=False,
                reservation_id=res1,
                task_id=task_id,
                attempt=claim1.attempt,
                lease_token=claim1.lease_token,
                dispatched=True,
                outcome="TIMEOUT",
            )

        # After Call 1: remaining is 50
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["remaining_cost_minor"] == 50
            assert b["spent_cost_minor"] == 50
            assert b["uncertain_cost_minor"] == 50

        # Call 2: Reserves the remaining 50 minor with a short lease, then crashes
        async with session_factory() as session:
            collector2 = EvidenceCollectorService(session)
            res2 = await collector2.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim1.attempt,
                lease_token=claim1.lease_token,
            )
            assert res2 is not None

        # Force lease expiry
        async with session_factory() as session:
            task = await session.get(TaskModel, task_id)
            assert task is not None
            task.lease_expiry = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        # Recovery sweeps expired lease and recovers reservation
        recovery_svc = TaskRecoveryService(session_factory)
        rec = await recovery_svc.recover_expired_leases(execution_id=exec_id)
        assert task_id in rec.recovered_retried

        # Remaining budget must now be 0
        async with session_factory() as session:
            res = await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
            b = res.scalar_one().budget
            assert b["remaining_cost_minor"] == 0
            assert b["spent_cost_minor"] == 100
            assert b["uncertain_cost_minor"] == 100

        # Attempt 3: Attempts to claim and reserve budget
        # Clear backoff to test budget reservation directly
        async with session_factory() as session:
            task = await session.get(TaskModel, task_id)
            assert task is not None
            task.lease_expiry = None
            await session.commit()

            fencing = WorkerFencingService(session)
            claim3 = await fencing.claim_task(
                message=TaskQueueMessage(task_id=task_id, execution_id=exec_id, attempt=1),
                worker="EVIDENCE",
                action="collect",
                lease_seconds=60,
            )
            collector3 = EvidenceCollectorService(session)
            res3 = await collector3.reserve_budget(
                exec_id,
                50,
                task_id=task_id,
                attempt=claim3.attempt,
                lease_token=claim3.lease_token,
            )
            assert res3 is None, "Budget ceiling must block further reservations when remaining is 0"
