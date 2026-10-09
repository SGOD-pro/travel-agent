"""FastAPI routes for orchestrator job status, timeline polling, and cancellation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_session
from functions.orchestrator.execution.cancellation import cancel_execution
from functions.orchestrator.execution.job_events import get_job_events
from functions.orchestrator.execution.models import ExecutionModel, TaskModel
from middleware.auth import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


class JobEventDTO(BaseModel):
    """Client-safe timeline event DTO."""

    id: uuid.UUID
    sequence: int
    event_type: str
    safe_payload: dict[str, Any]
    timestamp: datetime


class JobStatusResponse(BaseModel):
    """Job status, progress, and timeline polling response conforming to API.md."""

    job_id: uuid.UUID
    status: str
    context_version: int
    progress: dict[str, Any] = Field(default_factory=dict)
    missing_inputs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    result_available: bool = False
    retry_after_seconds: int = 0
    timeline: list[JobEventDTO] = Field(default_factory=list)
    next_cursor: str | None = None


class JobEventsPageResponse(BaseModel):
    """Paginated timeline events response conforming to API.md."""

    events: list[JobEventDTO]
    next_cursor: str | None = None


class CancelJobRequest(BaseModel):
    """Optional payload for job cancellation."""

    reason: str | None = Field(default="User cancellation request", max_length=255)


class CancelJobResponse(BaseModel):
    """Cancellation confirmation response."""

    job_id: uuid.UUID
    status: str
    cancelled: bool


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    cursor: Annotated[int | None, Query(description="Sequence cursor for timeline pagination")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Max timeline events to return")] = 50,
) -> JobStatusResponse:
    """Retrieves owner-scoped job status, progress, and initial timeline events."""
    exec_stmt = select(ExecutionModel).where(ExecutionModel.id == job_id)
    execution = (await session.execute(exec_stmt)).scalar_one_or_none()

    if execution is None or execution.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found",
        )

    # Compute task progress counts
    task_counts_stmt = (
        select(
            sa.func.count(TaskModel.id).label("total"),
            sa.func.count(TaskModel.id).filter(TaskModel.status == "COMPLETED").label("completed"),
            sa.func.count(TaskModel.id).filter(TaskModel.status == "FAILED").label("failed"),
        ).where(TaskModel.execution_id == job_id)
    )
    counts = (await session.execute(task_counts_stmt)).first()
    total_tasks = counts.total if counts else 0
    completed_tasks = counts.completed if counts else 0
    failed_tasks = counts.failed if counts else 0

    progress: dict[str, Any] = {
        "tasks_total": total_tasks,
        "tasks_completed": completed_tasks,
        "tasks_failed": failed_tasks,
        "workflow": execution.workflow,
    }

    # Fetch timeline events
    events, next_seq = await get_job_events(session, execution_id=job_id, cursor=cursor, limit=limit)
    event_dtos = [
        JobEventDTO(
            id=e.id,
            sequence=e.sequence,
            event_type=e.event_type,
            safe_payload=e.safe_payload,
            timestamp=e.timestamp,
        )
        for e in events
    ]

    is_active = execution.status in ("CREATED", "RUNNING", "WAITING_TASKS", "WAITING_USER")
    retry_after = 5 if is_active else 0
    result_available = execution.status == "SUCCEEDED"

    error_info = None
    if execution.status == "FAILED":
        error_info = {
            "code": "EXECUTION_FAILED",
            "message": "Workflow execution failed or deadline exceeded",
        }

    return JobStatusResponse(
        job_id=execution.id,
        status=execution.status,
        context_version=execution.context_version,
        progress=progress,
        missing_inputs=[],
        warnings=[],
        error=error_info,
        result_available=result_available,
        retry_after_seconds=retry_after,
        timeline=event_dtos,
        next_cursor=str(next_seq) if next_seq is not None else None,
    )


@router.get("/{job_id}/events", response_model=JobEventsPageResponse)
async def get_job_timeline_events(
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    cursor: Annotated[int | None, Query(description="Sequence cursor for timeline pagination")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Max timeline events to return")] = 50,
) -> JobEventsPageResponse:
    """Retrieves paginated timeline events for an execution."""
    exec_stmt = select(ExecutionModel).where(ExecutionModel.id == job_id)
    execution = (await session.execute(exec_stmt)).scalar_one_or_none()

    if execution is None or execution.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found",
        )

    events, next_seq = await get_job_events(session, execution_id=job_id, cursor=cursor, limit=limit)
    event_dtos = [
        JobEventDTO(
            id=e.id,
            sequence=e.sequence,
            event_type=e.event_type,
            safe_payload=e.safe_payload,
            timestamp=e.timestamp,
        )
        for e in events
    ]

    return JobEventsPageResponse(
        events=event_dtos,
        next_cursor=str(next_seq) if next_seq is not None else None,
    )


@router.post("/{job_id}/cancel", response_model=CancelJobResponse)
async def cancel_job(
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    request: CancelJobRequest | None = None,
) -> CancelJobResponse:
    """Cancels a pending or running job with owner verification and Transaction 8 fencing."""
    reason = request.reason if request and request.reason else "User cancellation request"

    execution, was_cancelled = await cancel_execution(
        session,
        execution_id=job_id,
        owner_id=current_user.id,
        reason=reason,
    )

    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found",
        )

    await session.commit()

    return CancelJobResponse(
        job_id=execution.id,
        status=execution.status,
        cancelled=was_cancelled,
    )
