"""FastAPI Trip management routes."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from travel.application.ports.repository import TripVersionConflictError
from travel.domain.jobs import Job, JobStatus
from travel.domain.trips import Trip, TripBrief, TripState
from travel.infrastructure.config import settings
from travel.persistence.unit_of_work import SqlAlchemyUnitOfWork
from travel.workflows.trip_planning import trip_planning_workflow

router = APIRouter(prefix="/api/v1/trips", tags=["trips"])

# Database session factory
engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_uow() -> SqlAlchemyUnitOfWork:
    return SqlAlchemyUnitOfWork(async_session_factory)


UowDep = Annotated[SqlAlchemyUnitOfWork, Depends(get_uow)]


# --- Request & Response DTOs ---


class CreateTripRequest(BaseModel):
    owner_id: uuid.UUID
    brief: TripBrief


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
) -> TripResponse:
    """Creates a new Trip aggregate and persists its initial version (v1)."""
    async with uow:
        trip = Trip(owner_id=request.owner_id, current_version=0, state=TripState.DRAFT)
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
) -> TripResponse:
    """Retrieves a Trip aggregate and its latest active brief snapshot."""
    async with uow:
        trip = await uow.trips.get_by_id(trip_id)
        if not trip:
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
) -> TripResponse:
    """Updates the trip brief with atomic optimistic locking.

    Returns 409 Conflict if expected_base_version does not match current_version.
    Deduplicates repeated command_id requests idempotently.
    """
    async with uow:
        trip = await uow.trips.get_by_id(trip_id)
        if not trip:
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
) -> PlanJobResponse:
    """Triggers end-to-end trip planning workflow and enqueues a durable job."""
    async with uow:
        trip = await uow.trips.get_by_id(trip_id)
        if not trip:
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
