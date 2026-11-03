"""FastAPI Trip management routes."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_session, get_session_factory
from contracts.jobs import Job, JobStatus
from contracts.trips import Trip, TripBrief, TripState
from functions.api.models import TripModel, TripVersionModel
from functions.api.repository import SqlAlchemyUnitOfWork, TripVersionConflictError
from functions.api.schemas import (
    AcceptedJobResponse,
    CreateTripRequest,
    PlanRequest,
)
from functions.api.service import accept_composition_job
from functions.orchestrator.graphs.trip_planner.graph import trip_planning_workflow
from middleware.auth import AuthenticatedUser, get_current_user
from middleware.rate_limit import public_request_rate_limiter

router = APIRouter(
    prefix="/api/v1/trips",
    tags=["trips"],
    dependencies=[Depends(public_request_rate_limiter)],
)


def get_uow() -> SqlAlchemyUnitOfWork:
    return SqlAlchemyUnitOfWork(get_session_factory())


UowDep = Annotated[SqlAlchemyUnitOfWork, Depends(get_uow)]


# --- Request & Response DTOs ---


class UpdateBriefRequest(BaseModel):
    expected_base_version: int
    brief: TripBrief
    command_id: uuid.UUID = Field(default_factory=uuid.uuid4)


class TripResponse(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    current_version: int
    state: TripState
    brief: TripBrief | None = None


class PlanJobResponse(BaseModel):
    job_id: uuid.UUID
    trip_id: uuid.UUID
    version: int
    status: str
    result: dict[str, Any] | None = None


# --- Route Handlers ---


@router.post("", response_model=TripResponse, status_code=status.HTTP_201_CREATED)
async def create_trip(
    request: CreateTripRequest,
    uow: UowDep,
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> TripResponse:
    """Creates a new Trip aggregate and persists its initial version (v1)."""
    async with uow:
        trip = Trip(owner_id=current_user.id, current_version=0, state=TripState.DRAFT)
        await uow.trips.save(trip)

        command_id = uuid.uuid4()
        version = await uow.trips.commit_version(
            trip_id=trip.id,
            expected_base_version=0,
            brief=request.brief,
            command_id=command_id,
        )
        await uow.commit()

    return TripResponse(
        id=trip.id,
        owner_id=trip.owner_id,
        current_version=version.version,
        state=trip.state,
        brief=version.brief,
    )


@router.get("/{trip_id}", response_model=TripResponse)
async def get_trip(
    trip_id: uuid.UUID,
    uow: UowDep,
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> TripResponse:
    """Retrieves a Trip aggregate and its latest active brief snapshot."""
    async with uow:
        trip = await uow.trips.get_by_id(trip_id)
        if not trip or trip.owner_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Trip {trip_id} not found",
            )
        version_snapshot = await uow.trips.get_version(trip_id, trip.current_version)

    return TripResponse(
        id=trip.id,
        owner_id=trip.owner_id,
        current_version=trip.current_version,
        state=trip.state,
        brief=version_snapshot.brief if version_snapshot else None,
    )


@router.post("/{trip_id}/brief", response_model=TripResponse)
async def update_trip_brief(
    trip_id: uuid.UUID,
    request: UpdateBriefRequest,
    uow: UowDep,
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> TripResponse:
    """Updates the trip brief with atomic optimistic locking.

    Returns 409 Conflict if expected_base_version does not match current_version.
    Deduplicates repeated command_id requests idempotently.
    """
    async with uow:
        trip = await uow.trips.get_by_id(trip_id)
        if not trip or trip.owner_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Trip {trip_id} not found",
            )

        try:
            version = await uow.trips.commit_version(
                trip_id=trip_id,
                expected_base_version=request.expected_base_version,
                brief=request.brief,
                command_id=request.command_id,
            )
            await uow.commit()
        except TripVersionConflictError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(e),
            ) from e

    return TripResponse(
        id=trip.id,
        owner_id=trip.owner_id,
        current_version=version.version,
        state=trip.state,
        brief=version.brief,
    )


@router.post("/{trip_id}/plan", response_model=PlanJobResponse)
async def plan_trip(
    trip_id: uuid.UUID,
    uow: UowDep,
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> PlanJobResponse:
    """Triggers end-to-end trip planning workflow and enqueues a durable job."""
    async with uow:
        trip = await uow.trips.get_by_id(trip_id)
        if not trip or trip.owner_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Trip {trip_id} not found",
            )
        snapshot = await uow.trips.get_version(trip_id, trip.current_version)
        if not snapshot:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Trip {trip_id} has no valid brief snapshot",
            )

        job = Job(
            type="trip_planning",
            payload={"trip_id": str(trip_id), "version": trip.current_version},
            status=JobStatus.PENDING,
            idempotency_key=f"plan_{trip_id}_v{trip.current_version}",
        )
        enqueued_job = await uow.jobs.enqueue(job)
        await uow.commit()

    # Execute workflow graph
    workflow_state = {
        "trip_id": str(trip_id),
        "version": trip.current_version,
        "brief": snapshot.brief.model_dump(mode="json"),
    }
    result = await trip_planning_workflow.ainvoke(workflow_state)

    return PlanJobResponse(
        job_id=enqueued_job.id,
        trip_id=trip_id,
        version=trip.current_version,
        status="completed" if result.get("status") == "completed" else "failed",
        result=result.get("proposal"),
    )


@router.post(
    "/{trip_id}/plans",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_trip_plan(
    trip_id: uuid.UUID,
    request: PlanRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AcceptedJobResponse:
    """Accepts a trip planning job with owner-scoped idempotency reservation."""
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required for job acceptance.",
        )

    trip_stmt = select(TripModel).where(TripModel.id == trip_id)
    trip_res = await session.execute(trip_stmt)
    trip = trip_res.scalar_one_or_none()
    if not trip or trip.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trip {trip_id} not found",
        )

    version_stmt = select(TripVersionModel).where(
        TripVersionModel.trip_id == trip_id,
        TripVersionModel.version == trip.current_version,
    )
    version = (await session.execute(version_stmt)).scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Trip {trip_id} has no valid brief snapshot",
        )

    payload_dict = request.model_dump(mode="json")
    payload_dict.update(
        {
            "trip_id": str(trip_id),
            "version": trip.current_version,
            "brief": version.brief_json,
        }
    )
    response_json = await accept_composition_job(
        session=session,
        owner_id=current_user.id,
        operation=f"start_trip_plan:{trip_id}",
        idempotency_key=idempotency_key.strip(),
        payload=payload_dict,
        trip_id=trip_id,
    )
    return AcceptedJobResponse(**response_json)
