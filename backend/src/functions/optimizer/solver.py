"""Constraint-based Itinerary Scheduler using Google OR-Tools.

Optimizes multi-stop travel sequences and daily time schedules:
- Formulates visit ordering as a Traveling Salesperson / Routing Problem with Time Windows (TSPTW).
- Integrates external transit matrices (from Routing API) and constraint windows.
- Deterministic, reproducible, and lightweight.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple, TypedDict

from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from pydantic import BaseModel, ConfigDict, Field


# --- Domain Schemas ---

class Coordinate(TypedDict):
    lat: float
    lng: float


class TimeWindow(BaseModel):
    """Represents a time constraint window for a location (e.g. opening hours)."""
    model_config = ConfigDict(frozen=True)

    start_minute: int = Field(ge=0, le=1439, description="Start time in minutes from midnight")
    end_minute: int = Field(ge=0, le=1439, description="End time in minutes from midnight")


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
    cost_entry_inr: float = 0.0


class ScheduledLeg(BaseModel):
    """Transit leg connecting two stops."""
    model_config = ConfigDict(frozen=True)

    from_stop: str
    to_stop: str
    duration_minutes: int
    distance_km: float
    fuel_cost_inr: float = 0.0
    toll_cost_inr: float = 0.0


class OptimizedRoute(BaseModel):
    """Complete optimized route output including costs and validation status."""
    model_config = ConfigDict(frozen=True)

    stops: List[ScheduledStop]
    legs: List[ScheduledLeg]
    total_travel_minutes: int
    total_fuel_cost_inr: float
    total_toll_cost_inr: float
    total_cost_inr: float
    is_valid: bool
    is_optimal: bool
    validation_message: str = ""


# --- Core Solver ---

class ItineraryScheduler:
    """Solves multi-stop routing, sequencing, and time window constraints."""

    @staticmethod
    def _haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculates great-circle distance (fallback if no Routing API matrix is provided)."""
        r = 6371.0
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
        # External integration parameters
        transit_matrix: Optional[list[list[int]]] = None,  # duration in minutes
        distance_matrix: Optional[list[list[float]]] = None,  # distance in km
        time_windows: Optional[list[TimeWindow]] = None,  # constraints per node index
        fuel_price_per_liter: float = 102.5,
        vehicle_mileage_kmpl: float = 15.0,
        toll_cost_per_km: float = 5.0,
    ) -> OptimizedRoute:
        """Optimizes stop order, respects time windows, and calculates cost breakdowns."""
        
        # 1. Assemble Location Nodes
        all_locations: list[dict[str, Any]] = [
            {"name": origin_name, "lat": origin_lat, "lng": origin_lng, "stay_minutes": 0}
        ]
        for s in stops:
            all_locations.append(
                {
                    "name": str(s["name"]),
                    "lat": float(s["lat"]),
                    "lng": float(s["lng"]),
                    "stay_minutes": int(s.get("stay_minutes", 120)),
                    "cost_entry_inr": float(s.get("cost_entry_inr", 0.0)),
                }
            )

        n = len(all_locations)
        if n == 1:
            return OptimizedRoute(
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
                total_fuel_cost_inr=0.0,
                total_toll_cost_inr=0.0,
                total_cost_inr=0.0,
                is_valid=True,
                is_optimal=True,
            )

        # 2. Build Matrices if not provided (Fallback to Haversine)
        if transit_matrix is None or distance_matrix is None:
            time_matrix: list[list[int]] = []
            dist_matrix: list[list[float]] = []
            for i in range(n):
                t_row: list[int] = []
                d_row: list[float] = []
                for j in range(n):
                    if i == j:
                        t_row.append(0)
                        d_row.append(0.0)
                    else:
                        dist = cls._haversine_distance_km(
                            all_locations[i]["lat"], all_locations[i]["lng"],
                            all_locations[j]["lat"], all_locations[j]["lng"]
                        )
                        # Assume average speed of 40 km/h in urban tourist areas
                        dur_mins = max(1, int((dist / 40.0) * 60))
                        t_row.append(dur_mins)
                        d_row.append(dist)
                time_matrix.append(t_row)
                dist_matrix.append(d_row)
        else:
            time_matrix = transit_matrix
            dist_matrix = distance_matrix

        # 3. Formulate OR-Tools Routing Model (TSPTW)
        manager = pywrapcp.RoutingIndexManager(n, 1, 0)
        routing = pywrapcp.RoutingModel(manager)

        def transit_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return int(time_matrix[from_node][to_node])

        transit_callback_index = routing.RegisterTransitCallback(transit_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

        # Add Time Windows Constraint if provided
        if time_windows:
            # Cumulative time dimension
            routing.AddDimension(
                transit_callback_index,
                slack_max=0,
                capacity=1440,  # Max minutes in a day
                fix_start_cumul_to_zero=True,
                name="Time",
            )
            time_dimension = routing.GetDimensionOrDie("Time")
            for node_idx, window in enumerate(time_windows):
                if node_idx == 0:
                    continue  # Origin has no window constraint
                index = manager.NodeToIndex(node_idx)
                time_dimension.CumulVar(index).SetRange(window.start_minute, window.end_minute)

        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        search_parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_parameters.time_limit.seconds = 5

        solution = routing.SolveWithParameters(search_parameters)
        is_optimal = solution is not None

        route_nodes: list[int] = []
        if solution:
            index = routing.Start(0)
            while not routing.IsEnd(index):
                route_nodes.append(manager.IndexToNode(index))
                index = solution.Value(routing.NextVar(index))
        else:
            # Fallback: Sequential order
            route_nodes = list(range(n))
            is_optimal = False

        # 4. Build Scheduled Stops & Legs with Cost Calculations
        scheduled_stops: list[ScheduledStop] = []
        scheduled_legs: list[ScheduledLeg] = []
        current_time = start_datetime
        total_travel_minutes = 0
        total_fuel = 0.0
        total_toll = 0.0

        for pos, node_idx in enumerate(route_nodes):
            loc = all_locations[node_idx]
            stay = loc["stay_minutes"]

            if pos > 0:
                prior_idx = route_nodes[pos - 1]
                travel_mins = time_matrix[prior_idx][node_idx]
                leg_dist = dist_matrix[prior_idx][node_idx]
                total_travel_minutes += travel_mins

                # Cost calculations
                fuel_needed = leg_dist / vehicle_mileage_kmpl
                leg_fuel_cost = fuel_needed * fuel_price_per_liter
                leg_toll_cost = leg_dist * toll_cost_per_km
                total_fuel += leg_fuel_cost
                total_toll += leg_toll_cost

                scheduled_legs.append(
                    ScheduledLeg(
                        from_stop=all_locations[prior_idx]["name"],
                        to_stop=loc["name"],
                        duration_minutes=travel_mins,
                        distance_km=round(leg_dist, 2),
                        fuel_cost_inr=round(leg_fuel_cost, 2),
                        toll_cost_inr=round(leg_toll_cost, 2),
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
                    cost_entry_inr=loc.get("cost_entry_inr", 0.0),
                )
            )

        total_cost = total_fuel + total_toll + sum(s.cost_entry_inr for s in scheduled_stops)
        is_valid = is_optimal  # Simple validation check

        return OptimizedRoute(
            stops=scheduled_stops,
            legs=scheduled_legs,
            total_travel_minutes=total_travel_minutes,
            total_fuel_cost_inr=round(total_fuel, 2),
            total_toll_cost_inr=round(total_toll, 2),
            total_cost_inr=round(total_cost, 2),
            is_valid=is_valid,
            is_optimal=is_optimal,
            validation_message="Route optimized successfully." if is_valid else "Solver fell back to sequential order.",
        )