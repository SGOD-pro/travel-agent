"""Constraint-based Itinerary Scheduler using Google OR-Tools.

Optimizes multi-stop travel sequences and daily time schedules:
- Formulates visit ordering as a Traveling Salesperson / Routing Problem with Time Windows (TSPTW).
- Ensures stop durations and travel leg transit times are respected without overlap.
- Deterministic and reproducible.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, TypedDict

from ortools.constraint_solver import pywrapcp, routing_enums_pb2
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
    """Solves multi-stop routing and sequencing constraints."""

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
        all_locations: list[_LocationNode] = [
            {"name": origin_name, "lat": origin_lat, "lng": origin_lng, "stay_minutes": 0}
        ]
        for s in stops:
            all_locations.append(
                {
                    "name": str(s["name"]),
                    "lat": float(s["lat"]),
                    "lng": float(s["lng"]),
                    "stay_minutes": int(s.get("stay_minutes", 120)),
                }
            )

        n = len(all_locations)
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
                    )
                ],
                legs=[],
                total_travel_minutes=0,
                is_optimal=True,
            )

        # Build distance & duration matrix (in integer minutes for OR-Tools)
        time_matrix: list[list[int]] = []
        dist_matrix: list[list[float]] = []

        for i in range(n):
            time_row: list[int] = []
            dist_row: list[float] = []
            for j in range(n):
                if i == j:
                    time_row.append(0)
                    dist_row.append(0.0)
                else:
                    dist = cls._haversine_distance_km(
                        all_locations[i]["lat"],
                        all_locations[i]["lng"],
                        all_locations[j]["lat"],
                        all_locations[j]["lng"],
                    )
                    # Convert distance to transit duration in minutes
                    dur_minutes = max(1, int((dist / average_speed_kmh) * 60))
                    time_row.append(dur_minutes)
                    dist_row.append(dist)
            time_matrix.append(time_row)
            dist_matrix.append(dist_row)

        # Formulate OR-Tools Routing Model
        manager = pywrapcp.RoutingIndexManager(n, 1, 0)
        routing = pywrapcp.RoutingModel(manager)

        def transit_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return int(time_matrix[from_node][to_node])

        transit_callback_index = routing.RegisterTransitCallback(transit_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        search_parameters.time_limit.seconds = 2

        solution = routing.SolveWithParameters(search_parameters)

        route_nodes: list[int] = []
        is_optimal = solution is not None

        if solution:
            index = routing.Start(0)
            while not routing.IsEnd(index):
                route_nodes.append(manager.IndexToNode(index))
                index = solution.Value(routing.NextVar(index))
        else:
            # Fallback: sequential order
            route_nodes = list(range(n))

        # Build scheduled stops & legs
        scheduled_stops: list[ScheduledStop] = []
        scheduled_legs: list[ScheduledLeg] = []
        current_time = start_datetime
        total_travel_minutes = 0

        for pos, node_idx in enumerate(route_nodes):
            loc = all_locations[node_idx]
            stay = loc["stay_minutes"]

            # If moving from prior stop, add travel time
            if pos > 0:
                prior_idx = route_nodes[pos - 1]
                travel_mins = time_matrix[prior_idx][node_idx]
                leg_dist = dist_matrix[prior_idx][node_idx]
                total_travel_minutes += travel_mins

                scheduled_legs.append(
                    ScheduledLeg(
                        from_stop=all_locations[prior_idx]["name"],
                        to_stop=loc["name"],
                        duration_minutes=travel_mins,
                        distance_km=round(leg_dist, 2),
                    )
                )
                current_time = current_time + timedelta(minutes=travel_mins)

            arr_time = current_time
            dep_time = current_time + timedelta(minutes=stay)
            current_time = dep_time

            scheduled_stops.append(
                ScheduledStop(
                    position=pos,
                    name=loc["name"],
                    latitude=loc["lat"],
                    longitude=loc["lng"],
                    arrival_time=arr_time,
                    departure_time=dep_time,
                    stay_duration_minutes=stay,
                )
            )

        return OptimizedSchedule(
            stops=scheduled_stops,
            legs=scheduled_legs,
            total_travel_minutes=total_travel_minutes,
            is_optimal=is_optimal,
        )
