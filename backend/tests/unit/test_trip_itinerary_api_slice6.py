"""Unit tests for spec 0007 Slice 6: Itinerary and Weather REST API endpoints.

Validates AC-8, AC-10, and AC-11:
1. GET /api/v1/trips/{trip_id} returns trip aggregate, current_route_id, and status with tenant isolation.
2. GET /api/v1/trips/{trip_id}/itinerary returns proposal, schedule, budget, weather, and linked GeoJSON map.
3. GET /api/v1/trips/{trip_id}/itinerary returns 409 Conflict when planning job is active.
4. GET /api/v1/trips/{trip_id}/itinerary returns 404 Not Found when trip is missing or proposal unpublished.
5. GET /api/v1/trips/{trip_id}/itinerary returns 403 Forbidden on tenant mismatch.
6. Preserves partial and unavailable routing diagnostics without fabricating geometry.
7. GET /api/v1/trips/{trip_id}/weather returns corridor weather advisory with Open Meteo attribution.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from contracts.routing import (
    GeoJsonFeature,
    RouteMapGeoJsonResponse,
    RouteStatus,
    SolverStatus,
)
from contracts.trips import Trip, TripBrief, TripState, TripVersion
from functions.api.models import TripModel
from functions.api.repository import SqlAlchemyUnitOfWork
from functions.api.routes.trips import (
    get_trip,
    get_trip_itinerary,
    get_trip_weather,
)
from functions.orchestrator.routing.models import RouteModel
from middleware.auth import AuthenticatedUser


@pytest.fixture
def test_owner_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def other_owner_id() -> uuid.UUID:
    return uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def test_user(test_owner_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        id=test_owner_id,
        email="traveler@swena.internal",
    )


@pytest.fixture
def other_user(other_owner_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        id=other_owner_id,
        email="other@swena.internal",
    )


@pytest.fixture
def sample_proposal(test_owner_id: uuid.UUID) -> dict[str, Any]:
    trip_id = str(uuid.uuid4())
    route_id = str(uuid.uuid4())
    return {
        "trip_id": trip_id,
        "version": 1,
        "status": "ready_for_approval",
        "schedule": {
            "stops": [
                {
                    "position": 1,
                    "name": "Bengaluru Origin",
                    "latitude": 12.9716,
                    "longitude": 77.5946,
                    "arrival_time": "2026-11-14T09:00:00Z",
                    "departure_time": "2026-11-14T09:30:00Z",
                    "stay_duration_minutes": 30,
                    "is_hours_assumed": False,
                    "hours_status": "CONFIRMED",
                },
                {
                    "position": 2,
                    "name": "Mysuru Palace",
                    "latitude": 12.3051,
                    "longitude": 76.6551,
                    "arrival_time": "2026-11-14T12:00:00Z",
                    "departure_time": "2026-11-14T15:00:00Z",
                    "stay_duration_minutes": 180,
                    "is_hours_assumed": True,
                    "hours_status": "HOURS_ASSUMED",
                },
            ],
            "legs": [
                {
                    "from_stop": "Bengaluru Origin",
                    "to_stop": "Mysuru Palace",
                    "duration_minutes": 150,
                    "distance_km": 145.0,
                }
            ],
            "total_travel_minutes": 150,
            "is_optimal": True,
        },
        "budget_summary": {
            "known_total": {"amount": "4500.00", "currency": "INR"},
            "is_complete": False,
            "unknown_count": 1,
            "unknown_categories": ["tolls_and_fees"],
        },
        "budget_lines": [
            {
                "category": "transportation",
                "amount": "3500.00",
                "currency": "INR",
                "basis": "fuel_estimate",
                "quantity": "1",
                "estimated": True,
                "unknown_reason": None,
            },
            {
                "category": "activities",
                "amount": "1000.00",
                "currency": "INR",
                "basis": "entry_fee_mysuru_palace",
                "quantity": "1",
                "estimated": False,
                "unknown_reason": None,
            },
            {
                "category": "tolls_and_fees",
                "amount": None,
                "currency": "INR",
                "basis": "expressway_toll",
                "quantity": "1",
                "estimated": False,
                "unknown_reason": "Toll rates unverified between Bengaluru and Mysuru",
            },
        ],
        "editorial_recommendations": [
            {
                "name": "Ranganathittu Bird Sanctuary",
                "category": "nature",
                "fit_score": 0.94,
                "uncertainty": "EDITORIAL_DISCOVERY",
                "description": "Nesting bird sanctuary along Kaveri river",
                "source": "KSTDC Verified",
            }
        ],
        "excluded_candidates": [],
        "weather_advisories": [
            {
                "stop_name": "Bengaluru Origin",
                "latitude": 12.9716,
                "longitude": 77.5946,
                "temperature_celsius": 24.5,
                "condition": "sunny",
                "humidity_percent": 55,
                "monsoon_risk": "low",
            },
            {
                "stop_name": "Mysuru Palace",
                "latitude": 12.3051,
                "longitude": 76.6551,
                "temperature_celsius": 27.0,
                "condition": "cloudy",
                "humidity_percent": 62,
                "monsoon_risk": "low",
            },
        ],
        "weather_status": "AVAILABLE",
        "validated_candidates": [],
        "discovered_places": [],
        "route_id": route_id,
        "route_status": "OPTIMAL",
        "routing_status": "OPTIMAL",
        "route_diagnostics": [],
        "provider_diagnostics": {
            "weather": {"provider": "open_meteo", "horizon_days": 16}
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }


# =====================================================================
# Unit Tests: GET /api/v1/trips/{trip_id}
# =====================================================================


@pytest.mark.asyncio
async def test_get_trip_returns_route_id_and_status(
    test_owner_id: uuid.UUID,
    test_user: AuthenticatedUser,
) -> None:
    trip_id = uuid.uuid4()
    route_id = uuid.uuid4()
    mock_trip = Trip(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=2,
        state=TripState.PROPOSED,
        current_route_id=route_id,
    )
    mock_version = TripVersion(
        trip_id=trip_id,
        version=2,
        brief=TripBrief(
            origin_name="Bengaluru",
            origin_lat=12.9716,
            origin_lng=77.5946,
            destinations=[{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394}],
            start_date=date(2026, 11, 14),
            end_date=date(2026, 11, 16),
        ),
        command_id=uuid.uuid4(),
    )

    mock_uow = MagicMock(spec=SqlAlchemyUnitOfWork)
    mock_uow.__aenter__.return_value = mock_uow
    mock_uow.trips = MagicMock()
    mock_uow.trips.get_by_id = AsyncMock(return_value=mock_trip)
    mock_uow.trips.get_version = AsyncMock(return_value=mock_version)

    response = await get_trip(
        trip_id=trip_id,
        uow=mock_uow,
        current_user=test_user,
    )

    assert response.id == trip_id
    assert response.owner_id == test_owner_id
    assert response.current_route_id == route_id
    assert response.state == TripState.PROPOSED
    assert response.status == "proposed"
    assert response.brief is not None
    assert response.brief.origin_name == "Bengaluru"


@pytest.mark.asyncio
async def test_get_trip_enforces_tenant_isolation(
    test_owner_id: uuid.UUID,
    other_user: AuthenticatedUser,
) -> None:
    trip_id = uuid.uuid4()
    mock_trip = Trip(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=1,
        state=TripState.DRAFT,
    )

    mock_uow = MagicMock(spec=SqlAlchemyUnitOfWork)
    mock_uow.__aenter__.return_value = mock_uow
    mock_uow.trips = MagicMock()
    mock_uow.trips.get_by_id = AsyncMock(return_value=mock_trip)

    with pytest.raises(HTTPException) as exc_info:
        await get_trip(
            trip_id=trip_id,
            uow=mock_uow,
            current_user=other_user,
        )
    assert exc_info.value.status_code == 403
    assert "belongs to another tenant" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_trip_returns_404_when_missing(
    test_user: AuthenticatedUser,
) -> None:
    trip_id = uuid.uuid4()
    mock_uow = MagicMock(spec=SqlAlchemyUnitOfWork)
    mock_uow.__aenter__.return_value = mock_uow
    mock_uow.trips = MagicMock()
    mock_uow.trips.get_by_id = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc_info:
        await get_trip(
            trip_id=trip_id,
            uow=mock_uow,
            current_user=test_user,
        )
    assert exc_info.value.status_code == 404
    assert f"Trip {trip_id} not found" in exc_info.value.detail


# =====================================================================
# Unit Tests: GET /api/v1/trips/{trip_id}/itinerary
# =====================================================================


@pytest.mark.asyncio
async def test_get_trip_itinerary_happy_path(
    test_owner_id: uuid.UUID,
    test_user: AuthenticatedUser,
    sample_proposal: dict[str, Any],
) -> None:
    trip_id = uuid.UUID(sample_proposal["trip_id"])
    route_id = uuid.UUID(sample_proposal["route_id"])

    trip_model = TripModel(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=1,
        state="proposed",
        current_route_id=route_id,
        itinerary_json=sample_proposal,
    )

    mock_session = AsyncMock()
    mock_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=trip_model)
    )

    # Mock RouteRepository and assemble_route_map_geojson
    mock_route = MagicMock(spec=RouteModel)
    mock_route.status = "FEASIBLE"
    mock_geojson = RouteMapGeoJsonResponse(
        features=[
            GeoJsonFeature(
                geometry={"type": "Point", "coordinates": [77.5946, 12.9716]},
                properties={"feature_type": "stop", "name": "Bengaluru Origin"},
            )
        ],
        total_distance_m=145000,
        total_travel_seconds=9000,
        status=RouteStatus.FEASIBLE,
        solver_status=SolverStatus.OPTIMAL,
    )

    with (
        patch("functions.orchestrator.routing.repository.RouteRepository.get_route_by_id", new_callable=AsyncMock) as mock_get_route,
        patch("functions.api.routes.trips.assemble_route_map_geojson", return_value=mock_geojson) as mock_assemble,
    ):
        mock_get_route.return_value = mock_route

        res = await get_trip_itinerary(
            trip_id=trip_id,
            session=mock_session,
            current_user=test_user,
        )

        assert res.trip_id == trip_id
        assert res.state == "proposed"
        assert res.status == "ready_for_approval"
        assert res.route_id == route_id
        assert res.schedule is not None
        assert len(res.schedule["stops"]) == 2
        assert res.schedule["stops"][1]["hours_status"] == "HOURS_ASSUMED"
        assert res.map_geojson is not None
        assert mock_assemble.called
        assert len(res.budget_lines) == 3
        # Unknown cost line item check
        unknown_line = next(b for b in res.budget_lines if b["category"] == "tolls_and_fees")
        assert unknown_line["amount"] is None
        assert "Toll rates unverified" in unknown_line["unknown_reason"]


@pytest.mark.asyncio
async def test_get_trip_itinerary_tenant_isolation_403(
    test_owner_id: uuid.UUID,
    other_user: AuthenticatedUser,
    sample_proposal: dict[str, Any],
) -> None:
    trip_id = uuid.UUID(sample_proposal["trip_id"])
    trip_model = TripModel(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=1,
        state="proposed",
        itinerary_json=sample_proposal,
    )
    mock_session = AsyncMock()
    mock_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=trip_model)
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_trip_itinerary(
            trip_id=trip_id,
            session=mock_session,
            current_user=other_user,
        )
    assert exc_info.value.status_code == 403
    assert "belongs to another tenant" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_trip_itinerary_returns_409_when_planning_in_progress(
    test_owner_id: uuid.UUID,
    test_user: AuthenticatedUser,
) -> None:
    trip_id = uuid.uuid4()
    trip_model = TripModel(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=1,
        state="planning",
        itinerary_json=None,
    )
    mock_session = AsyncMock()
    # First query returns trip_model, second query returns active execution id
    trip_exec_res = MagicMock(scalar_one_or_none=MagicMock(return_value=trip_model))
    active_exec_res = MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=uuid.uuid4()))))
    mock_session.execute.side_effect = [trip_exec_res, active_exec_res]

    with pytest.raises(HTTPException) as exc_info:
        await get_trip_itinerary(
            trip_id=trip_id,
            session=mock_session,
            current_user=test_user,
        )
    assert exc_info.value.status_code == 409
    assert "Trip planning is in progress" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_trip_itinerary_returns_404_when_draft_without_proposal(
    test_owner_id: uuid.UUID,
    test_user: AuthenticatedUser,
) -> None:
    trip_id = uuid.uuid4()
    trip_model = TripModel(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=1,
        state="draft",
        itinerary_json=None,
    )
    mock_session = AsyncMock()
    trip_exec_res = MagicMock(scalar_one_or_none=MagicMock(return_value=trip_model))
    active_exec_res = MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None))))
    mock_session.execute.side_effect = [trip_exec_res, active_exec_res]

    with pytest.raises(HTTPException) as exc_info:
        await get_trip_itinerary(
            trip_id=trip_id,
            session=mock_session,
            current_user=test_user,
        )
    assert exc_info.value.status_code == 404
    assert "No proposal has been published" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_trip_itinerary_handles_routing_unavailable(
    test_owner_id: uuid.UUID,
    test_user: AuthenticatedUser,
    sample_proposal: dict[str, Any],
) -> None:
    trip_id = uuid.UUID(sample_proposal["trip_id"])
    partial_proposal = dict(sample_proposal)
    partial_proposal["route_id"] = None
    partial_proposal["route_status"] = "UNAVAILABLE"
    partial_proposal["routing_status"] = "ROUTING_UNAVAILABLE"
    partial_proposal["route_diagnostics"] = [
        "Road routing engine unavailable; stops preserved without driving schedule or route polyline"
    ]

    trip_model = TripModel(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=1,
        state="proposed",
        current_route_id=None,
        itinerary_json=partial_proposal,
    )
    mock_session = AsyncMock()
    mock_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=trip_model)
    )

    res = await get_trip_itinerary(
        trip_id=trip_id,
        session=mock_session,
        current_user=test_user,
    )

    assert res.trip_id == trip_id
    assert res.route_id is None
    assert res.routing_status == "ROUTING_UNAVAILABLE"
    assert res.map_geojson is None
    assert len(res.route_diagnostics) == 1
    assert "Road routing engine unavailable" in res.route_diagnostics[0]


@pytest.mark.asyncio
async def test_get_trip_itinerary_handles_infeasible_route(
    test_owner_id: uuid.UUID,
    test_user: AuthenticatedUser,
    sample_proposal: dict[str, Any],
) -> None:
    trip_id = uuid.UUID(sample_proposal["trip_id"])
    infeasible_proposal = dict(sample_proposal)
    infeasible_proposal["route_status"] = "INFEASIBLE"
    infeasible_proposal["route_diagnostics"] = [
        "Overconstrained stop time windows; stop 2 dropped from schedule"
    ]

    trip_model = TripModel(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=1,
        state="proposed",
        current_route_id=None,
        itinerary_json=infeasible_proposal,
    )
    mock_session = AsyncMock()
    mock_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=trip_model)
    )

    res = await get_trip_itinerary(
        trip_id=trip_id,
        session=mock_session,
        current_user=test_user,
    )

    assert res.route_status == "INFEASIBLE"
    assert len(res.route_diagnostics) == 1
    assert "Overconstrained" in res.route_diagnostics[0]


# =====================================================================
# Unit Tests: GET /api/v1/trips/{trip_id}/weather
# =====================================================================


@pytest.mark.asyncio
async def test_get_trip_weather_happy_path(
    test_owner_id: uuid.UUID,
    test_user: AuthenticatedUser,
    sample_proposal: dict[str, Any],
) -> None:
    trip_id = uuid.UUID(sample_proposal["trip_id"])
    trip_model = TripModel(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=1,
        state="proposed",
        itinerary_json=sample_proposal,
    )
    mock_session = AsyncMock()
    mock_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=trip_model)
    )

    res = await get_trip_weather(
        trip_id=trip_id,
        session=mock_session,
        current_user=test_user,
    )

    assert res.trip_id == trip_id
    assert res.weather_status == "AVAILABLE"
    assert res.attribution == "Weather data by Open Meteo.com"
    assert len(res.stops) == 2
    assert res.stops[0]["stop_name"] == "Bengaluru Origin"


@pytest.mark.asyncio
async def test_get_trip_weather_tenant_isolation_403(
    test_owner_id: uuid.UUID,
    other_user: AuthenticatedUser,
    sample_proposal: dict[str, Any],
) -> None:
    trip_id = uuid.UUID(sample_proposal["trip_id"])
    trip_model = TripModel(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=1,
        state="proposed",
        itinerary_json=sample_proposal,
    )
    mock_session = AsyncMock()
    mock_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=trip_model)
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_trip_weather(
            trip_id=trip_id,
            session=mock_session,
            current_user=other_user,
        )
    assert exc_info.value.status_code == 403
    assert "belongs to another tenant" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_trip_weather_returns_404_when_missing_or_unpublished(
    test_owner_id: uuid.UUID,
    test_user: AuthenticatedUser,
) -> None:
    trip_id = uuid.uuid4()
    trip_model = TripModel(
        id=trip_id,
        owner_id=test_owner_id,
        current_version=1,
        state="draft",
        itinerary_json=None,
    )
    mock_session = AsyncMock()
    mock_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=trip_model)
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_trip_weather(
            trip_id=trip_id,
            session=mock_session,
            current_user=test_user,
        )
    assert exc_info.value.status_code == 404
    assert "No proposal has been published" in exc_info.value.detail
