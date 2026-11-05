"""Execution nodes for the Route Optimizer workflow."""
import logging
from datetime import datetime
from typing import Any

from config.config import get_settings
from functions.optimizer.constraints import apply_time_window_constraints
from functions.optimizer.routing import get_route_geometry, get_transit_matrix
from functions.optimizer.solver import ItineraryScheduler

from .state import RouteState

logger = logging.getLogger(__name__)


async def geocode_node(state: RouteState) -> dict[str, Any]:
    request = state["request"]
    destinations = list(request.get("destinations", []))
    detour = request.get("detour_waypoints", [])
    all_stops = list(detour) + destinations
    coords = [(request["origin_lat"], request["origin_lng"])] + [
        (destination["lat"], destination["lng"])
        for destination in all_stops
    ]
    if request.get("return_to_origin"):
        coords.append((request["origin_lat"], request["origin_lng"]))
    return {"coordinates": coords, "effective_stops": all_stops}


async def routing_matrix_node(state: RouteState) -> dict[str, Any]:
    coords = state["coordinates"]
    request = state["request"]
    speed = request.get("average_speed_kmh", 45.0)
    time_mat, dist_mat = await get_transit_matrix(
        coords,
        speed,
        route_options=request,
    )
    return {"transit_matrix": time_mat, "distance_matrix": dist_mat}


async def constraint_node(state: RouteState) -> dict[str, Any]:
    request = state["request"]
    stops = state.get("effective_stops") or request.get("destinations", [])
    start_datetime = datetime.fromisoformat(request["start_datetime"])
    windows = apply_time_window_constraints(stops, start_datetime)
    return {"constraints": windows}


async def solver_node(state: RouteState) -> dict[str, Any]:
    req = state["request"]
    stops = state.get("effective_stops") or req.get("destinations", [])
    schedule = ItineraryScheduler.solve_schedule(
        origin_name=req["origin_name"],
        origin_lat=req["origin_lat"],
        origin_lng=req["origin_lng"],
        stops=stops,
        start_datetime=datetime.fromisoformat(req["start_datetime"]),
        transit_matrix=state["transit_matrix"],
        distance_matrix=state["distance_matrix"],
        time_windows=state["constraints"],
        return_to_origin=req.get("return_to_origin", False),
    )
    return {"optimized_route": schedule, "attempt_count": state.get("attempt_count", 0) + 1}


async def validation_node(state: RouteState) -> dict[str, Any]:
    route = state.get("optimized_route")
    if route and route.is_valid:
        settings = get_settings()
        provider = getattr(settings, "ROUTING_PROVIDER", "osrm")
        geometry = await get_route_geometry(
            [(stop.latitude, stop.longitude) for stop in route.stops],
            route_options=state["request"],
        )
        route = route.model_copy(
            update={"geometry_geojson": geometry, "routing_provider": provider}
        )
        return {"validation_status": "VALID", "optimized_route": route}
    return {"validation_status": "FAILED"}
