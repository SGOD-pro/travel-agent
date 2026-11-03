"""State schema for the Route Optimizer LangGraph workflow."""
from typing import TypedDict, List, Optional, Any

class RouteState(TypedDict):
    request: dict
    coordinates: List[tuple]
    transit_matrix: Optional[List[List[int]]]
    distance_matrix: Optional[List[List[float]]]
    constraints: List[Any]
    optimized_route: Optional[Any]
    validation_status: str
    attempt_count: int