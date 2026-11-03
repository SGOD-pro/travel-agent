"""Execution nodes for the Route Optimizer workflow."""
import logging
from datetime import datetime
from .state import RouteState
from functions.optimizer.routing import get_transit_matrix
from functions.optimizer.constraints import apply_time_window_constraints
from functions.optimizer.solver import ItineraryScheduler

logger = logging.getLogger(__name__)


async def geocode_node(state: RouteState) -> dict:
    coords = [(0.0, 0.0)]
    return {"coordinates": coords}


async def routing_matrix_node(state: RouteState) -> dict:
    coords = state["coordinates"]
    speed = state["request"].get("average_speed_kmh", 45.0)
    time_mat, dist_mat = await get_transit_matrix(coords, speed)
    return {"transit_matrix": time_mat, "distance_matrix": dist_mat}


async def constraint_node(state: RouteState) -> dict:
    windows = apply_time_window_constraints(state["request"].get("destinations", []), datetime.now())
    return {"constraints": windows}


async def solver_node(state: RouteState) -> dict:
    req = state["request"]
    schedule = ItineraryScheduler.solve_schedule(
        origin_name=req["origin_name"],
        origin_lat=req["origin_lat"],
        origin_lng=req["origin_lng"],
        stops=req["destinations"],
        start_datetime=datetime.fromisoformat(req["start_datetime"]),
        transit_matrix=state["transit_matrix"],
        distance_matrix=state["distance_matrix"],
        time_windows=state["constraints"],
    )
    return {"optimized_route": schedule, "attempt_count": state.get("attempt_count", 0) + 1}


async def validation_node(state: RouteState) -> dict:
    route = state.get("optimized_route")
    if route and route.is_valid:
        return {"validation_status": "VALID"}
    return {"validation_status": "FAILED"}