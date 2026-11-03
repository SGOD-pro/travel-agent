"""Conditional routing policies for the optimizer workflow."""
from .state import RouteState


def route_validation_policy(state: RouteState) -> str:
    status = state.get("validation_status")
    attempts = state.get("attempt_count", 0)
    
    if status == "VALID":
        return "costs"
    elif attempts < 3:
        return "solver"
    else:
        return "fail"