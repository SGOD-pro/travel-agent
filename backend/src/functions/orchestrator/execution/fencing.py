"""Worker claim and lease fencing service for orchestrator execution engine conforming to spec 0001."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import (
    ExecutionModel,
    OutboxEventModel,
    TaskModel,
    WorkerResultModel,
)


class ExecutionEngineError(Exception):
    """Base exception for orchestrator execution engine errors."""


class ExecutionNotFoundError(ExecutionEngineError):
    """Raised when the specified parent execution record does not exist."""


class ParentCancelledError(ExecutionEngineError):
    """Raised when the parent execution is cancelled."""


class ParentTerminalError(ExecutionEngineError):
    """Raised when the parent execution is in a terminal state."""


class DeadlineExceededError(ExecutionEngineError):
    """Raised when an execution or task deadline has passed."""


class StaleContextError(ExecutionEngineError):
    """Raised when context version mismatches."""


class TaskNotFoundError(ExecutionEngineError):
    """Raised when the specified task does not exist."""


class ExecutionMismatchError(ExecutionEngineError):
    """Raised when the task does not belong to the given execution."""


class WrongWorkerError(ExecutionEngineError):
    """Raised when a worker or action does not match the task specification."""


class AttemptsExhaustedError(ExecutionEngineError):
    """Raised when task attempts have reached max_attempts limit."""


class TaskAlreadyTerminalError(ExecutionEngineError):
    """Raised when a task is already completed, failed, or cancelled."""


class TaskLeaseActiveError(ExecutionEngineError):
    """Raised when a task is currently held by an active unexpired worker lease."""


class TaskInRetryBackoffError(ExecutionEngineError):
    """Raised when a task is currently in retry backoff and cannot be claimed."""


class StaleQueueMessageError(ExecutionEngineError):
    """Raised when a queue message was dispatched for an earlier attempt."""


class TaskClaimConflictError(ExecutionEngineError):
    """Raised when a task claim loses a concurrent race condition."""


class LeaseFencingError(ExecutionEngineError):
    """Raised when a lease renewal, completion, or failure check fails due to stale lease token or expiration."""


class TaskQueueMessage(BaseModel):
    """Queue message contract for dispatching tasks to workers."""

    schema_version: str = "1.0"
    task_id: uuid.UUID
    execution_id: uuid.UUID
    attempt: int | None = None


class TaskClaimResult(BaseModel):
    """Result of an atomic task claim."""

    task_id: uuid.UUID
    execution_id: uuid.UUID
    worker: str
    action: str
    attempt: int
    context_version: int
    lease_token: uuid.UUID
    lease_expiry: datetime
    payload_ref: dict[str, Any] = Field(default_factory=dict)


class WorkerFencingService:
    """Service managing worker task claims, lease fencing, renewals, and completions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_task(
        self,
        *,
        message: TaskQueueMessage | dict[str, Any],
        worker: str,
        action: str,
        expected_context_version: int = 1,
        lease_seconds: int = 300,
    ) -> TaskClaimResult:
        """Atomically claims a task from a queue message.

        Enforces consistent parent then task lock ordering:
        1. Lock executions row FOR UPDATE
        2. Lock tasks row FOR UPDATE
        3. Validate execution identity, worker/action, context, parent status, deadlines,
           retry eligibility, and attempt limit.
        4. Increments attempt strictly on successful claim.

        Raises domain exceptions on validation failures with zero attempt increment.
        """
        if isinstance(message, dict):
            task_id = uuid.UUID(str(message["task_id"]))
            execution_id = uuid.UUID(str(message["execution_id"]))
            message_attempt = message.get("attempt")
        else:
            task_id = message.task_id
            execution_id = message.execution_id
            message_attempt = message.attempt

        now = datetime.now(UTC)

        # Consistent lock order: Parent (executions) FIRST
        exec_stmt = (
            select(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .with_for_update()
        )
        exec_res = await self._session.execute(exec_stmt)
        execution = exec_res.scalar_one_or_none()

        if execution is None:
            raise ExecutionNotFoundError(f"Execution {execution_id} not found")

        if execution.status == "CANCELLED" or execution.cancelled_at is not None:
            raise ParentCancelledError(f"Execution {execution_id} is cancelled")

        if execution.status in ("SUCCEEDED", "FAILED", "PARTIAL"):
            raise ParentTerminalError(
                f"Execution {execution_id} is in terminal status {execution.status}"
            )

        if execution.status not in ("RUNNING", "WAITING_TASKS", "CREATED"):
            raise ParentTerminalError(
                f"Execution {execution_id} is in ineligible status {execution.status}"
            )

        if execution.deadline_at <= now:
            raise DeadlineExceededError(
                f"Execution {execution_id} deadline passed at {execution.deadline_at}"
            )

        if execution.context_version != expected_context_version:
            raise StaleContextError(
                f"Execution context version {execution.context_version} does not match expected {expected_context_version}"
            )

        # Consistent lock order: Child (tasks) SECOND
        task_stmt = (
            select(TaskModel)
            .where(TaskModel.id == task_id)
            .with_for_update()
        )
        task_res = await self._session.execute(task_stmt)
        task = task_res.scalar_one_or_none()

        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found")

        if task.execution_id != execution_id:
            raise ExecutionMismatchError(
                f"Task {task_id} belongs to execution {task.execution_id}, not {execution_id}"
            )

        if task.worker != worker or task.action != action:
            raise WrongWorkerError(
                f"Task requires {task.worker}/{task.action}, requested {worker}/{action}"
            )

        if task.context_version != expected_context_version:
            raise StaleContextError(
                f"Task context version {task.context_version} does not match expected {expected_context_version}"
            )

        if task.deadline_at <= now:
            raise DeadlineExceededError(
                f"Task {task_id} deadline passed at {task.deadline_at}"
            )

        if task.status in ("COMPLETED", "FAILED", "CANCELLED"):
            raise TaskAlreadyTerminalError(
                f"Task {task_id} is already in terminal state {task.status}"
            )

        if task.attempt >= task.max_attempts:
            raise AttemptsExhaustedError(
                f"Task {task_id} has exhausted attempts ({task.attempt}/{task.max_attempts})"
            )

        # Stale queue message check: an old queue message cannot bypass attempt or retry backoff
        if message_attempt is not None and message_attempt < task.attempt:
            raise StaleQueueMessageError(
                f"Queue message attempt {message_attempt} is stale; task is already at attempt {task.attempt}"
            )

        # Check if task is currently under active retry backoff delay
        if (
            task.status == "PENDING"
            and task.lease_expiry is not None
            and task.lease_expiry > now
        ):
            raise TaskInRetryBackoffError(
                f"Task {task_id} is in retry backoff until {task.lease_expiry}"
            )

        # Check if an active unexpired lease is held by another worker
        if (
            task.status in ("CLAIMED", "RUNNING")
            and task.lease_expiry is not None
            and task.lease_expiry > now
        ):
            raise TaskLeaseActiveError(
                f"Task {task_id} is currently executing under active lease until {task.lease_expiry}"
            )

        # Eligible for claim: execute atomic fenced claim update
        new_lease_token = uuid.uuid4()
        new_lease_expiry = now + timedelta(seconds=lease_seconds)

        claim_sql = text(
            """
            UPDATE tasks
            SET status = 'CLAIMED',
                lease_token = :lease_token,
                lease_expiry = :lease_expiry,
                attempt = attempt + 1,
                updated_at = :updated_at
            WHERE id = :task_id
              AND (
                (status = 'PENDING' AND (lease_expiry IS NULL OR lease_expiry <= :now))
                OR (status IN ('CLAIMED', 'RUNNING') AND lease_expiry <= :now)
              )
            RETURNING id, attempt, lease_token, lease_expiry;
            """
        )
        async with self._session.begin_nested():
            res = await self._session.execute(
                claim_sql,
                {
                    "lease_token": new_lease_token,
                    "lease_expiry": new_lease_expiry,
                    "updated_at": now,
                    "task_id": task_id,
                    "now": now,
                },
            )
            claimed_row = res.mappings().first()
            if claimed_row is None:
                raise TaskClaimConflictError(
                    f"Task {task_id} claim conflict: task was claimed concurrently"
                )

        await self._session.commit()

        return TaskClaimResult(
            task_id=task.id,
            execution_id=execution.id,
            worker=task.worker,
            action=task.action,
            attempt=claimed_row["attempt"],
            context_version=task.context_version,
            lease_token=claimed_row["lease_token"],
            lease_expiry=claimed_row["lease_expiry"],
            payload_ref=task.payload_ref,
        )

    async def renew_lease(
        self,
        *,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        lease_token: uuid.UUID,
        expected_context_version: int | None = None,
        extension_seconds: int = 300,
    ) -> datetime:
        """Atomically extends an active task lease.

        Consistent lock ordering: checks parent execution first, then verifies
        unexpired lease token before extending.
        """
        now = datetime.now(UTC)

        # 1. Lock executions parent row
        exec_stmt = (
            select(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .with_for_update()
        )
        exec_res = await self._session.execute(exec_stmt)
        execution = exec_res.scalar_one_or_none()

        if execution is None:
            raise ExecutionNotFoundError(f"Execution {execution_id} not found")

        if execution.status == "CANCELLED" or execution.cancelled_at is not None:
            raise ParentCancelledError(
                f"Execution {execution_id} is cancelled; lease renewal rejected"
            )

        if execution.status in ("SUCCEEDED", "FAILED", "PARTIAL"):
            raise ParentTerminalError(
                f"Execution {execution_id} is terminal {execution.status}; lease renewal rejected"
            )

        if execution.status not in ("RUNNING", "WAITING_TASKS", "CREATED"):
            raise ParentTerminalError(
                f"Execution {execution_id} status {execution.status} ineligible; lease renewal rejected"
            )

        if execution.deadline_at <= now:
            raise DeadlineExceededError(
                f"Execution {execution_id} deadline passed; lease renewal rejected"
            )

        # 2. Lock and check tasks row SECOND
        task_stmt = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
        task_res = await self._session.execute(task_stmt)
        task = task_res.scalar_one_or_none()

        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found")

        if task.execution_id != execution_id:
            raise ExecutionMismatchError(f"Task {task_id} execution mismatch")

        if execution.context_version != task.context_version:
            raise StaleContextError(
                f"Execution context version {execution.context_version} mismatches task context {task.context_version}"
            )

        if (
            expected_context_version is not None
            and task.context_version != expected_context_version
        ):
            raise StaleContextError(
                f"Task context version {task.context_version} does not match expected {expected_context_version}"
            )

        if task.deadline_at <= now:
            raise DeadlineExceededError(
                f"Task {task_id} deadline passed; lease renewal rejected"
            )

        if (
            task.lease_token != lease_token
            or task.status not in ("CLAIMED", "RUNNING")
            or task.lease_expiry is None
            or task.lease_expiry <= now
        ):
            raise LeaseFencingError(
                f"Lease renewal rejected for task {task_id}: lease token expired or stolen"
            )

        # 3. Fenced update on task
        renew_sql = text(
            """
            UPDATE tasks
            SET lease_expiry = :new_expiry,
                updated_at = :updated_at
            WHERE id = :task_id
              AND execution_id = :execution_id
              AND lease_token = :lease_token
              AND status IN ('CLAIMED', 'RUNNING')
              AND lease_expiry > :now
            RETURNING id, lease_expiry;
            """
        )
        new_expiry = now + timedelta(seconds=extension_seconds)
        async with self._session.begin_nested():
            res = await self._session.execute(
                renew_sql,
                {
                    "new_expiry": new_expiry,
                    "updated_at": now,
                    "task_id": task_id,
                    "execution_id": execution_id,
                    "lease_token": lease_token,
                    "now": now,
                },
            )
            row = res.mappings().first()
            if row is None:
                raise LeaseFencingError(
                    f"Lease renewal rejected for task {task_id}: lease token expired or stolen"
                )

        await self._session.commit()
        return row["lease_expiry"]

    async def complete_task(
        self,
        *,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        lease_token: uuid.UUID,
        output: dict[str, Any],
        expected_context_version: int | None = None,
        warnings: list[str] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Fenced task completion.

        Validates parent execution status, verifies active unexpired lease token,
        persists worker_results record, sets tasks.status = COMPLETED with result_id,
        and emits TASK_COMPLETED outbox event in ONE atomic transaction.
        """
        now = datetime.now(UTC)

        # 1. Lock executions parent row FIRST
        exec_stmt = (
            select(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .with_for_update()
        )
        exec_res = await self._session.execute(exec_stmt)
        execution = exec_res.scalar_one_or_none()

        if execution is None:
            raise ExecutionNotFoundError(f"Execution {execution_id} not found")

        if execution.status == "CANCELLED" or execution.cancelled_at is not None:
            raise ParentCancelledError(
                f"Execution {execution_id} was cancelled; worker completion rejected"
            )

        if execution.status in ("SUCCEEDED", "FAILED", "PARTIAL"):
            raise ParentTerminalError(
                f"Execution {execution_id} is terminal {execution.status}; completion rejected"
            )

        if execution.status not in ("RUNNING", "WAITING_TASKS", "CREATED"):
            raise ParentTerminalError(
                f"Execution {execution_id} status {execution.status} ineligible; completion rejected"
            )

        if execution.deadline_at <= now:
            raise DeadlineExceededError(
                f"Execution {execution_id} deadline passed; worker completion rejected"
            )

        # 2. Lock and check tasks row SECOND
        task_stmt = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
        task_res = await self._session.execute(task_stmt)
        task = task_res.scalar_one_or_none()

        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found")

        if task.execution_id != execution_id:
            raise ExecutionMismatchError(f"Task {task_id} execution mismatch")

        if execution.context_version != task.context_version:
            raise StaleContextError(
                f"Execution context version {execution.context_version} mismatches task context {task.context_version}"
            )

        if (
            expected_context_version is not None
            and task.context_version != expected_context_version
        ):
            raise StaleContextError(
                f"Task context version {task.context_version} does not match expected {expected_context_version}"
            )

        if task.deadline_at <= now:
            raise DeadlineExceededError(
                f"Task {task_id} deadline passed; worker completion rejected"
            )

        if (
            task.lease_token != lease_token
            or task.status not in ("CLAIMED", "RUNNING")
            or task.lease_expiry is None
            or task.lease_expiry <= now
        ):
            raise LeaseFencingError(
                f"Task {task_id} completion rejected: stale or expired lease token"
            )

        # 3. Insert worker_results record, fenced update, and outbox event inside a savepoint
        result_id = uuid.uuid4()
        worker_result = WorkerResultModel(
            id=result_id,
            task_id=task_id,
            execution_id=execution_id,
            attempt=task.attempt,
            context_version=task.context_version,
            status="SUCCESS",
            output_ref=output,
            warnings=warnings or [],
            error=None,
            usage=usage or {},
            started_at=now,
            finished_at=now,
        )

        complete_sql = text(
            """
            UPDATE tasks
            SET status = 'COMPLETED',
                result_id = :result_id,
                lease_token = NULL,
                lease_expiry = NULL,
                updated_at = :updated_at
            WHERE id = :task_id
              AND lease_token = :lease_token
              AND status IN ('CLAIMED', 'RUNNING')
              AND lease_expiry > :now
            RETURNING id;
            """
        )

        outbox_event = OutboxEventModel(
            id=uuid.uuid4(),
            execution_id=execution_id,
            aggregate_id=execution_id,
            task_id=task_id,
            kind="TASK_COMPLETED",
            event_type="TASK_COMPLETED",
            payload_ref={
                "schema_version": "1.0",
                "task_id": str(task_id),
                "execution_id": str(execution_id),
                "result_id": str(result_id),
            },
            payload={
                "schema_version": "1.0",
                "task_id": str(task_id),
                "execution_id": str(execution_id),
                "result_id": str(result_id),
            },
            due_at=now,
            attempts=0,
            created_at=now,
        )

        async with self._session.begin_nested():
            self._session.add(worker_result)
            await self._session.flush()

            res = await self._session.execute(
                complete_sql,
                {
                    "result_id": result_id,
                    "updated_at": now,
                    "task_id": task_id,
                    "lease_token": lease_token,
                    "now": now,
                },
            )
            row = res.mappings().first()
            if row is None:
                raise LeaseFencingError(
                    f"Task {task_id} completion update failed: stale lease token"
                )

            self._session.add(outbox_event)
            await self._session.flush()

            await record_job_event(
                self._session,
                execution_id=execution_id,
                event_type="TASK_COMPLETED",
                safe_payload={
                    "task_id": str(task_id),
                    "worker": task.worker,
                    "action": task.action,
                    "attempt": task.attempt,
                },
            )

        await self._session.commit()
        return result_id

    async def fail_task(
        self,
        *,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        lease_token: uuid.UUID,
        error: dict[str, Any],
        expected_context_version: int | None = None,
        retryable: bool = True,
        backoff_seconds: int | None = None,
    ) -> uuid.UUID:
        """Fenced task failure.

        Validates parent execution and task lease token, persists worker_results
        error record, and either transitions task to FAILED (if max attempts reached
        or non retryable) or resets to PENDING with exponential backoff delay on
        both task lease_expiry and outbox due_at.
        """
        now = datetime.now(UTC)

        # 1. Lock executions parent row FIRST
        exec_stmt = (
            select(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .with_for_update()
        )
        exec_res = await self._session.execute(exec_stmt)
        execution = exec_res.scalar_one_or_none()

        if execution is None:
            raise ExecutionNotFoundError(f"Execution {execution_id} not found")

        if execution.status == "CANCELLED" or execution.cancelled_at is not None:
            raise ParentCancelledError(
                f"Execution {execution_id} was cancelled; worker failure rejected"
            )

        if execution.status in ("SUCCEEDED", "FAILED", "PARTIAL"):
            raise ParentTerminalError(
                f"Execution {execution_id} is terminal {execution.status}; failure rejected"
            )

        if execution.status not in ("RUNNING", "WAITING_TASKS", "CREATED"):
            raise ParentTerminalError(
                f"Execution {execution_id} status {execution.status} ineligible; failure rejected"
            )

        if execution.deadline_at <= now:
            raise DeadlineExceededError(
                f"Execution {execution_id} deadline passed; failure rejected"
            )

        # 2. Lock and check tasks row SECOND
        task_stmt = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
        task_res = await self._session.execute(task_stmt)
        task = task_res.scalar_one_or_none()

        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found")

        if task.execution_id != execution_id:
            raise ExecutionMismatchError(f"Task {task_id} execution mismatch")

        if execution.context_version != task.context_version:
            raise StaleContextError(
                f"Execution context version {execution.context_version} mismatches task context {task.context_version}"
            )

        if (
            expected_context_version is not None
            and task.context_version != expected_context_version
        ):
            raise StaleContextError(
                f"Task context version {task.context_version} does not match expected {expected_context_version}"
            )

        if task.deadline_at <= now:
            raise DeadlineExceededError(
                f"Task {task_id} deadline passed; failure rejected"
            )

        if (
            task.lease_token != lease_token
            or task.status not in ("CLAIMED", "RUNNING")
            or task.lease_expiry is None
            or task.lease_expiry <= now
        ):
            raise LeaseFencingError(
                f"Task {task_id} failure rejected: stale or expired lease token"
            )

        # 3. Prepare worker_results, outbox event, and fail SQL
        result_id = uuid.uuid4()
        worker_result = WorkerResultModel(
            id=result_id,
            task_id=task_id,
            execution_id=execution_id,
            attempt=task.attempt,
            context_version=task.context_version,
            status="FAILED",
            output_ref=None,
            warnings=[],
            error=error,
            usage={},
            started_at=now,
            finished_at=now,
        )

        is_terminal_failure = (task.attempt >= task.max_attempts) or not retryable
        if is_terminal_failure:
            new_status = "FAILED"
            event_kind = "TASK_FAILED"
            backoff_expiry = None
            event_due_at = now
            event_payload = {
                "schema_version": "1.0",
                "task_id": str(task_id),
                "execution_id": str(execution_id),
                "result_id": str(result_id),
                "attempt": task.attempt,
                "error": error,
            }
        else:
            new_status = "PENDING"
            event_kind = "TASK_DISPATCH"
            # Exponential backoff based on attempts made if not explicitly specified
            if backoff_seconds is None:
                calc_backoff = int(30 * (2 ** max(0, task.attempt - 1)))
            else:
                calc_backoff = backoff_seconds
            backoff_expiry = now + timedelta(seconds=calc_backoff)
            event_due_at = backoff_expiry
            event_payload = {
                "schema_version": "1.0",
                "task_id": str(task_id),
                "execution_id": str(execution_id),
                "worker": task.worker,
                "action": task.action,
                "attempt": task.attempt,
                "context_version": task.context_version,
                "payload": task.payload_ref,
                "result_id": str(result_id),
            }

        fail_sql = text(
            """
            UPDATE tasks
            SET status = :new_status,
                result_id = :result_id,
                lease_token = NULL,
                lease_expiry = :backoff_expiry,
                updated_at = :updated_at
            WHERE id = :task_id
              AND lease_token = :lease_token
              AND status IN ('CLAIMED', 'RUNNING')
              AND lease_expiry > :now
            RETURNING id;
            """
        )

        outbox_event = OutboxEventModel(
            id=uuid.uuid4(),
            execution_id=execution_id,
            aggregate_id=execution_id,
            task_id=task_id,
            kind=event_kind,
            event_type=event_kind,
            payload_ref=event_payload,
            payload=event_payload,
            due_at=event_due_at,
            attempts=0,
            created_at=now,
        )

        async with self._session.begin_nested():
            self._session.add(worker_result)
            await self._session.flush()

            res = await self._session.execute(
                fail_sql,
                {
                    "new_status": new_status,
                    "result_id": result_id,
                    "backoff_expiry": backoff_expiry,
                    "updated_at": now,
                    "task_id": task_id,
                    "lease_token": lease_token,
                    "now": now,
                },
            )
            row = res.mappings().first()
            if row is None:
                raise LeaseFencingError(
                    f"Task {task_id} failure update failed: stale lease token"
                )

            self._session.add(outbox_event)
            await self._session.flush()

            await record_job_event(
                self._session,
                execution_id=execution_id,
                event_type="TASK_FAILED",
                safe_payload={
                    "task_id": str(task_id),
                    "worker": task.worker,
                    "action": task.action,
                    "attempt": task.attempt,
                    "retry_eligible": not is_terminal_failure,
                },
            )

        await self._session.commit()
        return result_id
