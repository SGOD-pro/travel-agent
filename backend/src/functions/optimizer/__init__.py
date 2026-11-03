"""Optimizer package exports."""

from functions.optimizer.solver import (
    ItineraryScheduler,
    OptimizedRoute,      # <-- FIXED: Renamed from OptimizedSchedule
    ScheduledStop,
    ScheduledLeg,
)

# Optional: Export schemas if they are defined
try:
    from functions.optimizer.schemas import RouteRequest, RouteResponse
except ImportError:
    pass

__all__ = [
    "ItineraryScheduler",
    "OptimizedRoute",
    "ScheduledStop",
    "ScheduledLeg",
    "RouteRequest",
    "RouteResponse",
]