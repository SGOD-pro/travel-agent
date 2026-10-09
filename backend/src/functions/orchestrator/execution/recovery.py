"""Scheduled task recovery service for orchestrator execution engine conforming to spec 0001."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import (
    ExecutionModel,
    OutboxEventModel,
    TaskModel,
)

DEFAULT_RECOVERY_BATCH_SIZE: int = 10
DEFAULT_MAX_RECOVERY_BATCH_SIZE: int = 100
DEFAULT_MAX_RECOVERY_RUNTIME_SECONDS: float = 25.0
DEFAULT_RECOVERY_BASE_BACKOFF_SECONDS: int = 2
DEFAULT_RECOVERY_MAX_BACKOFF_SECONDS: int = 60
DEFAULT_MAX_ATTEMPTS: int = 3
DEFAULT_LOCK_TIMEOUT_SECONDS: float = 5.0


@dataclass
class ExecutionRecoveryResult:
    """Summary of executions recovered in a single recovery batch."""

    recovered_resumed: list[uuid.UUID] = field(default_factory=list)
    recovered_failed: list[uuid.UUID] = field(default_factory=list)
    recovered_cancelled: list[uuid.UUID] = field(default_factory=list)
    skipped_active: list[uuid.UUID] = field(default_factory=list)

    @property
    def total_recovered(self) -> int:
        return (
            len(self.recovered_resumed)
            + len(self.recovered_failed)
            + len(self.recovered_cancelled)
        )


@dataclass
class TaskRecoveryResult:
    """Summary of tasks recovered in a single recovery batch."""

    recovered_retried: list[uuid.UUID] = field(default_factory=list)
    recovered_failed: list[uuid.UUID] = field(default_factory=list)
    recovered_cancelled: list[uuid.UUID] = field(default_factory=list)
    skipped_active: list[uuid.UUID] = field(default_factory=list)

    @property
    def total_recovered(self) -> int:
        return (
            len(self.recovered_retried)
            + len(self.recovered_failed)
            + len(self.recovered_cancelled)
        )


@dataclass
class TaskRecoveryRunSummary:
    """Summary of the scheduled recovery loop run."""

    batches_processed: int = 0
    total_scanned: int = 0
    total_retried: int = 0
    total_failed: int = 0
    total_cancelled: int = 0
    total_skipped: int = 0
    elapsed_seconds: float = 0.0
    terminated_by_timeout: bool = False
    executions_recovered: int = 0


class TaskRecoveryService:
    """Scheduled task recovery service.

    Recovers tasks with expired worker leases using consistent parent-first
    lock ordering, enforces retry backoff and attempt limits without incrementing
    attempts on recovery, and guarantees terminal status and outbox event
    emission when deadlines, cancellations, or attempt limits are reached.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        batch_size: int = DEFAULT_RECOVERY_BATCH_SIZE,
        max_batch_size: int = DEFAULT_MAX_RECOVERY_BATCH_SIZE,
        max_runtime_seconds: float = DEFAULT_MAX_RECOVERY_RUNTIME_SECONDS,
        base_backoff_seconds: int = DEFAULT_RECOVERY_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: int = DEFAULT_RECOVERY_MAX_BACKOFF_SECONDS,
        max_attempts: int | None = None,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._max_batch_size = max_batch_size
        self._batch_size = max(1, min(batch_size, max_batch_size))
        self._max_runtime_seconds = max(1.0, max_runtime_seconds)
        self._base_backoff_seconds = max(1, base_backoff_seconds)
        self._max_backoff_seconds = max(base_backoff_seconds, max_backoff_seconds)
        self._max_attempts = max(1, max_attempts) if max_attempts is not None else None
        self._lock_timeout_seconds = max(0.1, lock_timeout_seconds)

    async def recover_expired_execution_leases(
        self,
        *,
        batch_size: int | None = None,
        timeout_seconds: float | None = None,
        lock_timeout_seconds: float | None = None,
    ) -> ExecutionRecoveryResult:
        """Atomically recovers a batch of RUNNING executions with expired runner leases.

        Enforces row locking on ExecutionModel rows, rechecks lease expiry,
        and safely invalidates stale ownership, re-emitting durable redispatch outbox events
        or transitioning terminal executions cleanly.
        """
        if timeout_seconds is not None:
            return await asyncio.wait_for(
                self._recover_expired_execution_leases_impl(
                    batch_size=batch_size,
                    lock_timeout_seconds=lock_timeout_seconds,
                ),
                timeout=timeout_seconds,
            )

        return await self._recover_expired_execution_leases_impl(
            batch_size=batch_size,
            lock_timeout_seconds=lock_timeout_seconds,
        )

    async def _recover_expired_execution_leases_impl(
        self,
        *,
        batch_size: int | None = None,
        lock_timeout_seconds: float | None = None,
    ) -> ExecutionRecoveryResult:
        effective_batch_size = max(
            1, min(batch_size or self._batch_size, self._max_batch_size)
        )
        now = datetime.now(UTC)
        result = ExecutionRecoveryResult()

        # Step 1: Scan for candidate execution IDs without row locking
        scan_stmt = (
            select(ExecutionModel.id)
            .where(
                (ExecutionModel.status == "RUNNING")
                & ExecutionModel.lease_expiry.is_not(None)
                & (ExecutionModel.lease_expiry <= now)
            )
            .order_by(ExecutionModel.lease_expiry.asc())
            .limit(effective_batch_size)
        )

        async with self._session_factory() as scan_session:
            scan_res = await scan_session.execute(scan_stmt)
            candidate_ids = [row[0] for row in scan_res.fetchall()]

        if not candidate_ids:
            return result

        # Step 2: Recover each candidate with row locking
        effective_lock_timeout = lock_timeout_seconds or self._lock_timeout_seconds
        for exec_id in candidate_ids:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text(f"SET LOCAL lock_timeout = '{int(effective_lock_timeout * 1000)}ms'")
                    )

                    exec_stmt = (
                        select(ExecutionModel)
                        .where(ExecutionModel.id == exec_id)
                        .with_for_update()
                    )
                    execution = (await session.execute(exec_stmt)).scalar_one_or_none()
                    if execution is None:
                        continue

                    # Recheck status and lease expiry under lock
                    current_now = datetime.now(UTC)
                    if (
                        execution.status != "RUNNING"
                        or execution.lease_expiry is None
                        or execution.lease_expiry > current_now
                    ):
                        result.skipped_active.append(exec_id)
                        continue

                    # Barrier 1: Cancellation check
                    if execution.cancelled_at is not None or execution.status == "CANCELLED":
                        execution.status = "CANCELLED"
                        execution.lease_token = None
                        execution.lease_expiry = None
                        execution.updated_at = current_now
                        result.recovered_cancelled.append(exec_id)
                        continue

                    # Barrier 2: Deadline check
                    if execution.deadline_at and execution.deadline_at <= current_now:
                        execution.status = "FAILED"
                        execution.lease_token = None
                        execution.lease_expiry = None
                        execution.updated_at = current_now

                        existing_failed_stmt = select(OutboxEventModel.id).where(
                            OutboxEventModel.execution_id == exec_id,
                            OutboxEventModel.kind == "EXECUTION_FAILED",
                        )
                        if not (await session.execute(existing_failed_stmt)).scalar():
                            session.add(
                                OutboxEventModel(
                                    id=uuid.uuid4(),
                                    execution_id=exec_id,
                                    aggregate_id=exec_id,
                                    task_id=None,
                                    kind="EXECUTION_FAILED",
                                    event_type="EXECUTION_FAILED",
                                    payload_ref={
                                        "execution_id": str(exec_id),
                                        "reason": "Deadline exceeded during execution lease recovery",
                                    },
                                    payload={
                                        "execution_id": str(exec_id),
                                        "reason": "Deadline exceeded during execution lease recovery",
                                    },
                                    due_at=current_now,
                                    attempts=0,
                                    created_at=current_now,
                                )
                            )
                            await record_job_event(
                                session,
                                execution_id=exec_id,
                                event_type="EXECUTION_FAILED",
                                safe_payload={
                                    "status": "FAILED",
                                    "reason": "Deadline exceeded during execution lease recovery",
                                },
                            )
                        result.recovered_failed.append(exec_id)
                        continue

                    # Barrier 3: Stale Runner Lease Expiry -> Reset ownership & emit redispatch
                    execution.lease_token = None
                    execution.lease_expiry = None
                    execution.updated_at = current_now

                    # Determine redispatch event type based on whether execution has checkpoints
                    if execution.checkpoint_id is None:
                        execution.status = "CREATED"
                        event_kind = "EXECUTION_START"
                    else:
                        execution.status = "WAITING_TASKS"
                        event_kind = "EXECUTION_RESUME"

                    # Deduplicate outbox event: check if an undelivered redispatch event exists
                    existing_redispatch_stmt = select(OutboxEventModel.id).where(
                        OutboxEventModel.execution_id == exec_id,
                        OutboxEventModel.kind == event_kind,
                        OutboxEventModel.delivered_at.is_(None),
                    )
                    if not (await session.execute(existing_redispatch_stmt)).scalar():
                        dispatch_payload = {"execution_id": str(exec_id)}
                        session.add(
                            OutboxEventModel(
                                id=uuid.uuid4(),
                                execution_id=exec_id,
                                aggregate_id=exec_id,
                                task_id=None,
                                kind=event_kind,
                                event_type=event_kind,
                                payload_ref=dispatch_payload,
                                payload=dispatch_payload,
                                due_at=current_now,
                                attempts=0,
                                created_at=current_now,
                            )
                        )

                    await record_job_event(
                        session,
                        execution_id=exec_id,
                        event_type="EXECUTION_LEASE_RECOVERED",
                        safe_payload={
                            "status": execution.status,
                            "event_kind": event_kind,
                        },
                    )
                    result.recovered_resumed.append(exec_id)

        return result

    async def recover_expired_leases(
        self,
        *,
        batch_size: int | None = None,
        execution_id: uuid.UUID | None = None,
        exclude_task_ids: set[uuid.UUID] | None = None,
        timeout_seconds: float | None = None,
        lock_timeout_seconds: float | None = None,
    ) -> TaskRecoveryResult:
        """Atomically recovers a batch of expired worker task leases and eligible pending tasks.

        Enforces strict parent-first lock ordering (executions -> tasks) to eliminate
        deadlock risks against concurrent worker completion, renewal, and cancellation.
        """
        if timeout_seconds is not None:
            return await asyncio.wait_for(
                self._recover_expired_leases_impl(
                    batch_size=batch_size,
                    execution_id=execution_id,
                    exclude_task_ids=exclude_task_ids,
                    lock_timeout_seconds=lock_timeout_seconds,
                ),
                timeout=timeout_seconds,
            )

        return await self._recover_expired_leases_impl(
            batch_size=batch_size,
            execution_id=execution_id,
            exclude_task_ids=exclude_task_ids,
            lock_timeout_seconds=lock_timeout_seconds,
        )

    async def _recover_expired_leases_impl(
        self,
        *,
        batch_size: int | None = None,
        execution_id: uuid.UUID | None = None,
        exclude_task_ids: set[uuid.UUID] | None = None,
        lock_timeout_seconds: float | None = None,
    ) -> TaskRecoveryResult:
        effective_batch_size = max(
            1, min(batch_size or self._batch_size, self._max_batch_size)
        )
        now = datetime.now(UTC)
        result = TaskRecoveryResult()

        # Step 1: Scan for candidate task IDs without row locking to preserve parent-first order
        # Identifies:
        # a) Tasks in CLAIMED or RUNNING with expired worker leases
        # b) Tasks in PENDING with expired task deadlines
        # c) Tasks in PENDING under cancelled, terminal, or deadline-expired parent executions
        scan_stmt = (
            select(TaskModel.id, TaskModel.execution_id)
            .join(ExecutionModel, TaskModel.execution_id == ExecutionModel.id)
            .where(
                or_(
                    (
                        TaskModel.status.in_(["CLAIMED", "RUNNING"])
                        & TaskModel.lease_expiry.is_not(None)
                        & (TaskModel.lease_expiry <= now)
                    ),
                    (
                        (TaskModel.status == "PENDING")
                        & (TaskModel.deadline_at <= now)
                    ),
                    (
                        (TaskModel.status == "PENDING")
                        & (
                            ExecutionModel.status.in_(
                                ["CANCELLED", "COMPLETED", "SUCCEEDED", "FAILED", "PARTIAL"]
                            )
                            | ExecutionModel.cancelled_at.is_not(None)
                            | (ExecutionModel.deadline_at <= now)
                        )
                    ),
                )
            )
        )
        if execution_id is not None:
            scan_stmt = scan_stmt.where(TaskModel.execution_id == execution_id)

        if exclude_task_ids:
            scan_stmt = scan_stmt.where(~TaskModel.id.in_(exclude_task_ids))

        scan_stmt = scan_stmt.order_by(
            func.coalesce(TaskModel.lease_expiry, TaskModel.deadline_at).asc()
        ).limit(effective_batch_size)

        async with self._session_factory() as scan_session:
            scan_res = await scan_session.execute(scan_stmt)
            candidates = [(row.id, row.execution_id) for row in scan_res]

        if not candidates:
            return result

        # Step 2: Recover each candidate with parent-first locking
        effective_lock_timeout = lock_timeout_seconds or self._lock_timeout_seconds
        for task_id, exec_id in candidates:
            async with self._session_factory() as session:
                async with session.begin():
                    # Set local lock timeout to prevent blocked queries from hanging indefinitely
                    await session.execute(
                        text(f"SET LOCAL lock_timeout = '{int(effective_lock_timeout * 1000)}ms'")
                    )

                    # 1. Lock Parent Execution FIRST
                    exec_stmt = (
                        select(ExecutionModel)
                        .where(ExecutionModel.id == exec_id)
                        .with_for_update()
                    )
                    exec_res = await session.execute(exec_stmt)
                    execution = exec_res.scalar_one_or_none()
                    if execution is None:
                        result.skipped_active.append(task_id)
                        continue

                    # 2. Lock Child Task SECOND
                    task_stmt = (
                        select(TaskModel)
                        .where(TaskModel.id == task_id)
                        .with_for_update()
                    )
                    task_res = await session.execute(task_stmt)
                    task = task_res.scalar_one_or_none()
                    if task is None:
                        result.skipped_active.append(task_id)
                        continue

                    # Fresh timestamp after lock acquisition to recheck eligibility accurately
                    current_now = datetime.now(UTC)

                    # 3. Check if task is already in terminal state
                    if task.status in ("COMPLETED", "FAILED", "CANCELLED"):
                        result.skipped_active.append(task_id)
                        continue

                    # 4. Parent Cancellation check
                    if execution.status == "CANCELLED" or execution.cancelled_at is not None:
                        task.status = "CANCELLED"
                        task.lease_token = None
                        task.lease_expiry = None
                        task.updated_at = current_now
                        session.add(
                            OutboxEventModel(
                                id=uuid.uuid4(),
                                execution_id=exec_id,
                                aggregate_id=exec_id,
                                task_id=task.id,
                                kind="TASK_CANCELLED",
                                event_type="TASK_CANCELLED",
                                payload_ref={
                                    "schema_version": "1.0",
                                    "task_id": str(task.id),
                                    "execution_id": str(exec_id),
                                    "reason": "parent_execution_cancelled",
                                },
                                payload={
                                    "schema_version": "1.0",
                                    "task_id": str(task.id),
                                    "execution_id": str(exec_id),
                                    "reason": "parent_execution_cancelled",
                                },
                                due_at=current_now,
                                attempts=0,
                                created_at=current_now,
                            )
                        )
                        result.recovered_cancelled.append(task.id)
                        continue

                    # 5. Parent Terminal / Ineligible check (COMPLETED, SUCCEEDED, FAILED, PARTIAL)
                    if (
                        execution.status in ("COMPLETED", "SUCCEEDED", "FAILED", "PARTIAL")
                        or execution.status not in ("RUNNING", "WAITING_TASKS", "CREATED")
                    ):
                        task.status = "FAILED"
                        task.lease_token = None
                        task.lease_expiry = None
                        task.updated_at = current_now
                        session.add(
                            OutboxEventModel(
                                id=uuid.uuid4(),
                                execution_id=exec_id,
                                aggregate_id=exec_id,
                                task_id=task.id,
                                kind="TASK_FAILED",
                                event_type="TASK_FAILED",
                                payload_ref={
                                    "schema_version": "1.0",
                                    "task_id": str(task.id),
                                    "execution_id": str(exec_id),
                                    "attempt": task.attempt,
                                    "reason": f"parent_execution_{execution.status.lower()}",
                                },
                                payload={
                                    "schema_version": "1.0",
                                    "task_id": str(task.id),
                                    "execution_id": str(exec_id),
                                    "attempt": task.attempt,
                                    "reason": f"parent_execution_{execution.status.lower()}",
                                },
                                due_at=current_now,
                                attempts=0,
                                created_at=current_now,
                            )
                        )
                        result.recovered_failed.append(task.id)
                        continue

                    # 6. Deadlines check (Execution deadline or task deadline)
                    if execution.deadline_at <= current_now or task.deadline_at <= current_now:
                        task.status = "FAILED"
                        task.lease_token = None
                        task.lease_expiry = None
                        task.updated_at = current_now
                        session.add(
                            OutboxEventModel(
                                id=uuid.uuid4(),
                                execution_id=exec_id,
                                aggregate_id=exec_id,
                                task_id=task.id,
                                kind="TASK_FAILED",
                                event_type="TASK_FAILED",
                                payload_ref={
                                    "schema_version": "1.0",
                                    "task_id": str(task.id),
                                    "execution_id": str(exec_id),
                                    "attempt": task.attempt,
                                    "reason": "deadline_exceeded",
                                },
                                payload={
                                    "schema_version": "1.0",
                                    "task_id": str(task.id),
                                    "execution_id": str(exec_id),
                                    "attempt": task.attempt,
                                    "reason": "deadline_exceeded",
                                },
                                due_at=current_now,
                                attempts=0,
                                created_at=current_now,
                            )
                        )
                        result.recovered_failed.append(task.id)
                        continue

                    # 7. Context version check
                    if execution.context_version != task.context_version:
                        task.status = "FAILED"
                        task.lease_token = None
                        task.lease_expiry = None
                        task.updated_at = current_now
                        session.add(
                            OutboxEventModel(
                                id=uuid.uuid4(),
                                execution_id=exec_id,
                                aggregate_id=exec_id,
                                task_id=task.id,
                                kind="TASK_FAILED",
                                event_type="TASK_FAILED",
                                payload_ref={
                                    "schema_version": "1.0",
                                    "task_id": str(task.id),
                                    "execution_id": str(exec_id),
                                    "attempt": task.attempt,
                                    "reason": "stale_context_version",
                                },
                                payload={
                                    "schema_version": "1.0",
                                    "task_id": str(task.id),
                                    "execution_id": str(exec_id),
                                    "attempt": task.attempt,
                                    "reason": "stale_context_version",
                                },
                                due_at=current_now,
                                attempts=0,
                                created_at=current_now,
                            )
                        )
                        result.recovered_failed.append(task.id)
                        continue

                    # 8. Check if task is in PENDING state
                    if task.status == "PENDING":
                        # Valid active retry in backoff or awaiting claim with healthy parent and unexpired deadlines
                        result.skipped_active.append(task.id)
                        continue

                    # 9. Task is in CLAIMED or RUNNING state: verify lease has actually expired
                    if task.lease_expiry is None or task.lease_expiry > current_now:
                        result.skipped_active.append(task.id)
                        continue

                    # 10. Attempt limit exhaustion check (respects task.max_attempts unless service override configured)
                    max_attempts_limit = (
                        min(task.max_attempts, self._max_attempts)
                        if self._max_attempts is not None
                        else task.max_attempts
                    )
                    if task.attempt >= max_attempts_limit:
                        task.status = "FAILED"
                        task.lease_token = None
                        task.lease_expiry = None
                        task.updated_at = current_now
                        session.add(
                            OutboxEventModel(
                                id=uuid.uuid4(),
                                execution_id=exec_id,
                                aggregate_id=exec_id,
                                task_id=task.id,
                                kind="TASK_FAILED",
                                event_type="TASK_FAILED",
                                payload_ref={
                                    "schema_version": "1.0",
                                    "task_id": str(task.id),
                                    "execution_id": str(exec_id),
                                    "attempt": task.attempt,
                                    "reason": "max_attempts_exhausted",
                                },
                                payload={
                                    "schema_version": "1.0",
                                    "task_id": str(task.id),
                                    "execution_id": str(exec_id),
                                    "attempt": task.attempt,
                                    "reason": "max_attempts_exhausted",
                                },
                                due_at=current_now,
                                attempts=0,
                                created_at=current_now,
                            )
                        )
                        result.recovered_failed.append(task.id)
                        continue

                    # 11. Eligible retry: Reset to PENDING, schedule delayed TASK_DISPATCH, DO NOT increment attempt!
                    calc_backoff = min(
                        self._max_backoff_seconds,
                        int(self._base_backoff_seconds * (2 ** max(0, task.attempt - 1))),
                    )
                    backoff_expiry = current_now + timedelta(seconds=calc_backoff)

                    task.status = "PENDING"
                    task.lease_token = None
                    task.lease_expiry = backoff_expiry
                    task.updated_at = current_now

                    session.add(
                        OutboxEventModel(
                            id=uuid.uuid4(),
                            execution_id=exec_id,
                            aggregate_id=exec_id,
                            task_id=task.id,
                            kind="TASK_DISPATCH",
                            event_type="TASK_DISPATCH",
                            payload_ref={
                                "schema_version": "1.0",
                                "task_id": str(task.id),
                                "execution_id": str(exec_id),
                                "worker": task.worker,
                                "action": task.action,
                                "attempt": task.attempt,
                                "context_version": task.context_version,
                                "payload": task.payload_ref,
                            },
                            payload={
                                "schema_version": "1.0",
                                "task_id": str(task.id),
                                "execution_id": str(exec_id),
                                "worker": task.worker,
                                "action": task.action,
                                "attempt": task.attempt,
                                "context_version": task.context_version,
                                "payload": task.payload_ref,
                            },
                            due_at=backoff_expiry,
                            attempts=0,
                            created_at=current_now,
                        )
                    )
                    result.recovered_retried.append(task.id)

        return result

    async def run_recovery_loop(
        self,
        *,
        max_runtime_seconds: float | None = None,
        execution_id: uuid.UUID | None = None,
        batch_size: int | None = None,
        idle_sleep_seconds: float = 0.5,
    ) -> TaskRecoveryRunSummary:
        """Runs the scheduled recovery loop until backlog is cleared or time budget expires.

        Bounds database lock and query waits using remaining time budgets.
        """
        runtime_limit = max_runtime_seconds or self._max_runtime_seconds
        start_time = time.monotonic()
        summary = TaskRecoveryRunSummary()
        skipped_task_ids: set[uuid.UUID] = set()

        while True:
            elapsed = time.monotonic() - start_time
            remaining_time = runtime_limit - elapsed
            if remaining_time <= 0:
                summary.terminated_by_timeout = True
                break

            # 1. Recover expired execution leases within time budget
            exec_res = ExecutionRecoveryResult()
            try:
                exec_res = await asyncio.wait_for(
                    self.recover_expired_execution_leases(
                        batch_size=batch_size,
                    ),
                    timeout=remaining_time,
                )
                summary.executions_recovered += exec_res.total_recovered
            except TimeoutError:
                summary.terminated_by_timeout = True
                break

            elapsed = time.monotonic() - start_time
            remaining_time = runtime_limit - elapsed
            if remaining_time <= 0:
                summary.terminated_by_timeout = True
                break

            # 2. Recover expired task leases within remaining time budget
            try:
                batch_res = await asyncio.wait_for(
                    self.recover_expired_leases(
                        batch_size=batch_size,
                        execution_id=execution_id,
                        exclude_task_ids=skipped_task_ids if skipped_task_ids else None,
                    ),
                    timeout=remaining_time,
                )
            except TimeoutError:
                summary.terminated_by_timeout = True
                break

            if (
                batch_res.total_recovered == 0
                and len(batch_res.skipped_active) == 0
                and exec_res.total_recovered == 0
                and len(exec_res.skipped_active) == 0
            ):
                # Backlog cleared
                break

            for tid in batch_res.skipped_active:
                skipped_task_ids.add(tid)

            summary.batches_processed += 1
            summary.total_scanned += (
                batch_res.total_recovered
                + len(batch_res.skipped_active)
                + exec_res.total_recovered
                + len(exec_res.skipped_active)
            )
            summary.total_retried += len(batch_res.recovered_retried)
            summary.total_failed += len(batch_res.recovered_failed) + len(exec_res.recovered_failed)
            summary.total_cancelled += len(batch_res.recovered_cancelled) + len(exec_res.recovered_cancelled)
            summary.total_skipped += len(batch_res.skipped_active) + len(exec_res.skipped_active)

            # Avoid tight loop spin if all items were skipped (e.g. concurrent updates)
            if (
                batch_res.total_recovered == 0
                and exec_res.total_recovered == 0
                and (len(batch_res.skipped_active) > 0 or len(exec_res.skipped_active) > 0)
            ):
                await asyncio.sleep(idle_sleep_seconds)

        summary.elapsed_seconds = time.monotonic() - start_time
        return summary
