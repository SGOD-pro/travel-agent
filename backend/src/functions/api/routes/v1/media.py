# backend/src/functions/api/routes/v1/media.py
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_session
from contracts.media import LocationCandidate, MediaLocationGraph
from functions.orchestrator.execution.models import ExecutionModel
from middleware.auth import AuthenticatedUser, get_current_user
from middleware.rate_limit import public_request_rate_limiter

router = APIRouter(
    prefix="/api/v1/media",
    tags=["Media Location Graph"],
    dependencies=[Depends(public_request_rate_limiter), Depends(get_current_user)],
)


@router.get("/location/{job_id}", response_model=MediaLocationGraph)
async def get_media_location_graph(
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> MediaLocationGraph:
    """Return an explicit unavailable response until media results are persisted."""
    owner_id = (
        await session.execute(
            select(ExecutionModel.owner_id).where(ExecutionModel.id == job_id)
        )
    ).scalar_one_or_none()
    if owner_id is None or owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Media location results are not persisted yet; poll the job status instead.",
    )


@router.get("/candidates/{job_id}/explain", response_model=list[LocationCandidate])
async def explain_candidates(job_id: str) -> list[LocationCandidate]:
    """
    Phase 11 Gate Endpoint:
    Returns ALL candidate matches with full evidence breakdowns,
    allowing manual review of explanations rather than just the top-1 guess.
    """
    raise HTTPException(
        status_code=501,
        detail="Candidate explanations are not available until media result persistence is implemented.",
    )
