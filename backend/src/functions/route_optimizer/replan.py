"""Mid-trip dynamic route replanning engine.

Conforms strictly to specification 0005 AC-8:
- Accepts current traveler coordinates and departure timestamp.
- Freezes completed stops with historical arrival timestamps from prior route version.
- Injects current position as the new departure origin.
- Re-optimizes remaining uncompleted stops using constraint solving or multimodal transitions.
- Writes an immutable incremented route_version referencing previous_route_version.
- Enforces optimistic concurrency controls and tenant isolation against concurrent replans.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from contracts.money import Currency
from contracts.routing import (
    OptimizedRoute,
    RouteLegOutput,
    RouteReplanRequest,
    RouteRequest,
    RouteStatus,
    RouteStopInput,
    ScheduledStopOutput,
    SolverStatus,
    StopType,
)
from contracts.trips import TransportMode
from functions.orchestrator.routing.models import RouteModel
from functions.orchestrator.routing.repository import (
    RouteNotFoundError,
    RouteRepository,
    RouteVersionConflictError,
)
from functions.route_optimizer.continuity import haversine_distance_m
from functions.route_optimizer.multimodal import MultimodalTransitionService
from functions.route_optimizer.solver import OrToolsRoutingSolver
from functions.route_optimizer.valhalla import validate_coordinates

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensures a datetime is timezone-aware in UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class ReplanError(Exception):
    """Base exception for replanning operations."""


class ReplanValidationError(ReplanError, ValueError):
    """Raised when replan parameters violate domain constraints."""


class RouteReplanService:
    """Orchestrates dynamic mid-trip replanning, stop freezing, and version sequencing."""

    def __init__(
        self,
        *,
        multimodal_service: MultimodalTransitionService | None = None,
        solver: OrToolsRoutingSolver | None = None,
        route_repo: RouteRepository | None = None,
    ) -> None:
        self._multimodal_service = multimodal_service
        self._solver = solver or OrToolsRoutingSolver()
        self._route_repo = route_repo

    async def replan_route(
        self,
        previous_route: OptimizedRoute,
        request: RouteReplanRequest,
        *,
        durations_s: list[list[int | None]] | None = None,
        distances_m: list[list[int | None]] | None = None,
    ) -> OptimizedRoute:
        """Calculates a replanned route from current traveler coordinates, freezing completed stops."""
        # 1. Geographic boundary validation
        validate_coordinates(request.current_location)

        # 2. Optimistic concurrency check
        if (
            request.expected_previous_version is not None
            and previous_route.route_version != request.expected_previous_version
        ):
            raise RouteVersionConflictError(
                f"Optimistic concurrency check failed: route version is {previous_route.route_version}, "
                f"expected {request.expected_previous_version}"
            )

        replan_at = _ensure_utc(request.replan_at)
        completed_ids = set(request.completed_stop_ids)

        # 3. Partition existing stops into completed and remaining
        frozen_completed_stops: list[ScheduledStopOutput] = []
        remaining_candidates: list[ScheduledStopOutput] = []

        for stop in sorted(previous_route.stops, key=lambda s: s.stop_order):
            is_done = stop.id in completed_ids or (stop.stop_id in completed_ids if stop.stop_id else False)
            if is_done:
                # Freeze completed stop with historical planned_arrival_at and planned_departure_at
                frozen_stop = stop.model_copy(update={"is_completed": True})
                frozen_completed_stops.append(frozen_stop)
            else:
                # Exclude old origin if not marked completed; traveler departs from Current Location
                if stop.stop_type != StopType.ORIGIN:
                    remaining_candidates.append(stop)

        completed_stop_ids_set = {s.id for s in frozen_completed_stops} | {
            s.stop_id for s in frozen_completed_stops if s.stop_id is not None
        }

        # 4. Extract historical completed legs between consecutive completed stops
        historical_completed_legs: list[RouteLegOutput] = []
        for leg in sorted(previous_route.legs, key=lambda leg_item: leg_item.leg_order):
            if leg.from_stop_id in completed_stop_ids_set and leg.to_stop_id in completed_stop_ids_set:
                historical_completed_legs.append(leg)

        # 5. Handle edge case: all stops already completed
        next_route_version = previous_route.route_version + 1
        eff_transport_mode = request.transport_mode or TransportMode.CAR_PETROL
        eff_fuel_profile = request.fuel_profile

        if not remaining_candidates:
            # Re-index frozen stops and legs
            ordered_frozen = [
                s.model_copy(
                    update={
                        "id": s.stop_id or s.id,
                        "stop_id": s.stop_id or s.id,
                        "stop_order": idx,
                    }
                )
                for idx, s in enumerate(frozen_completed_stops)
            ]
            ordered_legs = [
                leg_item.model_copy(update={"leg_order": idx})
                for idx, leg_item in enumerate(historical_completed_legs)
            ]
            return previous_route.model_copy(
                update={
                    "id": uuid.uuid4(),
                    "route_version": next_route_version,
                    "previous_route_version": previous_route.route_version,
                    "status": RouteStatus.FEASIBLE,
                    "solver_status": SolverStatus.OPTIMAL,
                    "stops": ordered_frozen,
                    "legs": ordered_legs,
                    "warnings": previous_route.warnings + ["All stops were previously completed; no remaining stops to sequence"],
                }
            )

        # 6. Inject current location as the new departure origin stop
        replan_origin_id = uuid.uuid4()
        replan_origin_input = RouteStopInput(
            id=replan_origin_id,
            name="Current Location",
            location=request.current_location,
            stop_type=StopType.ORIGIN,
            dwell_seconds=0,
            is_completed=True,
        )

        remaining_stop_inputs: list[RouteStopInput] = []
        for s in remaining_candidates:
            orig_id = s.stop_id or s.id or uuid.uuid4()
            remaining_stop_inputs.append(
                RouteStopInput(
                    id=orig_id,
                    place_id=s.place_id,
                    name=s.name,
                    location=s.location,
                    stop_type=s.stop_type,
                    is_must_visit=s.is_must_visit,
                    window_start=s.window_start,
                    window_end=s.window_end,
                    dwell_seconds=s.dwell_seconds,
                    is_completed=False,
                )
            )

        sub_request_stops = [replan_origin_input] + remaining_stop_inputs

        sub_request = RouteRequest(
            owner_id=previous_route.owner_id,
            trip_id=previous_route.trip_id,
            execution_id=previous_route.execution_id,
            departure_at=replan_at,
            return_deadline=request.return_deadline or previous_route.return_deadline,
            stops=sub_request_stops,
            objective=request.objective or previous_route.objective,
            is_round_trip=False,
            time_limit_seconds=request.time_limit_seconds,
            transport_mode=eff_transport_mode,
            fuel_profile=eff_fuel_profile,
        )

        # 7. Solve the remaining route
        new_route_solution: OptimizedRoute
        if durations_s is not None and distances_m is not None:
            new_route_solution = self._solver.solve(sub_request, durations_s, distances_m)
        elif self._multimodal_service is not None:
            new_route_solution = await self._multimodal_service.plan_multimodal_route(sub_request)
        else:
            # Fallback to haversine matrix with OrToolsRoutingSolver
            speed_mps = 1.38889 if eff_transport_mode == TransportMode.WALKING else 12.5
            locs = [s.location for s in sub_request_stops]
            durs: list[list[int | None]] = []
            dists: list[list[int | None]] = []
            for o in locs:
                dur_row: list[int | None] = []
                dist_row: list[int | None] = []
                for d in locs:
                    dist = haversine_distance_m(o, d)
                    dur = max(1, int(round(dist / speed_mps))) if dist > 0 else 0
                    dist_row.append(dist)
                    dur_row.append(dur)
                durs.append(dur_row)
                dists.append(dist_row)

            new_route_solution = self._solver.solve(sub_request, durs, dists)

        # 8. Reconstruct and stitch full route: frozen completed stops + current location origin + newly scheduled stops
        all_stops: list[ScheduledStopOutput] = []
        stop_index = 0

        # A. Frozen completed stops
        for s in frozen_completed_stops:
            eff_id = s.stop_id or s.id
            all_stops.append(
                s.model_copy(
                    update={
                        "id": eff_id,
                        "stop_id": eff_id,
                        "stop_order": stop_index,
                        "is_completed": True,
                    }
                )
            )
            stop_index += 1

        # B. New origin and newly scheduled remaining stops
        for s in new_route_solution.stops:
            eff_id = s.stop_id or s.id
            all_stops.append(
                s.model_copy(
                    update={
                        "id": eff_id,
                        "stop_id": eff_id,
                        "stop_order": stop_index,
                        "is_completed": True if eff_id == replan_origin_id else False,
                    }
                )
            )
            stop_index += 1

        # 9. Stitch legs: historical completed legs + new remaining legs
        all_legs: list[RouteLegOutput] = []
        leg_index = 0

        for leg_item in historical_completed_legs:
            all_legs.append(leg_item.model_copy(update={"leg_order": leg_index}))
            leg_index += 1

        for leg_item in new_route_solution.legs:
            all_legs.append(leg_item.model_copy(update={"leg_order": leg_index}))
            leg_index += 1

        # 10. Recompute aggregate metrics across full itinerary
        total_dist_m = sum(leg_item.distance_m for leg_item in all_legs)
        total_travel_s = sum(leg_item.travel_seconds for leg_item in all_legs)
        total_dwell_s = sum(s.dwell_seconds for s in all_stops if not s.is_skipped)
        total_wait_s = sum(s.wait_seconds for s in all_stops if not s.is_skipped)

        driving_dist_m = sum(
            leg_item.distance_m for leg_item in all_legs if leg_item.transport_mode != TransportMode.WALKING
        )
        walking_dist_m = sum(
            leg_item.distance_m for leg_item in all_legs if leg_item.transport_mode == TransportMode.WALKING
        )

        if all_stops:
            first_departure = all_stops[0].planned_departure_at or previous_route.departure_at
            last_arrival = all_stops[-1].planned_arrival_at or first_departure
            actual_return_at: datetime | None = last_arrival
            total_elapsed_s = max(0, int((last_arrival - first_departure).total_seconds()))
        else:
            first_departure = replan_at
            last_arrival = replan_at
            actual_return_at = None
            total_elapsed_s = 0

        fuel_amount: Decimal | None = None
        fuel_basis: str | None = None
        fuel_currency = Currency.INR
        if eff_fuel_profile is not None and driving_dist_m > 0:
            driving_dist_km = (Decimal(str(driving_dist_m)) / Decimal("1000")).quantize(
                Decimal("0.001"), rounding=ROUND_HALF_UP
            )
            fuel_liters = (driving_dist_km / eff_fuel_profile.mileage_km_per_liter).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            fuel_amount = (fuel_liters * eff_fuel_profile.fuel_price_per_liter).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            fuel_basis = (
                f"fuel_{eff_fuel_profile.mode.value} "
                f"({driving_dist_km}km @ {eff_fuel_profile.mileage_km_per_liter}km/l)"
            )
            fuel_currency = eff_fuel_profile.currency

        # Combined warnings and diagnostics
        combined_warnings = list(previous_route.warnings) + [
            f"Mid-trip replan version {next_route_version} departing from current location at {replan_at.isoformat()}"
        ] + list(new_route_solution.warnings)

        combined_diagnostics = list(previous_route.diagnostics) + list(new_route_solution.diagnostics)

        # Reconcile dropped stops preserving historical audit records from prior route versions
        all_scheduled_ids = {s.id for s in all_stops} | {
            s.stop_id for s in all_stops if s.stop_id is not None
        }
        reconciled_dropped: list[ScheduledStopOutput] = [
            s.model_copy(update={"id": s.stop_id or s.id})
            for s in new_route_solution.dropped_stops
        ]
        seen_dropped_ids = {s.id for s in reconciled_dropped} | {
            s.stop_id for s in reconciled_dropped if s.stop_id is not None
        }
        for prev_dropped in previous_route.dropped_stops:
            p_id = prev_dropped.stop_id or prev_dropped.id
            if (
                prev_dropped.id not in all_scheduled_ids
                and (prev_dropped.stop_id is None or prev_dropped.stop_id not in all_scheduled_ids)
                and prev_dropped.id not in seen_dropped_ids
                and (prev_dropped.stop_id is None or prev_dropped.stop_id not in seen_dropped_ids)
            ):
                reconciled_dropped.append(
                    prev_dropped.model_copy(update={"id": p_id, "stop_id": p_id})
                )
                seen_dropped_ids.add(p_id)

        return OptimizedRoute(
            id=uuid.uuid4(),
            owner_id=previous_route.owner_id,
            trip_id=previous_route.trip_id,
            execution_id=previous_route.execution_id,
            route_version=next_route_version,
            previous_route_version=previous_route.route_version,
            status=new_route_solution.status,
            solver_status=new_route_solution.solver_status,
            objective=new_route_solution.objective,
            departure_at=first_departure,
            return_deadline=sub_request.return_deadline,
            actual_return_at=actual_return_at,
            total_distance_m=total_dist_m,
            total_travel_seconds=total_travel_s,
            total_dwell_seconds=total_dwell_s,
            total_wait_seconds=total_wait_s,
            total_elapsed_seconds=total_elapsed_s,
            driving_distance_m=driving_dist_m,
            walking_distance_m=walking_dist_m,
            fuel_amount=fuel_amount,
            fuel_currency=fuel_currency,
            fuel_basis=fuel_basis,
            stops=all_stops,
            legs=all_legs,
            restrictions=list(new_route_solution.restrictions),
            dropped_stops=reconciled_dropped,
            warnings=combined_warnings,
            assumptions=list(previous_route.assumptions),
            diagnostics=combined_diagnostics,
        )

    async def replan_and_persist(
        self,
        *,
        route_id: uuid.UUID,
        owner_id: uuid.UUID,
        request: RouteReplanRequest,
        route_repo: RouteRepository,
        durations_s: list[list[int | None]] | None = None,
        distances_m: list[list[int | None]] | None = None,
    ) -> RouteModel:
        """Loads previous route, verifies tenant ownership, performs replan, and persists new route version atomically."""
        # 1. Tenant-isolated lookup
        prev_route_model = await route_repo.get_route_by_id(route_id, owner_id=owner_id)
        if prev_route_model is None:
            raise RouteNotFoundError(
                f"Route {route_id} not found for owner {owner_id}"
            )

        previous_route = prev_route_model.to_optimized_route()

        # 2. Perform replanning calculation
        replanned_route = await self.replan_route(
            previous_route,
            request,
            durations_s=durations_s,
            distances_m=distances_m,
        )

        # 3. Persist new route version under optimistic concurrency controls
        new_route_model = await route_repo.save_route(
            replanned_route,
            owner_id=owner_id,
            trip_id=previous_route.trip_id,
            execution_id=previous_route.execution_id,
            route_version=replanned_route.route_version,
            previous_route_version=replanned_route.previous_route_version,
        )

        return new_route_model


__all__ = [
    "ReplanError",
    "ReplanValidationError",
    "RouteReplanService",
]
