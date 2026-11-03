# backend/src/functions/optimizer/constraints.py
"""Constraint handlers for time windows, festivals, and closures."""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from contracts.routing import TimeWindow  # Ensure TimeWindow is imported if used


def apply_time_window_constraints(
    destinations: List[dict[str, Any]], 
    start_datetime: datetime
) -> List[TimeWindow]:
    """Applies time window constraints (e.g. opening hours) to destinations.

    Args:
        destinations: List of destination dicts (must contain 'time_window' if applicable).
        start_datetime: The start datetime of the trip.

    Returns:
        List[TimeWindow]: A list of TimeWindow constraints for the solver.
    """
    windows: List[TimeWindow] = []
    
    # Origin (index 0) typically has no time window constraint
    windows.append(TimeWindow(start_minute=0, end_minute=1439))

    for dest in destinations:
        # If a destination specifies a time window, use it; otherwise, default to all day
        tw = dest.get("time_window")
        if tw and isinstance(tw, dict):
            windows.append(TimeWindow(
                start_minute=tw.get("start_minute", 0),
                end_minute=tw.get("end_minute", 1439)
            ))
        else:
            # Default to full day availability
            windows.append(TimeWindow(start_minute=0, end_minute=1439))

    return windows