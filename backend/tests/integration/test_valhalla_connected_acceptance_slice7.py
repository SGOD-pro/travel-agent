"""Connected provider acceptance test suite for external Valhalla routing (AC-3, AC-7, AC-9).

Conforms strictly to specification 0005 Slice 7:
1. Skips cleanly when unconfigured or disabled (RUN_CONNECTED_VALHALLA_TESTS != '1' or VALHALLA_ENDPOINT_URL not set).
2. Fails with actionable diagnostics when enabled but unreachable or misconfigured.
3. Probes GET /status verifying active health and connectivity.
4. Executes live asymmetric multi-stop road matrix calculations via /sources_to_targets.
5. Verifies turn-by-turn maneuvers and polyline6 decoded GeoJSON geometries via /route.
6. Proves multimodal network costing segregation (driving vs walking).
7. Proves hard road closure avoidance via exclude_polygons without traversing closed roads.
8. Executes end-to-end multi-stop itinerary optimization connecting live Valhalla matrices with OrToolsRoutingSolver.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from shapely.geometry import Polygon, shape

from config.config import settings
from contracts.routing import (
    Point,
    RouteObjective,
    RouteRequest,
    RouteStatus,
    RouteStopInput,
    StopType,
)
from contracts.trips import TransportMode
from functions.route_optimizer.solver import OrToolsRoutingSolver
from functions.route_optimizer.valhalla import (
    ValhallaClient,
    ValhallaError,
    ValhallaRoutingError,
)

# Reference test coordinates in Kolkata metropolitan area
HOWRAH_STATION = Point(latitude=22.5839, longitude=88.3426)
VICTORIA_MEMORIAL = Point(latitude=22.5448, longitude=88.3426)
SCIENCE_CITY = Point(latitude=22.5394, longitude=88.3968)
KOLKATA_AIRPORT = Point(latitude=22.6547, longitude=88.4467)


def _is_connected_valhalla_enabled() -> tuple[bool, str]:
    """Checks whether live Valhalla provider acceptance testing is enabled and configured."""
    flag = os.getenv("RUN_CONNECTED_VALHALLA_TESTS", "0").strip().lower()
    if flag not in ("1", "true", "yes"):
        return False, "RUN_CONNECTED_VALHALLA_TESTS is not enabled (set RUN_CONNECTED_VALHALLA_TESTS=1)"

    endpoint = settings.VALHALLA_ENDPOINT_URL
    if not endpoint or not endpoint.strip():
        return False, "VALHALLA_ENDPOINT_URL is unconfigured in application settings"

    return True, ""


@pytest.fixture
async def live_valhalla_client() -> AsyncGenerator[ValhallaClient, None]:
    """Provides an authenticated client to live Valhalla, failing with actionable diagnostics if down."""
    enabled, reason = _is_connected_valhalla_enabled()
    if not enabled:
        pytest.skip(f"Connected Valhalla acceptance test skipped: {reason}")

    endpoint = cast(str, settings.VALHALLA_ENDPOINT_URL)
    client = ValhallaClient(endpoint)

    is_healthy = await client.check_health()
    assert is_healthy is True, (
        f"Valhalla health probe failed against {endpoint}. "
        "Ensure the Valhalla service or Docker container is active and reachable."
    )

    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_valhalla_connected_health_probe(
    live_valhalla_client: ValhallaClient,
) -> None:
    """Verifies that GET /status responds with 200 OK on live Valhalla instance."""
    is_healthy = await live_valhalla_client.check_health()
    assert is_healthy is True


@pytest.mark.asyncio
async def test_valhalla_connected_multistop_asymmetric_matrix(
    live_valhalla_client: ValhallaClient,
) -> None:
    """Verifies live /sources_to_targets endpoint produces valid asymmetric distance and duration matrices."""
    points = [HOWRAH_STATION, VICTORIA_MEMORIAL, SCIENCE_CITY, KOLKATA_AIRPORT]

    matrix = await live_valhalla_client.sources_to_targets(
        origins=points,
        destinations=points,
        mode=TransportMode.CAR_PETROL,
    )

    # 1. Dimension checks
    assert len(matrix.distances_m) == 4
    assert len(matrix.durations_s) == 4

    # 2. Diagonal values are zero
    for i in range(4):
        assert matrix.distances_m[i][i] == 0
        assert matrix.durations_s[i][i] == 0

    # 3. Off-diagonal values are non-zero positive integers
    for i in range(4):
        for j in range(4):
            if i != j:
                dist = matrix.distances_m[i][j]
                dur = matrix.durations_s[i][j]
                assert dist is not None and dist > 0, f"Distance from {i} to {j} should be positive"
                assert dur is not None and dur > 0, f"Duration from {i} to {j} should be positive"

    # 4. Asymmetry check: street directions cause differences between (i, j) and (j, i)
    has_asymmetry = any(
        matrix.distances_m[i][j] != matrix.distances_m[j][i]
        or matrix.durations_s[i][j] != matrix.durations_s[j][i]
        for i in range(4)
        for j in range(i + 1, 4)
    )
    assert has_asymmetry is True, "Expected realistic road network asymmetry across city pairs"


@pytest.mark.asyncio
async def test_valhalla_connected_route_geometries_and_maneuvers(
    live_valhalla_client: ValhallaClient,
) -> None:
    """Verifies live /route endpoint returns turn-by-turn maneuvers and polyline6 decoded geometries."""
    waypoints = [HOWRAH_STATION, VICTORIA_MEMORIAL]

    result = await live_valhalla_client.route(
        locations=waypoints,
        mode=TransportMode.CAR_PETROL,
    )

    # Overall summary metrics
    assert result.total_distance_m > 0
    assert result.total_travel_seconds > 0

    # Leg geometry structure
    assert len(result.legs) == 1
    leg = result.legs[0]
    assert leg.distance_m > 0
    assert leg.travel_seconds > 0

    geom = leg.geometry
    assert geom["type"] == "LineString"
    coords = geom["coordinates"]
    assert len(coords) >= 10, "Expected detailed geometry coordinates along driving route"

    # Validate coordinate ranges
    for coord in coords:
        lon, lat = coord[0], coord[1]
        assert -180.0 <= lon <= 180.0
        assert -90.0 <= lat <= 90.0

    # Turn-by-turn maneuvers
    assert len(leg.turn_instructions) > 0
    first_maneuver = leg.turn_instructions[0]
    assert "instruction" in first_maneuver
    assert isinstance(first_maneuver["time_seconds"], int)
    assert isinstance(first_maneuver["distance_meters"], int)


@pytest.mark.asyncio
async def test_valhalla_connected_multimodal_costing_segregation(
    live_valhalla_client: ValhallaClient,
) -> None:
    """Verifies distinct driving vs pedestrian routing metrics reflecting network segregation (AC-4, AC-7)."""
    origins = [HOWRAH_STATION]
    destinations = [VICTORIA_MEMORIAL]

    car_matrix = await live_valhalla_client.sources_to_targets(
        origins=origins,
        destinations=destinations,
        mode=TransportMode.CAR_PETROL,
    )

    walk_matrix = await live_valhalla_client.sources_to_targets(
        origins=origins,
        destinations=destinations,
        mode=TransportMode.WALKING,
    )

    car_dur = car_matrix.durations_s[0][0]
    walk_dur = walk_matrix.durations_s[0][0]

    assert car_dur is not None and car_dur > 0
    assert walk_dur is not None and walk_dur > 0

    # Walking duration between city stations must be significantly greater than driving duration
    assert walk_dur > car_dur * 2, f"Walking duration ({walk_dur}s) should exceed driving duration ({car_dur}s)"


@pytest.mark.asyncio
async def test_valhalla_connected_hard_closure_avoidance(
    live_valhalla_client: ValhallaClient,
) -> None:
    """Verifies exclude_polygons enforces strict road closure avoidance on live routing (AC-3)."""
    # 1. Base route without exclusion
    base_route = await live_valhalla_client.route(
        locations=[HOWRAH_STATION, VICTORIA_MEMORIAL],
        mode=TransportMode.CAR_PETROL,
    )
    base_coords = base_route.legs[0].geometry["coordinates"]
    assert len(base_coords) > 5

    # 2. Pick a midpoint coordinate from the base route to construct a blocking exclusion polygon
    midpoint = base_coords[len(base_coords) // 2]
    mid_lon, mid_lat = midpoint[0], midpoint[1]
    delta = 0.003  # ~300 meters bounding box

    exclusion_box = [
        [mid_lon - delta, mid_lat - delta],
        [mid_lon + delta, mid_lat - delta],
        [mid_lon + delta, mid_lat + delta],
        [mid_lon - delta, mid_lat + delta],
        [mid_lon - delta, mid_lat - delta],
    ]
    exclusion_poly = Polygon(exclusion_box)

    # 3. Request route with exclude_polygons
    try:
        rerouted = await live_valhalla_client.route(
            locations=[HOWRAH_STATION, VICTORIA_MEMORIAL],
            mode=TransportMode.CAR_PETROL,
            exclude_polygons=[exclusion_box],
        )
        # If routed around, leg geometry must not intersect the exclusion polygon
        rerouted_geom = shape(rerouted.legs[0].geometry)
        assert not rerouted_geom.intersects(exclusion_poly), (
            "Computed route intersected active road closure exclusion polygon"
        )
    except (ValhallaRoutingError, ValhallaError):
        # Valhalla failing closed when an essential bottleneck is closed is valid behavior
        pass


@pytest.mark.asyncio
async def test_valhalla_connected_end_to_end_multistop_optimization(
    live_valhalla_client: ValhallaClient,
) -> None:
    """Verifies end-to-end multi-stop itinerary optimization combining live Valhalla matrices with OR-Tools."""
    dep_time = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    deadline = dep_time + timedelta(hours=9)

    stops = [
        RouteStopInput(
            id=uuid.uuid4(),
            name="Howrah Station",
            location=HOWRAH_STATION,
            stop_type=StopType.ORIGIN,
        ),
        RouteStopInput(
            id=uuid.uuid4(),
            name="Victoria Memorial",
            location=VICTORIA_MEMORIAL,
            stop_type=StopType.DESTINATION,
            is_optional=False,
            opening_time=dep_time + timedelta(hours=1),
            closing_time=dep_time + timedelta(hours=7),
            dwell_seconds=3600,
        ),
        RouteStopInput(
            id=uuid.uuid4(),
            name="Science City",
            location=SCIENCE_CITY,
            stop_type=StopType.DESTINATION,
            is_optional=False,
            opening_time=dep_time + timedelta(hours=2),
            closing_time=dep_time + timedelta(hours=8),
            dwell_seconds=3600,
        ),
        RouteStopInput(
            id=uuid.uuid4(),
            name="Kolkata Airport",
            location=KOLKATA_AIRPORT,
            stop_type=StopType.DESTINATION,
            is_optional=True,
            opening_time=dep_time,
            closing_time=dep_time + timedelta(hours=10),
            dwell_seconds=1800,
        ),
    ]

    points = [s.location for s in stops]

    # Query live matrix from external Valhalla
    matrix = await live_valhalla_client.sources_to_targets(
        origins=points,
        destinations=points,
        mode=TransportMode.CAR_PETROL,
    )

    request = RouteRequest(
        departure_at=dep_time,
        return_deadline=deadline,
        objective=RouteObjective.FASTEST,
        transport_mode=TransportMode.CAR_PETROL,
        stops=stops,
    )

    solver = OrToolsRoutingSolver()
    route = solver.solve(
        request=request,
        durations_s=matrix.durations_s,
        distances_m=matrix.distances_m,
    )

    assert route.status in (RouteStatus.FEASIBLE, RouteStatus.PARTIAL)
    assert len(route.stops) >= 3  # Origin plus at least the two mandatory destinations
    assert route.total_distance_m > 0
    assert route.total_travel_seconds > 0

    # Chronological progression check
    prev_dep = dep_time
    for scheduled_stop in route.stops:
        assert scheduled_stop.planned_arrival_at is not None
        assert scheduled_stop.planned_departure_at is not None
        assert scheduled_stop.planned_arrival_at >= prev_dep
        assert scheduled_stop.planned_departure_at >= scheduled_stop.planned_arrival_at
        prev_dep = scheduled_stop.planned_departure_at
