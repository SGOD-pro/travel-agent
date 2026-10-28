"""Unit test suite for Slice 4: External Valhalla HTTP client and hard closure avoidance.

Conforms strictly to specification 0005 AC-3 and AC-7:
- Coordinate validation and SSRF rejection.
- Valhalla costing models (auto, motorcycle, pedestrian, bicycle).
- Polyline6 shape decoding into GeoJSON LineString coordinates.
- Asymmetric matrix parsing (km to meters, duration in seconds, null unreachable handling).
- Bounded retry backoff on 5xx errors and fast failure on 4xx errors.
- Strict road closure exclusion extraction for Valhalla exclude_polygons.
- Post-routing geometry revalidation with temporal window filtering.
- Safe fail-closed semantics for unsupported restrictions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest

from contracts.routing import (
    Point,
    RestrictionType,
    RouteLegOutput,
)
from contracts.trips import TransportMode
from functions.route_optimizer.closures import (
    ClosureAvoidanceService,
    RestrictionClaim,
    check_temporal_overlap,
)
from functions.route_optimizer.fixtures import MockValhallaClient
from functions.route_optimizer.valhalla import (
    ValhallaClient,
    ValhallaConnectionError,
    ValhallaRoutingError,
    ValhallaSSRFError,
    ValhallaTimeoutError,
    decode_polyline6,
    polyline6_to_geojson_coordinates,
    validate_coordinates,
)

# --- Coordinate Validation & SSRF Tests ---


def test_validate_coordinates_valid() -> None:
    """Valid coordinates inside geographic limits pass without exception."""
    validate_coordinates(Point(latitude=22.5726, longitude=88.3639))
    validate_coordinates(Point(latitude=-90.0, longitude=-180.0))
    validate_coordinates(Point(latitude=90.0, longitude=180.0))


def test_validate_coordinates_invalid_latitude() -> None:
    """Out-of-bounds latitude raises ValhallaSSRFError."""
    with pytest.raises(ValhallaSSRFError, match="Invalid latitude"):
        validate_coordinates(90.1, 88.0)
    with pytest.raises(ValhallaSSRFError, match="Invalid latitude"):
        validate_coordinates(-90.1, 88.0)


def test_validate_coordinates_invalid_longitude() -> None:
    """Out-of-bounds longitude raises ValhallaSSRFError."""
    with pytest.raises(ValhallaSSRFError, match="Invalid longitude"):
        validate_coordinates(22.0, 180.5)
    with pytest.raises(ValhallaSSRFError, match="Invalid longitude"):
        validate_coordinates(22.0, -180.5)


def test_validate_coordinates_missing_longitude_raises_value_error() -> None:
    """Passing a raw float latitude without longitude raises explicit ValueError."""
    with pytest.raises(ValueError, match="requires an explicit longitude"):
        validate_coordinates(22.5726)


# --- Polyline6 Decoding Tests ---


def test_decode_polyline6_known_vector() -> None:
    """Verifies that 6-decimal precision polyline encoding decodes accurately."""
    # Polyline6 encoding for Darjeeling coordinates
    # Point 1: 27.036000, 88.262700
    # Point 2: 27.058300, 88.267600
    encoded = "_ucqr@wacjgDwpj@gqH"
    decoded = decode_polyline6(encoded)
    assert len(decoded) == 2
    assert abs(decoded[0][0] - 27.036) < 1e-4
    assert abs(decoded[0][1] - 88.2627) < 1e-4

    # GeoJSON format [[lon, lat], ...]
    geojson_coords = polyline6_to_geojson_coordinates(encoded)
    assert len(geojson_coords) == 2
    assert abs(geojson_coords[0][0] - 88.2627) < 1e-4
    assert abs(geojson_coords[0][1] - 27.036) < 1e-4


# --- Transport Mode Costing Mappings ---


def test_costing_for_mode() -> None:
    """Verifies internal TransportMode maps to expected Valhalla costing profiles."""
    client = ValhallaClient("http://localhost:8002")
    assert client._costing_for_mode(TransportMode.CAR_PETROL) == "auto"
    assert client._costing_for_mode(TransportMode.MOTORCYCLE_PETROL) == "motorcycle"
    assert client._costing_for_mode(TransportMode.WALKING) == "pedestrian"
    assert client._costing_for_mode(TransportMode.BICYCLE) == "bicycle"


# --- HTTP Client Matrix and Route Parsing Tests ---


@pytest.mark.asyncio
async def test_valhalla_client_sources_to_targets_success() -> None:
    """Verifies /sources_to_targets parses distances into meters and handles unreachable cells."""
    mock_valhalla_response = {
        "sources_to_targets": [
            [
                {"distance": 0.0, "time": 0.0, "from_index": 0, "to_index": 0},
                {"distance": 5.432, "time": 650.0, "from_index": 0, "to_index": 1},
            ],
            [
                {"distance": 6.100, "time": 720.0, "from_index": 1, "to_index": 0},
                None,  # Unreachable pair
            ],
        ],
        "units": "kilometers",
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sources_to_targets"
        return httpx.Response(200, json=mock_valhalla_response)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ValhallaClient("http://localhost:8002", http_client=http_client)
        origins = [
            Point(latitude=22.5700, longitude=88.3600),
            Point(latitude=22.5800, longitude=88.3700),
        ]
        destinations = [
            Point(latitude=22.5700, longitude=88.3600),
            Point(latitude=22.5800, longitude=88.3700),
        ]
        res = await client.sources_to_targets(origins, destinations, TransportMode.CAR_PETROL)

        assert len(res.distances_m) == 2
        assert len(res.durations_s) == 2
        # (0, 0)
        assert res.distances_m[0][0] == 0
        assert res.durations_s[0][0] == 0
        # (0, 1) -> 5.432 km -> 5432 m
        assert res.distances_m[0][1] == 5432
        assert res.durations_s[0][1] == 650
        # (1, 0) -> 6.1 km -> 6100 m
        assert res.distances_m[1][0] == 6100
        assert res.durations_s[1][0] == 720
        # (1, 1) -> Unreachable -> None
        assert res.distances_m[1][1] is None
        assert res.durations_s[1][1] is None


@pytest.mark.asyncio
async def test_valhalla_client_route_success() -> None:
    """Verifies /route endpoint parses legs, maneuvers, and decodes geometries."""
    mock_route_response = {
        "trip": {
            "summary": {"length": 10.5, "time": 1200.0},
            "legs": [
                {
                    "summary": {"length": 10.5, "time": 1200.0},
                    "shape": "_ucqr@wacjgDwpj@gqH",
                    "maneuvers": [
                        {
                            "instruction": "Drive north on Hill Cart Road",
                            "type": 1,
                            "street_names": ["Hill Cart Road"],
                            "time": 300.0,
                            "length": 2.5,
                        }
                    ],
                }
            ],
        }
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/route"
        return httpx.Response(200, json=mock_route_response)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ValhallaClient("http://localhost:8002", http_client=http_client)
        points = [
            Point(latitude=27.0360, longitude=88.2627),
            Point(latitude=27.0583, longitude=88.2676),
        ]
        route_result = await client.route(
            points,
            mode=TransportMode.CAR_PETROL,
            exclude_polygons=[[[88.265, 27.045], [88.266, 27.045], [88.266, 27.046], [88.265, 27.045]]],
        )

        assert route_result.total_distance_m == 10500
        assert route_result.total_travel_seconds == 1200
        assert len(route_result.legs) == 1
        leg0 = route_result.legs[0]
        assert leg0.distance_m == 10500
        assert leg0.travel_seconds == 1200
        assert leg0.geometry["type"] == "LineString"
        assert len(leg0.geometry["coordinates"]) == 2
        assert len(leg0.turn_instructions) == 1
        assert "Hill Cart Road" in leg0.turn_instructions[0]["instruction"]


# --- Error Handling & Retry Tests ---


@pytest.mark.asyncio
async def test_valhalla_client_retries_on_5xx_and_succeeds() -> None:
    """Verifies client retries transient 500 error and returns result on subsequent success."""
    attempts = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, json={"sources_to_targets": [[{"distance": 1.0, "time": 60}]]})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ValhallaClient("http://localhost:8002", http_client=http_client)
        res = await client.sources_to_targets(
            [Point(latitude=22.0, longitude=88.0)],
            [Point(latitude=22.0, longitude=88.0)],
        )
        assert attempts == 2
        assert client.retry_count == 1
        assert res.distances_m[0][0] == 1000


@pytest.mark.asyncio
async def test_valhalla_client_fast_fails_on_4xx() -> None:
    """Verifies client does not retry 400 client errors and fails immediately."""
    attempts = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, text="Bad Request: Location not on road network")

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ValhallaClient("http://localhost:8002", http_client=http_client)
        with pytest.raises(ValhallaRoutingError, match="Valhalla client error 400"):
            await client.route([
                Point(latitude=22.0, longitude=88.0),
                Point(latitude=22.1, longitude=88.1),
            ])
        assert attempts == 1
        assert client.retry_count == 0


@pytest.mark.asyncio
async def test_valhalla_client_timeout_handling() -> None:
    """Verifies timeout exception triggers retries and eventually raises ValhallaTimeoutError."""

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Socket read timed out")

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ValhallaClient("http://localhost:8002", http_client=http_client)
        with pytest.raises(ValhallaTimeoutError, match="timed out"):
            await client.route([
                Point(latitude=22.0, longitude=88.0),
                Point(latitude=22.1, longitude=88.1),
            ])
        assert client.retry_count >= 1


@pytest.mark.asyncio
async def test_valhalla_client_unconfigured_url() -> None:
    """Verifies unconfigured endpoint URL raises ValhallaConnectionError immediately."""
    client = ValhallaClient(base_url=None)
    client.base_url = None
    with pytest.raises(ValhallaConnectionError, match="VALHALLA_ENDPOINT_URL is not configured"):
        await client.route([
            Point(latitude=22.0, longitude=88.0),
            Point(latitude=22.1, longitude=88.1),
        ])


# --- Closure Avoidance and Revalidation Tests ---


def test_extract_valhalla_exclude_polygons_polygon() -> None:
    """Verifies GeoJSON Polygon coordinates convert into Valhalla outer rings."""
    poly_geom = {
        "type": "Polygon",
        "coordinates": [
            [
                [88.2600, 27.0400],
                [88.2700, 27.0400],
                [88.2700, 27.0500],
                [88.2600, 27.0500],
                [88.2600, 27.0400],
            ]
        ],
    }
    claim = RestrictionClaim(
        id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry=poly_geom,
    )
    polygons, records, diags = ClosureAvoidanceService.extract_valhalla_exclude_polygons([claim])

    assert len(polygons) == 1
    assert len(records) == 1
    assert records[0].was_applied is True
    assert len(diags) == 0
    # Outer ring coordinates
    assert len(polygons[0]) == 5
    assert polygons[0][0] == [88.2600, 27.0400]


def test_extract_valhalla_exclude_polygons_linestring_buffer() -> None:
    """Verifies GeoJSON LineString buffers into a closed exclusion polygon."""
    line_geom = {
        "type": "LineString",
        "coordinates": [[88.2600, 27.0400], [88.2650, 27.0450]],
    }
    claim = RestrictionClaim(
        id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry=line_geom,
    )
    polygons, records, diags = ClosureAvoidanceService.extract_valhalla_exclude_polygons([claim])

    assert len(polygons) == 1
    assert len(records) == 1
    assert records[0].was_applied is True
    assert len(diags) == 0
    # Buffered polygon exterior ring has at least 4 coordinates
    assert len(polygons[0]) >= 4


def test_extract_valhalla_exclude_polygons_multipolygon() -> None:
    """Verifies GeoJSON MultiPolygon coordinates extract each constituent outer ring."""
    multi_geom = {
        "type": "MultiPolygon",
        "coordinates": [
            [
                [
                    [88.2600, 27.0400],
                    [88.2700, 27.0400],
                    [88.2700, 27.0500],
                    [88.2600, 27.0500],
                    [88.2600, 27.0400],
                ]
            ],
            [
                [
                    [88.2800, 27.0600],
                    [88.2900, 27.0600],
                    [88.2900, 27.0700],
                    [88.2800, 27.0700],
                    [88.2800, 27.0600],
                ]
            ],
        ],
    }
    claim = RestrictionClaim(
        id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry=multi_geom,
    )
    polygons, records, diags = ClosureAvoidanceService.extract_valhalla_exclude_polygons([claim])

    assert len(polygons) == 2
    assert len(records) == 1
    assert records[0].was_applied is True
    assert len(diags) == 0
    assert len(polygons[0]) == 5
    assert len(polygons[1]) == 5


def test_extract_valhalla_exclude_polygons_unsupported_fails_closed() -> None:
    """Verifies unsupported geometry types are flagged with diagnostic details."""
    point_geom = {"type": "Point", "coordinates": [88.2600, 27.0400]}
    claim = RestrictionClaim(
        id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry=point_geom,
    )
    polygons, records, diags = ClosureAvoidanceService.extract_valhalla_exclude_polygons([claim])

    assert len(polygons) == 0
    assert len(records) == 1
    assert records[0].was_applied is False
    assert "UNSUPPORTED_RESTRICTION" in records[0].unresolved_reason  # type: ignore[operator]
    assert len(diags) == 1


def test_temporal_overlap_filtering() -> None:
    """Verifies that check_temporal_overlap correctly identifies active vs inactive windows."""
    t_start = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 10, 10, 45, tzinfo=UTC)

    # 1. Restriction covers the traversal window -> Overlaps
    r_start = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    r_end = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert check_temporal_overlap(t_start, t_end, r_start, r_end) is True

    # 2. Restriction ends before traversal begins -> No overlap
    r_past_start = datetime(2026, 11, 10, 7, 0, tzinfo=UTC)
    r_past_end = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
    assert check_temporal_overlap(t_start, t_end, r_past_start, r_past_end) is False

    # 3. Restriction starts after traversal completes -> No overlap
    r_future_start = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    r_future_end = datetime(2026, 11, 10, 16, 0, tzinfo=UTC)
    assert check_temporal_overlap(t_start, t_end, r_future_start, r_future_end) is False

    # 4. Unbounded restriction (e.g. permanent closure) -> Overlaps
    assert check_temporal_overlap(t_start, t_end, None, None) is True


def test_revalidate_leg_geometries_detects_intersection() -> None:
    """Verifies post-routing spatial intersection flags legs crossing active closures."""
    # LineString crossing from (88.20, 27.00) to (88.30, 27.00)
    leg = RouteLegOutput(
        leg_order=0,
        from_stop_id=uuid.uuid4(),
        to_stop_id=uuid.uuid4(),
        from_stop_name="Origin",
        to_stop_name="Destination",
        departure_at=datetime(2026, 11, 10, 10, 0, tzinfo=UTC),
        arrival_at=datetime(2026, 11, 10, 10, 30, tzinfo=UTC),
        distance_m=10000,
        travel_seconds=1800,
        geometry={
            "type": "LineString",
            "coordinates": [[88.20, 27.00], [88.30, 27.00]],
        },
    )

    # Closure polygon in the middle (88.24 - 88.26, 26.99 - 27.01)
    closure_claim = RestrictionClaim(
        id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [88.24, 26.99],
                    [88.26, 26.99],
                    [88.26, 27.01],
                    [88.24, 27.01],
                    [88.24, 26.99],
                ]
            ],
        },
        effective_start=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
        effective_end=datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
        is_active=True,
        is_verified=True,
    )

    violations = ClosureAvoidanceService.revalidate_leg_geometries([leg], [closure_claim])
    assert len(violations) == 1
    assert "ROAD_CLOSURE_INTERSECTION" in violations[0]


def test_revalidate_leg_geometries_ignores_inactive_closures() -> None:
    """Verifies spatial intersection is ignored if the restriction was inactive during travel."""
    leg = RouteLegOutput(
        leg_order=0,
        from_stop_id=uuid.uuid4(),
        to_stop_id=uuid.uuid4(),
        from_stop_name="Origin",
        to_stop_name="Destination",
        departure_at=datetime(2026, 11, 10, 10, 0, tzinfo=UTC),
        arrival_at=datetime(2026, 11, 10, 10, 30, tzinfo=UTC),
        distance_m=10000,
        travel_seconds=1800,
        geometry={
            "type": "LineString",
            "coordinates": [[88.20, 27.00], [88.30, 27.00]],
        },
    )

    # Closure was active yesterday only
    past_closure = RestrictionClaim(
        id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [88.24, 26.99],
                    [88.26, 26.99],
                    [88.26, 27.01],
                    [88.24, 27.01],
                    [88.24, 26.99],
                ]
            ],
        },
        effective_start=datetime(2026, 11, 9, 9, 0, tzinfo=UTC),
        effective_end=datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
    )

    violations = ClosureAvoidanceService.revalidate_leg_geometries([leg], [past_closure])
    assert len(violations) == 0


# --- MockValhallaClient Offline Tests ---


@pytest.mark.asyncio
async def test_mock_valhalla_client_offline() -> None:
    """Verifies that MockValhallaClient calculates matrices and synthetic routes offline."""
    mock = MockValhallaClient()
    p1 = Point(latitude=27.0360, longitude=88.2627)
    p2 = Point(latitude=27.0583, longitude=88.2676)

    matrix = await mock.sources_to_targets([p1, p2], [p1, p2], TransportMode.CAR_PETROL)
    assert len(matrix.distances_m) == 2
    assert matrix.distances_m[0][0] == 0
    assert matrix.distances_m[0][1] is not None and matrix.distances_m[0][1] > 0

    route_res = await mock.route(
        [p1, p2],
        mode=TransportMode.CAR_PETROL,
        exclude_polygons=[[[88.26, 27.04], [88.27, 27.04], [88.27, 27.05], [88.26, 27.04]]],
    )
    assert route_res.total_distance_m > 0
    assert len(route_res.legs) == 1
    assert len(mock.exclude_polygons_received) == 1
    assert mock.call_count == 2


@pytest.mark.asyncio
async def test_changed_road_closure_full_intersection_and_route_change() -> None:
    """Proves full line-polygon intersection and demonstrates that the road closure changes the route."""
    from shapely.geometry import LineString, Point as ShapelyPoint, Polygon

    # Point A (MG Road Depot) and Point B (Cubbon Park Parking Hub)
    p1 = Point(latitude=12.9716, longitude=77.6101)
    p2 = Point(latitude=12.9794, longitude=77.5855)

    # Road closure polygon directly on the straight-line path between P1 and P2
    # Longitude: 77.5950 to 77.6010, Latitude: 12.9740 to 12.9770
    closure_coords = [
        [77.5950, 12.9740],
        [77.6010, 12.9740],
        [77.6010, 12.9770],
        [77.5950, 12.9770],
        [77.5950, 12.9740],
    ]
    closure_poly = Polygon(closure_coords)

    # Direct line between P1 and P2
    direct_line = LineString([[p1.longitude, p1.latitude], [p2.longitude, p2.latitude]])

    # 1. Assert endpoints P1 and P2 are outside the polygon, but the line crosses through (full line-polygon intersection)
    assert not closure_poly.contains(ShapelyPoint(p1.longitude, p1.latitude))
    assert not closure_poly.contains(ShapelyPoint(p2.longitude, p2.latitude))
    assert direct_line.intersects(closure_poly), "Direct path must intersect closure polygon"

    # 2. Test revalidation rejects the direct intersecting leg
    intersecting_leg = RouteLegOutput(
        leg_order=0,
        from_stop_id=uuid.uuid4(),
        to_stop_id=uuid.uuid4(),
        from_stop_name="Depot",
        to_stop_name="Parking Hub",
        departure_at=datetime(2026, 11, 10, 10, 0, tzinfo=UTC),
        arrival_at=datetime(2026, 11, 10, 10, 30, tzinfo=UTC),
        distance_m=2800,
        travel_seconds=300,
        geometry={"type": "LineString", "coordinates": [[p1.longitude, p1.latitude], [p2.longitude, p2.latitude]]},
    )
    closure_claim = RestrictionClaim(
        id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry={"type": "Polygon", "coordinates": [closure_coords]},
        effective_start=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
        effective_end=datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
        is_active=True,
        is_verified=True,
    )
    violations = ClosureAvoidanceService.revalidate_leg_geometries([intersecting_leg], [closure_claim])
    assert len(violations) == 1
    assert "ROAD_CLOSURE_INTERSECTION" in violations[0]

    # 3. Solve route WITHOUT closure using MockValhallaClient
    mock = MockValhallaClient()
    unrestricted_route = await mock.route([p1, p2], mode=TransportMode.CAR_PETROL, exclude_polygons=[])
    assert len(unrestricted_route.legs) == 1
    unrestricted_coords = unrestricted_route.legs[0].geometry["coordinates"]
    assert LineString(unrestricted_coords).intersects(closure_poly), "Unrestricted route must intersect the closure"

    # 4. Solve route WITH closure using MockValhallaClient
    detour_route = await mock.route([p1, p2], mode=TransportMode.CAR_PETROL, exclude_polygons=[closure_coords])
    assert len(detour_route.legs) == 1
    detour_coords = detour_route.legs[0].geometry["coordinates"]

    # 5. Prove closure changes the route:
    # - Route coordinates differ
    assert detour_coords != unrestricted_coords, "Closure must alter route geometry"
    # - Detour distance is strictly greater than unrestricted distance
    assert detour_route.total_distance_m > unrestricted_route.total_distance_m, (
        f"Detour distance ({detour_route.total_distance_m}m) must exceed unrestricted ({unrestricted_route.total_distance_m}m)"
    )
    # - Detoured route leg does NOT intersect the closure polygon (full line-polygon non-intersection)
    assert not LineString(detour_coords).intersects(closure_poly), "Detour path must not intersect closure polygon"

    # 6. Revalidation of detoured leg passes cleanly with zero violations
    detour_leg = RouteLegOutput(
        leg_order=0,
        from_stop_id=uuid.uuid4(),
        to_stop_id=uuid.uuid4(),
        from_stop_name="Depot",
        to_stop_name="Parking Hub",
        departure_at=datetime(2026, 11, 10, 10, 0, tzinfo=UTC),
        arrival_at=datetime(2026, 11, 10, 10, 35, tzinfo=UTC),
        distance_m=detour_route.total_distance_m,
        travel_seconds=detour_route.total_travel_seconds,
        geometry={"type": "LineString", "coordinates": detour_coords},
    )
    assert len(ClosureAvoidanceService.revalidate_leg_geometries([detour_leg], [closure_claim])) == 0

