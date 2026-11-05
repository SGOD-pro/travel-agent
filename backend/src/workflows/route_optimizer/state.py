"""State schema for the Route Optimizer LangGraph workflow."""
from typing import Any, TypedDict


class RouteState(TypedDict, total=False):
    request: dict[str, Any]
    coordinates: list[tuple[float, float]]
    effective_stops: list[dict[str, Any]]
    transit_matrix: list[list[int]] | None
    distance_matrix: list[list[float]] | None
    constraints: list[Any]
    optimized_route: Any | None
    validation_status: str
    attempt_count: int
