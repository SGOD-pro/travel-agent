# backend/src/functions/optimizer/service.py
"""Route Optimizer Service chaining routing, constraints, solver, and costs."""
from __future__ import annotations

import logging
from datetime import datetime

from .schemas import RouteRequest, RouteResponse
from .solver import ItineraryScheduler
from .routing import get_transit_matrix
from .constraints import apply_time_window_constraints

logger = logging.getLogger(__name__)


class RouteOptimizerService:
    """Service handling route optimization workflows."""

    @staticmethod
    async def optimize_route(req: RouteRequest) -> RouteResponse:
        try:
            # 1. Parse datetime
            start_dt = datetime.fromisoformat(req.start_datetime)

            # 2. Fetch Route Matrix via Routing API
            coords = [(req.origin_lat, req.origin_lng)] + [
                (d["lat"], d["lng"]) for d in req.destinations
            ]
            time_matrix, dist_matrix = await get_transit_matrix(coords, req.average_speed_kmh)

            # 3. Apply Constraints (Festivals, closures, opening hours)
            time_windows = apply_time_window_constraints(req.destinations, start_dt)

            # 4. Solve TSP / TSPTW using OR-Tools
            schedule = ItineraryScheduler.solve_schedule(
                origin_name=req.origin_name,
                origin_lat=req.origin_lat,
                origin_lng=req.origin_lng,
                stops=req.destinations,
                start_datetime=start_dt,  # <-- FIXED: Changed from start_datetime to start_dt
                transit_matrix=time_matrix,
                distance_matrix=dist_matrix,
                time_windows=time_windows,
                fuel_price_per_liter=req.fuel_price_per_liter,
                vehicle_mileage_kmpl=req.vehicle_mileage_kmpl,
            )

            # 5. Use the total cost calculated by the solver (includes fuel, tolls, entry fees)
            # The solver already returns the exact total_cost_inr in the OptimizedRoute model
            return RouteResponse(
                success=True,
                message="Route optimized successfully.",
                data=schedule
            )
        except Exception as e:
            logger.exception("Optimization failed")
            return RouteResponse(success=False, message=str(e))