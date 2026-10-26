"""Unit tests for RouteMap GeoJSON assembler and infeasible replan stop presentation.

Validates:
1. Fallback skip_reason code MANDATORY_CONSTRAINT_INFEASIBLE when assumptions is empty.
2. Completed stops retain is_completed=True and green marker on infeasible routes.
3. replan_and_persist preserves is_completed=True for finished stops on infeasible replans.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import Point as ShapelyPoint

from contracts.routing import (
    Point,
    RouteReplanRequest,
    RouteStatus,
    SolverStatus,
)
from functions.orchestrator.routing.geojson import assemble_route_map_geojson
from functions.orchestrator.routing.models import RouteModel, RouteStopModel
from functions.route_optimizer.replan import RouteReplanService


def _create_mock_route_stop(
    stop_id: uuid.UUID,
    order: int,
    name: str,
    lon: float,
    lat: float,
    is_completed: bool = False,
    is_skipped: bool = False,
    skip_reason: str | None = None,
) -> RouteStopModel:
    pt = ShapelyPoint(lon, lat)
    stop = RouteStopModel(
        id=stop_id,
        route_id=uuid.uuid4(),
        stop_order=order,
        name=name,
        location=from_shape(pt, srid=4326),
        stop_type="DESTINATION",
        is_must_visit=True,
        is_completed=is_completed,
        is_skipped=is_skipped,
        skip_reason=skip_reason,
        dwell_seconds=1800,
        wait_seconds=0,
    )
    return stop


def test_assemble_route_map_geojson_fallback_skip_reason_and_completed_pins() -> None:
    """Verifies that infeasible routes provide a fallback skip_reason and render completed stops green."""
    route_id = uuid.uuid4()
    completed_id = uuid.uuid4()
    infeasible_id = uuid.uuid4()

    stop_completed = _create_mock_route_stop(
        completed_id, 1, "Completed Temple", 88.34, 22.54, is_completed=True, is_skipped=False
    )
    stop_infeasible = _create_mock_route_stop(
        infeasible_id, 2, "Infeasible Venue", 88.36, 22.56, is_completed=False, is_skipped=True
    )

    route = RouteModel(
        id=route_id,
        owner_id=uuid.uuid4(),
        route_version=2,
        status=RouteStatus.INFEASIBLE.value,
        solver_status=SolverStatus.INFEASIBLE.value,
        objective="FASTEST",
        departure_at=datetime(2026, 10, 26, 9, 0, tzinfo=UTC),
        total_distance_m=0,
        total_travel_seconds=0,
        stops=[stop_completed, stop_infeasible],
        legs=[],
        restrictions=[],
        assumptions=[],  # Empty assumptions
        warnings=[],
    )

    geojson_resp = assemble_route_map_geojson(route)

    assert geojson_resp.status == RouteStatus.INFEASIBLE
    assert len(geojson_resp.features) == 2

    # Stop 1 (completed)
    feat1 = next(f for f in geojson_resp.features if f.properties["id"] == str(completed_id))
    assert feat1.properties["is_completed"] is True
    assert feat1.properties["is_skipped"] is False
    assert feat1.properties["marker-color"] == "#059669"
    assert feat1.properties["skip_reason"] is None

    # Stop 2 (infeasible uncompleted)
    feat2 = next(f for f in geojson_resp.features if f.properties["id"] == str(infeasible_id))
    assert feat2.properties["is_completed"] is False
    assert feat2.properties["is_skipped"] is True
    assert feat2.properties["marker-color"] == "#dc2626"
    assert feat2.properties["skip_reason"] == "MANDATORY_CONSTRAINT_INFEASIBLE"


@pytest.mark.asyncio
async def test_replan_and_persist_preserves_completed_stops_on_infeasible_outcome() -> None:
    """Verifies that replan_and_persist marks completed stops as is_completed=True and unvisited stops as skipped."""
    owner_id = uuid.uuid4()
    route_id = uuid.uuid4()
    completed_stop_id = uuid.uuid4()
    infeasible_stop_id = uuid.uuid4()

    stop1 = _create_mock_route_stop(
        completed_stop_id, 1, "Completed Cafe", 88.34, 22.54, is_completed=False
    )
    stop2 = _create_mock_route_stop(
        infeasible_stop_id, 2, "Mandatory Closed Venue", 88.36, 22.56, is_completed=False
    )

    prev_route_model = RouteModel(
        id=route_id,
        owner_id=owner_id,
        route_version=1,
        status=RouteStatus.FEASIBLE.value,
        solver_status=SolverStatus.OPTIMAL.value,
        objective="FASTEST",
        departure_at=datetime(2026, 10, 26, 9, 0, tzinfo=UTC),
        fuel_currency="INR",
        total_distance_m=0,
        total_travel_seconds=0,
        total_dwell_seconds=0,
        total_wait_seconds=0,
        total_elapsed_seconds=0,
        driving_distance_m=0,
        walking_distance_m=0,
        stops=[stop1, stop2],
        legs=[],
        restrictions=[],
    )

    mock_repo = AsyncMock()
    mock_repo.get_route_by_id.return_value = prev_route_model
    mock_repo.save_route.side_effect = lambda route, **kwargs: route

    replan_service = RouteReplanService()
    # Force replan_route to return an infeasible route
    infeasible_solution = prev_route_model.to_optimized_route().model_copy(
        update={
            "status": RouteStatus.INFEASIBLE,
            "solver_status": SolverStatus.INFEASIBLE,
            "diagnostics": ["Mandatory stop cannot complete service before closing window"],
            "stops": [],
            "dropped_stops": [],
            "legs": [],
        }
    )
    replan_service.replan_route = AsyncMock(return_value=infeasible_solution)

    req = RouteReplanRequest(
        current_location=Point(latitude=22.55, longitude=88.35),
        replan_at=datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
        completed_stop_ids=[completed_stop_id],
    )

    await replan_service.replan_and_persist(
        route_id=route_id,
        owner_id=owner_id,
        request=req,
        route_repo=mock_repo,
    )

    # Check the route passed to save_route
    assert mock_repo.save_route.called
    saved_route = mock_repo.save_route.call_args[0][0]
    assert saved_route.status == RouteStatus.INFEASIBLE

    # Completed stop is in stops with is_completed=True and is_skipped=False
    assert len(saved_route.stops) == 1
    assert saved_route.stops[0].id == completed_stop_id
    assert saved_route.stops[0].is_completed is True
    assert saved_route.stops[0].is_skipped is False

    # Infeasible stop is in dropped_stops with is_completed=False and is_skipped=True
    assert len(saved_route.dropped_stops) == 1
    assert saved_route.dropped_stops[0].id == infeasible_stop_id
    assert saved_route.dropped_stops[0].is_completed is False
    assert saved_route.dropped_stops[0].is_skipped is True
    assert saved_route.dropped_stops[0].skip_reason == "MANDATORY_STOP_WINDOW_BREACH"
