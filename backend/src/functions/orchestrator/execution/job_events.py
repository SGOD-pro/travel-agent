"""Job events recording and timeline queries conforming to spec 0001 and DATABASE.md."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from functions.orchestrator.execution.models import JobEventModel


async def record_job_event(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    event_type: str,
    safe_payload: dict[str, Any] | None = None,
) -> JobEventModel:
    """Transactionally records a timeline event with monotonic sequence allocation."""
    seq_stmt = select(sa.func.coalesce(sa.func.max(JobEventModel.sequence), 0) + 1).where(
        JobEventModel.execution_id == execution_id
    )
    next_seq = (await session.execute(seq_stmt)).scalar() or 1

    event = JobEventModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        sequence=next_seq,
        event_type=event_type,
        safe_payload=safe_payload or {},
        timestamp=datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    return event


async def get_job_events(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    cursor: int | None = None,
    limit: int = 50,
) -> tuple[list[JobEventModel], int | None]:
    """Retrieves paginated timeline events for an execution using sequence cursor.

    Args:
        session: Active database session.
        execution_id: Execution identifier.
        cursor: Sequence number after which to fetch (sequence > cursor).
        limit: Max events to return (bounded between 1 and 100).

    Returns:
        tuple of (events_list, next_cursor)
    """
    clamped_limit = max(1, min(limit, 100))
    stmt = select(JobEventModel).where(JobEventModel.execution_id == execution_id)
    if cursor is not None:
        stmt = stmt.where(JobEventModel.sequence > cursor)
    stmt = stmt.order_by(JobEventModel.sequence.asc()).limit(clamped_limit + 1)

    rows = list((await session.execute(stmt)).scalars().all())
    if len(rows) > clamped_limit:
        events = rows[:clamped_limit]
        next_cursor = events[-1].sequence
    else:
        events = rows
        next_cursor = None

    return events, next_cursor
