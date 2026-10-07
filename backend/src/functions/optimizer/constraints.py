"""Routing constraints definition."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RouteConstraints(BaseModel):
    max_duration_hours: float = 24.0
    avoid_toll_roads: bool = False
    preferred_start_time: datetime | None = None
    max_walking_distance_km: float = 2.0
    budget_limit_inr: float | None = None
