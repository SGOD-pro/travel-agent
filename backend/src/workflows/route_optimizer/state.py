"""State schema for the Route Optimizer LangGraph workflow."""
from typing import Any, TypedDict


class RouteState(TypedDict):
    request: dict[str, Any]
    coordinates: list[tuple[float, float]]
    transit_matrix: list[list[int]] | None
    distance_matrix: list[list[float]] | None
    constraints: list[Any]
    optimized_route: Any | None
    validation_status: str
    attempt_count: int
