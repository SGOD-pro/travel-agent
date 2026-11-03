# backend/src/functions/optimizer/schemas.py
"""Pydantic schemas for the Route Optimizer."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

# FIXED: Changed to relative import to resolve ModuleNotFoundError
from .solver import OptimizedRoute, ScheduledStop, ScheduledLeg


class RouteRequest(BaseModel):
    """Input payload for route optimization."""
    origin_name: str
    origin_lat: float
    origin_lng: float
    destinations: List[dict] = Field(..., description="List of stops with lat, lng, name, stay_minutes")
    start_datetime: str  # ISO format string
    average_speed_kmh: float = 45.0
    vehicle_mileage_kmpl: float = 15.0
    fuel_price_per_liter: float = 102.5


class RouteResponse(BaseModel):
    """Wrapper response for the API."""
    success: bool
    message: str
    data: Optional[OptimizedRoute] = None