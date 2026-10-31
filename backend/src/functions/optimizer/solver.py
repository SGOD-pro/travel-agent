"""Constraint-based Itinerary Scheduler using Google OR-Tools.

Optimizes multi-stop travel sequences and daily time schedules:
- Formulates visit ordering as a Traveling Salesperson / Routing Problem with Time Windows (TSPTW).
- Ensures stop durations and travel leg transit times are respected without overlap.
- Deterministic and reproducible.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict


class _LocationNode(TypedDict):
    name: str
    lat: float
    lng: float
    stay_minutes: int


class ScheduledStop(BaseModel):
    """An ordered stop on the scheduled itinerary."""

    model_config = ConfigDict(frozen=True)

    position: int
    name: str
    latitude: float
    longitude: float
    arrival_time: datetime
    departure_time: datetime
    stay_duration_minutes: int
    window_start: datetime | None = None
    window_end: datetime | None = None
    is_hours_assumed: bool = False
    hours_status: str = "UNKNOWN"


class ScheduledLeg(BaseModel):
    """Transit leg connecting two stops."""

    model_config = ConfigDict(frozen=True)

    from_stop: str
    to_stop: str
    duration_minutes: int
    distance_km: float


class OptimizedSchedule(BaseModel):
    """Complete optimized schedule output."""

    model_config = ConfigDict(frozen=True)

    stops: list[ScheduledStop]
    legs: list[ScheduledLeg]
    total_travel_minutes: int
    is_optimal: bool


class ItineraryScheduler:
    """Solves multi-stop routing and sequencing constraints.

    Serves as a backward-compatible facade delegating internally to OrToolsRoutingSolver.
    """

    @staticmethod
    def _haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        import math

        r = 6371.0  # Earth radius in kilometers
        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(lon2 - lon1)
        a = (
            math.sin(d_lat / 2) ** 2
            + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return r * c

    @classmethod
    def solve_schedule(
        cls,
        origin_name: str,
        origin_lat: float,
        origin_lng: float,
        stops: list[dict[str, Any]],
        start_datetime: datetime,
        average_speed_kmh: float = 50.0,
    ) -> OptimizedSchedule:
        """Optimizes stop order and builds scheduled legs and arrival/departure timestamps."""
        from contracts.routing import (
            Point,
            RouteObjective,
            RouteRequest,
            RouteStopInput,
            SolverStatus,
            StopType,
        )
        from functions.route_optimizer.solver import OrToolsRoutingSolver

        # 1. Build stop inputs
        route_stops: list[RouteStopInput] = [
            RouteStopInput(
                name=origin_name,
                location=Point(latitude=float(origin_lat), longitude=float(origin_lng)),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
                dwell_seconds=0,
            )
        ]
        stop_input_map: dict[str, dict[str, Any]] = {}
        for s in stops:
            s_name = str(s["name"])
            stop_input_map[s_name] = s
            stay_mins = int(s.get("stay_minutes", 120))

            w_start_val = s.get("window_start")
            w_end_val = s.get("window_end")
            dt_w_start: datetime | None = None
            dt_w_end: datetime | None = None

            if isinstance(w_start_val, datetime):
                dt_w_start = w_start_val
            elif isinstance(w_start_val, time):
                dt_w_start = datetime.combine(
                    start_datetime.date(), w_start_val, tzinfo=start_datetime.tzinfo
                )
            elif isinstance(w_start_val, str) and w_start_val.strip():
                parts = w_start_val.strip().split(":")
                if len(parts) >= 2:
                    try:
                        dt_w_start = datetime.combine(
                            start_datetime.date(),
                            time(int(parts[0]), int(parts[1])),
                            tzinfo=start_datetime.tzinfo,
                        )
                    except Exception:
                        pass

            if isinstance(w_end_val, datetime):
                dt_w_end = w_end_val
            elif isinstance(w_end_val, time):
                dt_w_end = datetime.combine(
                    start_datetime.date(), w_end_val, tzinfo=start_datetime.tzinfo
                )
            elif isinstance(w_end_val, str) and w_end_val.strip():
                parts = w_end_val.strip().split(":")
                if len(parts) >= 2:
                    try:
                        dt_w_end = datetime.combine(
                            start_datetime.date(),
                            time(int(parts[0]), int(parts[1])),
                            tzinfo=start_datetime.tzinfo,
                        )
                    except Exception:
                        pass

            route_stops.append(
                RouteStopInput(
                    name=s_name,
                    location=Point(latitude=float(s["lat"]), longitude=float(s["lng"])),
                    stop_type=StopType.DESTINATION,
                    is_must_visit=True,
                    dwell_seconds=stay_mins * 60,
                    window_start=dt_w_start,
                    window_end=dt_w_end,
                )
            )

        n = len(route_stops)
        if n == 1:
            return OptimizedSchedule(
                stops=[
                    ScheduledStop(
                        position=0,
                        name=origin_name,
                        latitude=origin_lat,
                        longitude=origin_lng,
                        arrival_time=start_datetime,
                        departure_time=start_datetime,
                        stay_duration_minutes=0,
                        is_hours_assumed=False,
                        hours_status="ORIGIN",
                    )
                ],
                legs=[],
                total_travel_minutes=0,
                is_optimal=True,
            )

        # 2. Build local distance & duration matrices using haversine calculation
        durations_s: list[list[int | None]] = []
        distances_m: list[list[int | None]] = []

        for i in range(n):
            dur_row: list[int | None] = []
            dist_row: list[int | None] = []
            for j in range(n):
                if i == j:
                    dur_row.append(0)
                    dist_row.append(0)
                else:
                    dist_km = cls._haversine_distance_km(
                        route_stops[i].location.latitude,
                        route_stops[i].location.longitude,
                        route_stops[j].location.latitude,
                        route_stops[j].location.longitude,
                    )
                    dur_sec = max(60, int((dist_km / average_speed_kmh) * 3600))
                    dist_m = max(1, int(dist_km * 1000))
                    dur_row.append(dur_sec)
                    dist_row.append(dist_m)
            durations_s.append(dur_row)
            distances_m.append(dist_row)

        # 3. Formulate RouteRequest and solve via OrToolsRoutingSolver
        request = RouteRequest(
            departure_at=start_datetime,
            stops=route_stops,
            objective=RouteObjective.FASTEST,
            is_round_trip=False,
            time_limit_seconds=5,
        )

        solver = OrToolsRoutingSolver()
        optimized = solver.solve(request, durations_s, distances_m)

        # 4. Map back to OptimizedSchedule
        sched_stops: list[ScheduledStop] = []
        for stop_out in optimized.stops:
            info = stop_input_map.get(stop_out.name, {})
            is_origin = (stop_out.name == origin_name and stop_out.stop_order == 0)
            sched_stops.append(
                ScheduledStop(
                    position=stop_out.stop_order,
                    name=stop_out.name,
                    latitude=stop_out.location.latitude,
                    longitude=stop_out.location.longitude,
                    arrival_time=stop_out.planned_arrival_at or start_datetime,
                    departure_time=stop_out.planned_departure_at or start_datetime,
                    stay_duration_minutes=stop_out.dwell_seconds // 60,
                    window_start=stop_out.window_start,
                    window_end=stop_out.window_end,
                    is_hours_assumed=bool(info.get("is_hours_assumed", False)) if not is_origin else False,
                    hours_status=str(info.get("hours_status", "ORIGIN" if is_origin else "UNKNOWN")),
                )
            )

        sched_legs: list[ScheduledLeg] = []
        for leg in optimized.legs:
            sched_legs.append(
                ScheduledLeg(
                    from_stop=leg.from_stop_name,
                    to_stop=leg.to_stop_name,
                    duration_minutes=leg.travel_seconds // 60,
                    distance_km=round(leg.distance_m / 1000.0, 2),
                )
            )

        return OptimizedSchedule(
            stops=sched_stops,
            legs=sched_legs,
            total_travel_minutes=optimized.total_travel_seconds // 60,
            is_optimal=(optimized.solver_status == SolverStatus.OPTIMAL),
        )


__all__ = [
    "ItineraryScheduler",
    "OptimizedSchedule",
    "ScheduledLeg",
    "ScheduledStop",
]
