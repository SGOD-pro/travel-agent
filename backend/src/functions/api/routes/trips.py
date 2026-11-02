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
from contracts.routing import RouteMapGeoJsonResponse
from contracts.trips import Trip, TripBrief, TripState
from functions.api.idempotency import IdempotencyConflictError, IdempotencyRepository
from functions.api.models import TripModel
from functions.api.repository import SqlAlchemyUnitOfWork, TripVersionConflictError
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.graphs.trip_planner.graph import trip_planning_workflow
from functions.orchestrator.routing.geojson import assemble_route_map_geojson
from functions.orchestrator.routing.repository import RouteRepository
from middleware.auth import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/api/v1/trips", tags=["trips"])


def get_uow() -> SqlAlchemyUnitOfWork:
    return SqlAlchemyUnitOfWork(get_session_factory())


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
    status: str | None = None
    brief: TripBrief | None = None
    current_route_id: uuid.UUID | None = None


class ItineraryResponse(BaseModel):
    trip_id: uuid.UUID
    version: int = 1
    state: str = "proposed"
    status: str = "ready_for_approval"
    schedule: dict[str, Any] | None = None
    budget_summary: dict[str, Any] | None = None
    budget_lines: list[dict[str, Any]] = Field(default_factory=list)
    editorial_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    excluded_candidates: list[dict[str, Any]] = Field(default_factory=list)
    weather_advisories: list[dict[str, Any]] = Field(default_factory=list)
    weather_status: str = "WEATHER_UNAVAILABLE"
    validated_candidates: list[dict[str, Any]] = Field(default_factory=list)
    discovered_places: list[dict[str, Any]] = Field(default_factory=list)
    route_id: uuid.UUID | None = None
    route_status: str | None = None
    routing_status: str | None = None
    route_diagnostics: list[str] = Field(default_factory=list)
    provider_diagnostics: dict[str, Any] = Field(default_factory=dict)
    map_geojson: RouteMapGeoJsonResponse | dict[str, Any] | None = None
    generated_at: str | None = None


class TripWeatherResponse(BaseModel):
    trip_id: uuid.UUID
    weather_status: str = "WEATHER_UNAVAILABLE"
    weather_advisories: list[dict[str, Any]] = Field(default_factory=list)
    overall_caution: str | None = None
    ghat_caution: str | None = None
    attribution: str = "Weather data by Open Meteo.com"
    stops: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class PlanJobResponse(BaseModel):
    job_id: uuid.UUID
    trip_id: uuid.UUID
    version: int
    status: str
    result: dict[str, Any] | None = None


class StartPlanRequest(BaseModel):
    planning_options: dict[str, Any] = Field(default_factory=dict)
    workflow: str = "TRAVEL"


class AcceptedPlanResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    status_url: str


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
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> TripResponse:
    """Retrieves a Trip aggregate and its latest active brief snapshot."""
    async with uow:
        trip = await uow.trips.get_by_id(trip_id)
        if not trip:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Trip {trip_id} not found",
            )
        if trip.owner_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access forbidden: trip {trip_id} belongs to another tenant",
            )
        version_snapshot = await uow.trips.get_version(trip_id, trip.current_version)

    return TripResponse(
        id=trip.id,
        owner_id=trip.owner_id,
        current_version=trip.current_version,
        state=trip.state,
        status=trip.state.value if hasattr(trip.state, "value") else str(trip.state),
        brief=version_snapshot.brief if version_snapshot else None,
        current_route_id=trip.current_route_id,
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


@router.post(
    "/{trip_id}/plans",
    response_model=AcceptedPlanResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_trip_plan(
    trip_id: uuid.UUID,
    request: StartPlanRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AcceptedPlanResponse:
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

    idempotency_repo = IdempotencyRepository(session)
    payload_dict = {
        "trip_id": str(trip_id),
        **request.model_dump(mode="json"),
    }
    try:
        response_json, status_code_res, is_cached = await idempotency_repo.accept_job(
            owner_id=current_user.id,
            operation=f"start_trip_plan:{trip_id}",
            idempotency_key=idempotency_key.strip(),
            payload=payload_dict,
            trip_id=trip_id,
            workflow=request.workflow,
        )
    except IdempotencyConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        ) from e

    return AcceptedPlanResponse(**response_json)


@router.get("/{trip_id}/itinerary", response_model=ItineraryResponse)
async def get_trip_itinerary(
    trip_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> ItineraryResponse:
    """Retrieves synthesized travel itinerary proposal with linked route map and diagnostics."""
    trip_stmt = select(TripModel).where(TripModel.id == trip_id)
    trip_res = await session.execute(trip_stmt)
    trip = trip_res.scalar_one_or_none()

    if not trip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trip {trip_id} not found",
        )

    if trip.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access forbidden: trip {trip_id} belongs to another tenant",
        )

    if not trip.itinerary_json:
        # Check if planning job is in progress
        active_exec_stmt = select(ExecutionModel.id).where(
            ExecutionModel.trip_id == trip_id,
            ExecutionModel.status.in_(["CREATED", "RUNNING", "WAITING_TASKS"]),
        )
        active_exec = (await session.execute(active_exec_stmt)).scalars().first()
        if active_exec or trip.state == "planning":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Trip planning is in progress",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Itinerary not found. No proposal has been published.",
        )

    proposal = dict(trip.itinerary_json)

    # Attach GeoJSON route map if current_route_id is linked
    map_geojson = None
    if trip.current_route_id:
        route_repo = RouteRepository(session)
        route_model = await route_repo.get_route_by_id(
            trip.current_route_id, owner_id=current_user.id
        )
        if route_model is not None:
            map_geojson = assemble_route_map_geojson(route_model)

    return ItineraryResponse(
        trip_id=trip.id,
        version=proposal.get("version", trip.current_version),
        state=trip.state,
        status=proposal.get("status", "ready_for_approval"),
        schedule=proposal.get("schedule"),
        budget_summary=proposal.get("budget_summary"),
        budget_lines=proposal.get("budget_lines", []),
        editorial_recommendations=proposal.get("editorial_recommendations", []),
        excluded_candidates=proposal.get("excluded_candidates", []),
        weather_advisories=proposal.get("weather_advisories", []),
        weather_status=proposal.get("weather_status", "WEATHER_UNAVAILABLE"),
        validated_candidates=proposal.get("validated_candidates", []),
        discovered_places=proposal.get("discovered_places", []),
        route_id=trip.current_route_id or proposal.get("route_id"),
        route_status=proposal.get("route_status"),
        routing_status=proposal.get("routing_status"),
        route_diagnostics=proposal.get("route_diagnostics", []),
        provider_diagnostics=proposal.get("provider_diagnostics", {}),
        map_geojson=map_geojson,
        generated_at=proposal.get("generated_at"),
    )


@router.get("/{trip_id}/weather", response_model=TripWeatherResponse)
async def get_trip_weather(
    trip_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> TripWeatherResponse:
    """Retrieves corridor weather advisory report with Open Meteo attribution."""
    trip_stmt = select(TripModel).where(TripModel.id == trip_id)
    trip_res = await session.execute(trip_stmt)
    trip = trip_res.scalar_one_or_none()

    if not trip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trip {trip_id} not found",
        )

    if trip.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access forbidden: trip {trip_id} belongs to another tenant",
        )

    if not trip.itinerary_json:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Weather advisory not found. No proposal has been published.",
        )

    proposal = dict(trip.itinerary_json)
    weather_advisories = proposal.get("weather_advisories", [])
    weather_status = proposal.get("weather_status", "WEATHER_UNAVAILABLE")
    provider_diagnostics = proposal.get("provider_diagnostics", {})

    stops: list[dict[str, Any]] = []
    overall_caution: str | None = None
    ghat_caution: str | None = None

    for adv in weather_advisories:
        if isinstance(adv, dict):
            if "stop_name" in adv or "stopName" in adv:
                stops.append(adv)
            if adv.get("ghat_caution") and not ghat_caution:
                ghat_caution = adv.get("ghat_caution")
            if adv.get("overall_caution") and not overall_caution:
                overall_caution = adv.get("overall_caution")

    return TripWeatherResponse(
        trip_id=trip.id,
        weather_status=weather_status,
        weather_advisories=weather_advisories,
        overall_caution=overall_caution,
        ghat_caution=ghat_caution,
        attribution="Weather data by Open Meteo.com",
        stops=stops,
        diagnostics=provider_diagnostics.get("weather", {}),
    )
