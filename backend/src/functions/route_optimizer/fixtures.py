"""Fixed 5x5 asymmetric matrix fixture with hard service windows and an unreachable pair.

Conforms to specification 0005:
- 5 locations (Base depot, HMI museum, Zoological Park, Tiger Hill summit, Batasia loop).
- Asymmetric travel times and distances.
- Unreachable arc from stop 1 to stop 3.
- Hard service windows, dwell times, mandatory stops, and optional stop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from contracts.routing import Point, RouteRequest, RouteStopInput, StopType

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
