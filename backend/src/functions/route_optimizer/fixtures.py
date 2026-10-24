"""Fixed 5x5 asymmetric matrix fixture with hard service windows and an unreachable pair.

Conforms to specification 0005:
- 5 locations (Base depot, HMI museum, Zoological Park, Tiger Hill summit, Batasia loop).
- Asymmetric travel times and distances.
- Unreachable arc from stop 1 to stop 3.
- Hard service windows, dwell times, mandatory stops, and optional stop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from contracts.routing import MatrixResponse, Point, RouteRequest, RouteStopInput, StopType
from contracts.trips import TransportMode

if TYPE_CHECKING:
    from functions.route_optimizer.valhalla import ValhallaRouteResult

FIXED_STOP_IDS: list[UUID] = [
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000003"),
    UUID("00000000-0000-0000-0000-000000000004"),
    UUID("00000000-0000-0000-0000-000000000005"),
]

# 5x5 Asymmetric durations in seconds (M[i][j] != M[j][i])
# Pair (1, 3) is UNREACHABLE (None)
FIXED_5X5_DURATIONS_S: list[list[int | None]] = [
    [0, 900, 1100, 2400, 1500],
    [1200, 0, 300, None, 1800],
    [1400, 450, 0, 2100, 1700],
    [2100, 2200, 2000, 0, 1500],
    [1800, 1600, 1900, 1600, 0],
]

# 5x5 Asymmetric distances in meters (M[i][j] != M[j][i])
# Pair (1, 3) is UNREACHABLE (None)
FIXED_5X5_DISTANCES_M: list[list[int | None]] = [
    [0, 5000, 6000, 15000, 8000],
    [5200, 0, 1200, None, 9500],
    [6300, 1500, 0, 13500, 9000],
    [14800, 14000, 13200, 0, 10000],
    [8500, 9200, 9400, 10200, 0],
]


def create_fixed_5x5_stops(
    base_departure: datetime | None = None,
    stop_3_must_visit: bool = True,
    stop_4_must_visit: bool = False,
) -> list[RouteStopInput]:
    """Generates 5 realistic stops with service windows and dwell durations."""
    t0 = base_departure or datetime(2026, 11, 10, 9, 0, tzinfo=UTC)

    return [
        # Stop 0: Base Camp / Hotel (Depot)
        RouteStopInput(
            id=FIXED_STOP_IDS[0],
            name="Darjeeling Base Hotel",
            location=Point(latitude=27.0360, longitude=88.2627),
            stop_type=StopType.ORIGIN,
            is_must_visit=True,
            window_start=None,
            window_end=None,
            dwell_seconds=0,
        ),
        # Stop 1: Himalayan Mountaineering Institute (Window 09:30 - 13:00, Dwell 60m)
        RouteStopInput(
            id=FIXED_STOP_IDS[1],
            name="Himalayan Mountaineering Institute",
            location=Point(latitude=27.0583, longitude=88.2676),
            stop_type=StopType.DESTINATION,
            is_must_visit=True,
            window_start=t0 + timedelta(minutes=30),
            window_end=t0 + timedelta(hours=4),
            dwell_seconds=3600,
        ),
        # Stop 2: Padmaja Naidu Himalayan Zoological Park (Window 10:00 - 15:30, Dwell 60m)
        RouteStopInput(
            id=FIXED_STOP_IDS[2],
            name="Padmaja Naidu Himalayan Zoological Park",
            location=Point(latitude=27.0595, longitude=88.2662),
            stop_type=StopType.DESTINATION,
            is_must_visit=True,
            window_start=t0 + timedelta(hours=1),
            window_end=t0 + timedelta(hours=6, minutes=30),
            dwell_seconds=3600,
        ),
        # Stop 3: Tiger Hill Summit (Window 11:00 - 17:00, Dwell 30m; note: Arc 1 -> 3 is unreachable)
        RouteStopInput(
            id=FIXED_STOP_IDS[3],
            name="Tiger Hill Summit",
            location=Point(latitude=26.9950, longitude=88.2850),
            stop_type=StopType.DESTINATION,
            is_must_visit=stop_3_must_visit,
            window_start=t0 + timedelta(hours=2),
            window_end=t0 + timedelta(hours=8),
            dwell_seconds=1800,
        ),
        # Stop 4: Batasia Loop Memorial (Window 13:00 - 16:30, Dwell 30m; Optional stop)
        RouteStopInput(
            id=FIXED_STOP_IDS[4],
            name="Batasia Loop Memorial",
            location=Point(latitude=27.0167, longitude=88.2467),
            stop_type=StopType.DESTINATION,
            is_must_visit=stop_4_must_visit,
            window_start=t0 + timedelta(hours=4),
            window_end=t0 + timedelta(hours=7, minutes=30),
            dwell_seconds=1800,
        ),
    ]


def create_fixed_5x5_request(
    base_departure: datetime | None = None,
    return_deadline: datetime | None = None,
    is_round_trip: bool = True,
    stop_3_must_visit: bool = True,
    stop_4_must_visit: bool = False,
    time_limit_seconds: int = 1,
) -> RouteRequest:
    """Builds a standard RouteRequest for the fixed 5x5 fixture."""
    t0 = base_departure or datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    stops = create_fixed_5x5_stops(
        base_departure=t0,
        stop_3_must_visit=stop_3_must_visit,
        stop_4_must_visit=stop_4_must_visit,
    )
    return RouteRequest(
        departure_at=t0,
        return_deadline=return_deadline,
        stops=stops,
        is_round_trip=is_round_trip,
        time_limit_seconds=time_limit_seconds,
    )


class MockValhallaClient:
    """Offline mock client simulating external Valhalla routing service."""

    def __init__(
        self,
        base_url: str = "http://mock-valhalla:8002",
        *,
        is_healthy: bool = True,
    ) -> None:
        self.base_url = base_url
        self.is_healthy = is_healthy
        self.call_count: int = 0
        self.retry_count: int = 0
        self.exclude_polygons_received: list[list[list[float]]] = []
        self.simulated_error: Exception | None = None
        self.canned_matrix: MatrixResponse | None = None

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> MockValhallaClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def check_health(self) -> bool:
        return self.is_healthy

    async def sources_to_targets(
        self,
        origins: list[Point],
        destinations: list[Point],
        mode: TransportMode = TransportMode.CAR_PETROL,
        costing_options: dict[str, object] | None = None,
    ) -> MatrixResponse:
        self.call_count += 1
        if self.simulated_error is not None:
            raise self.simulated_error

        if self.canned_matrix is not None:
            return self.canned_matrix

        # Generate realistic offline haversine matrix
        speed_mps = 1.38889 if mode == TransportMode.WALKING else 12.5
        durations_s: list[list[int | None]] = []
        distances_m: list[list[int | None]] = []

        # Inlined import to avoid circular dependencies between fixtures and continuity
        from functions.route_optimizer.continuity import haversine_distance_m

        for orig in origins:
            dur_row: list[int | None] = []
            dist_row: list[int | None] = []
            for dest in destinations:
                dist = haversine_distance_m(orig, dest)
                dur = max(1, int(round(dist / speed_mps))) if dist > 0 else 0
                dist_row.append(dist)
                dur_row.append(dur)
            durations_s.append(dur_row)
            distances_m.append(dist_row)

        return MatrixResponse(durations_s=durations_s, distances_m=distances_m)

    async def route(
        self,
        locations: list[Point],
        mode: TransportMode = TransportMode.CAR_PETROL,
        exclude_polygons: list[list[list[float]]] | None = None,
        costing_options: dict[str, object] | None = None,
    ) -> ValhallaRouteResult:
        self.call_count += 1
        if self.simulated_error is not None:
            raise self.simulated_error

        if exclude_polygons:
            self.exclude_polygons_received.extend(exclude_polygons)

        # Inlined imports to avoid circular dependencies between fixtures, continuity, and valhalla
        from functions.route_optimizer.continuity import haversine_distance_m
        from functions.route_optimizer.valhalla import ValhallaLegResult, ValhallaRouteResult

        speed_mps = 1.38889 if mode == TransportMode.WALKING else 12.5
        legs: list[ValhallaLegResult] = []
        total_dist = 0
        total_time = 0
        all_coords: list[list[float]] = []

        for i in range(len(locations) - 1):
            p1 = locations[i]
            p2 = locations[i + 1]
            dist = haversine_distance_m(p1, p2)
            dur = max(1, int(round(dist / speed_mps))) if dist > 0 else 0
            from shapely.geometry import LineString, Polygon

            mid_lon = (p1.longitude + p2.longitude) / 2.0
            mid_lat = (p1.latitude + p2.latitude) / 2.0
            coords = [
                [p1.longitude, p1.latitude],
                [mid_lon, mid_lat],
                [p2.longitude, p2.latitude],
            ]

            detour_occurred = False
            for poly_coords in (exclude_polygons or []):
                if len(poly_coords) >= 3:
                    try:
                        poly = Polygon(poly_coords)
                        if LineString(coords).intersects(poly):
                            detour_occurred = True
                            minx, miny, maxx, maxy = poly.bounds
                            candidates = [
                                [minx - 0.005, maxy + 0.005],
                                [maxx + 0.005, miny - 0.005],
                                [minx - 0.01, miny - 0.01],
                                [maxx + 0.01, maxy + 0.01],
                            ]
                            for cand_lon, cand_lat in candidates:
                                test_coords = [
                                    [p1.longitude, p1.latitude],
                                    [cand_lon, cand_lat],
                                    [p2.longitude, p2.latitude],
                                ]
                                if not LineString(test_coords).intersects(poly):
                                    coords = test_coords
                                    break
                    except Exception:
                        pass

            if detour_occurred:
                dist = int(round(dist * 1.25))
                dur = int(round(dur * 1.25))

            total_dist += dist
            total_time += dur
            all_coords.extend(coords)

            legs.append(
                ValhallaLegResult(
                    distance_m=dist,
                    travel_seconds=dur,
                    geometry={"type": "LineString", "coordinates": coords},
                    turn_instructions=[
                        {
                            "instruction": f"Head toward stop {i + 1}",
                            "type": 1,
                            "street_names": ["Main Road"],
                            "time_seconds": dur,
                            "distance_meters": dist,
                        }
                    ],
                )
            )

        return ValhallaRouteResult(
            total_distance_m=total_dist,
            total_travel_seconds=total_time,
            legs=legs,
            geometry={"type": "LineString", "coordinates": all_coords},
        )

