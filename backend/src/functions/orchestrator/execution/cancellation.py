"""Fenced execution cancellation service conforming to Transaction 8 of spec 0001."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import ExecutionModel, OutboxEventModel


async def cancel_execution(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    owner_id: uuid.UUID,
    reason: str = "User cancellation request",
) -> tuple[ExecutionModel | None, bool]:
    """Cancels a running execution using Transaction 8 and parent-first locking.

    Returns:
        tuple of (execution, was_cancelled_now)
        - (None, False) if execution not found or owner mismatch.
        - (execution, False) if execution is already in terminal state.
        - (execution, True) if execution was successfully cancelled by this call.
    """
    now = datetime.now(UTC)

    # Phase 1: Parent-first row lock on executions
    exec_stmt = (
        select(ExecutionModel)
        .where(ExecutionModel.id == execution_id)
        .with_for_update()
    )
    execution = (await session.execute(exec_stmt)).scalar_one_or_none()

    if execution is None or execution.owner_id != owner_id:
        return None, False

    if execution.status in ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"):
        # Terminal execution: repeated cancellation retains terminal state cleanly
        return execution, False

    # Phase 2: Fenced transition to CANCELLED
    execution.status = "CANCELLED"
    execution.cancelled_at = now
    execution.updated_at = now

    # Phase 3: Timeline event record in job_events
    await record_job_event(
        session,
        execution_id=execution_id,
        event_type="JOB_CANCELLED",
        safe_payload={"status": "CANCELLED", "reason": reason},
    )

    # Phase 4: EXECUTION_CANCELLED outbox event
    outbox_event = OutboxEventModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        aggregate_id=execution_id,
        task_id=None,
        kind="EXECUTION_CANCELLED",
        event_type="EXECUTION_CANCELLED",
        payload_ref={"execution_id": str(execution_id), "reason": reason},
        payload={"execution_id": str(execution_id), "reason": reason},
        due_at=now,
        attempts=0,
        created_at=now,
    )
    session.add(outbox_event)

    return execution, True
