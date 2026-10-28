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
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from config.config import settings
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
from functions.route_optimizer.closures import (
    ClosureAvoidanceService,
    RestrictionClaim,
    UnsupportedRestrictionError,
)
from functions.route_optimizer.multimodal import MultimodalTransitionService
from functions.route_optimizer.solver import OrToolsRoutingSolver
from functions.route_optimizer.valhalla import (
    ValhallaClient,
    ValhallaConnectionError,
    validate_coordinates,
)

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
        valhalla_client: Any | None = None,
        closure_service: ClosureAvoidanceService | None = None,
        solver: OrToolsRoutingSolver | None = None,
        route_repo: RouteRepository | None = None,
    ) -> None:
        self._valhalla_client = valhalla_client or ValhallaClient()
        self._closure_service = closure_service or ClosureAvoidanceService()
        self._multimodal_service = multimodal_service or MultimodalTransitionService(
            valhalla_client=self._valhalla_client,
            closure_service=self._closure_service,
        )
        self._solver = solver or OrToolsRoutingSolver()
        self._route_repo = route_repo

    async def replan_route(
        self,
        previous_route: OptimizedRoute,
        request: RouteReplanRequest,
        *,
        durations_s: list[list[int | None]] | None = None,
        distances_m: list[list[int | None]] | None = None,
        restrictions: list[RestrictionClaim] | None = None,
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
            is_done = stop.id in completed_ids or (
                stop.stop_id in completed_ids if stop.stop_id else False
            )
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
            if (
                leg.from_stop_id in completed_stop_ids_set
                and leg.to_stop_id in completed_stop_ids_set
            ):
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
                    "warnings": previous_route.warnings
                    + ["All stops were previously completed; no remaining stops to sequence"],
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

        # Inspect previous route for multimodal indicators (walking legs or parking hubs)
        pedestrian_stop_ids = {
            leg.to_stop_id
            for leg in (previous_route.legs or [])
            if leg.transport_mode == TransportMode.WALKING
        }
        has_parking_hub = any(s.stop_type == StopType.PARKING_HUB for s in (previous_route.stops or []))

        remaining_stop_inputs: list[RouteStopInput] = []
        for s in remaining_candidates:
            # Omit previously injected parking hub stop; multimodal service will inject a verified parking hub dynamically
            if s.stop_type == StopType.PARKING_HUB:
                continue

            orig_id = s.stop_id or s.id or uuid.uuid4()
            is_ped = (
                s.id in pedestrian_stop_ids
                or s.stop_id in pedestrian_stop_ids
                or (has_parking_hub and s.stop_type == StopType.DESTINATION)
            )

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
                    is_pedestrian_only=is_ped,
                )
            )

        # If previous route started from a depot/origin, append return to home depot
        if previous_route.stops and previous_route.stops[0].stop_type == StopType.ORIGIN:
            home_stop = previous_route.stops[0]
            clean_home_name = home_stop.name
            if clean_home_name.lower().startswith("return to "):
                clean_home_name = clean_home_name[10:].strip()
            if not any(s.name in (home_stop.name, f"Return to {clean_home_name}") for s in remaining_stop_inputs):
                remaining_stop_inputs.append(
                    RouteStopInput(
                        id=uuid.uuid4(),
                        place_id=home_stop.place_id,
                        name=f"Return to {clean_home_name}",
                        location=home_stop.location,
                        stop_type=StopType.DESTINATION,
                        dwell_seconds=0,
                        is_must_visit=True,
                        is_pedestrian_only=False,
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
        solved_with_multimodal = False
        if durations_s is not None and distances_m is not None:
            new_route_solution = self._solver.solve(sub_request, durations_s, distances_m)
        elif self._multimodal_service is not None and any(
            s.is_pedestrian_only or s.vehicle_restricted for s in sub_request_stops
        ):
            new_route_solution = await self._multimodal_service.plan_multimodal_route(
                sub_request, restrictions=restrictions
            )
            solved_with_multimodal = True
        elif self._valhalla_client is not None or (
            hasattr(settings, "VALHALLA_ENDPOINT_URL") and settings.VALHALLA_ENDPOINT_URL
        ):
            client = self._valhalla_client or ValhallaClient()
            if (
                hasattr(client, "base_url")
                and not client.base_url
                and not hasattr(client, "canned_matrix")
            ):
                raise ValhallaConnectionError(
                    "VALHALLA_ENDPOINT_URL is not configured; cannot query external routing service"
                )
            locs = [s.location for s in sub_request_stops]
            matrix = await client.sources_to_targets(locs, locs, mode=eff_transport_mode)
            new_route_solution = self._solver.solve(
                sub_request, matrix.durations_s, matrix.distances_m
            )
        elif self._multimodal_service is not None:
            new_route_solution = await self._multimodal_service.plan_multimodal_route(
                sub_request, restrictions=restrictions
            )
            solved_with_multimodal = True
        else:
            raise ValhallaConnectionError(
                "VALHALLA_ENDPOINT_URL is not configured; cannot query external routing service"
            )

        if (
            not solved_with_multimodal
            and new_route_solution.status in (RouteStatus.FEASIBLE, RouteStatus.PARTIAL)
            and new_route_solution.legs
        ):
            client = self._valhalla_client
            if client is not None and hasattr(client, "route"):
                new_route_solution = await self._hydrate_new_legs(
                    new_route_solution=new_route_solution,
                    sub_request=sub_request,
                    client=client,
                    restrictions=restrictions,
                )

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
        if (
            new_route_solution.status not in (RouteStatus.INFEASIBLE, RouteStatus.UNAVAILABLE)
            or frozen_completed_stops
        ):
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

        if (
            new_route_solution.status not in (RouteStatus.INFEASIBLE, RouteStatus.UNAVAILABLE)
            or frozen_completed_stops
        ):
            for leg_item in new_route_solution.legs:
                all_legs.append(leg_item.model_copy(update={"leg_order": leg_index}))
                leg_index += 1

        # 10. Recompute aggregate metrics across full itinerary
        total_dist_m = sum(leg_item.distance_m for leg_item in all_legs)
        total_travel_s = sum(leg_item.travel_seconds for leg_item in all_legs)
        total_dwell_s = sum(s.dwell_seconds for s in all_stops if not s.is_skipped)
        total_wait_s = sum(s.wait_seconds for s in all_stops if not s.is_skipped)

        driving_dist_m = sum(
            leg_item.distance_m
            for leg_item in all_legs
            if leg_item.transport_mode != TransportMode.WALKING
        )
        walking_dist_m = sum(
            leg_item.distance_m
            for leg_item in all_legs
            if leg_item.transport_mode == TransportMode.WALKING
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
        combined_warnings = (
            list(previous_route.warnings)
            + [
                f"Mid-trip replan version {next_route_version} departing from current location at {replan_at.isoformat()}"
            ]
            + list(new_route_solution.warnings)
        )

        combined_diagnostics = list(previous_route.diagnostics) + list(
            new_route_solution.diagnostics
        )

        # Reconcile dropped stops preserving historical audit records from prior route versions
        all_scheduled_ids = {s.id for s in all_stops} | {
            s.stop_id for s in all_stops if s.stop_id is not None
        }
        reconciled_dropped: list[ScheduledStopOutput] = [
            s.model_copy(update={"id": s.stop_id or s.id}) for s in new_route_solution.dropped_stops
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

        reconstructed_route = OptimizedRoute(
            id=uuid.uuid4(),
            owner_id=previous_route.owner_id,
            trip_id=previous_route.trip_id,
            execution_id=previous_route.execution_id,
            parent_route_id=previous_route.id,
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

        if self._closure_service is not None and restrictions:
            reconstructed_route = self._closure_service.apply_and_revalidate(
                reconstructed_route, restrictions
            )

        return reconstructed_route

    async def _hydrate_new_legs(
        self,
        *,
        new_route_solution: OptimizedRoute,
        sub_request: RouteRequest,
        client: Any,
        restrictions: list[RestrictionClaim] | None = None,
    ) -> OptimizedRoute:
        """Hydrates real provider road/walking geometries for newly scheduled legs with closure exclusions."""
        leg_polys: list[list[list[float]]] = []
        if restrictions and self._closure_service is not None:
            active_restrs = [r for r in restrictions if r.is_active and r.is_verified]
            leg_polys, _, diags = self._closure_service.extract_valhalla_exclude_polygons(
                active_restrs
            )
            if diags:
                raise UnsupportedRestrictionError(diags[0])

        stops_by_id = {s.id: s for s in new_route_solution.stops}
        stops_by_stop_id = {s.stop_id: s for s in new_route_solution.stops if s.stop_id is not None}
        input_stops_by_id = {s.id: s for s in sub_request.stops}

        current_time = sub_request.departure_at
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        hydrated_legs: list[RouteLegOutput] = []
        updated_stops: list[ScheduledStopOutput] = []

        total_distance_m: int = 0
        total_travel_s: int = 0
        total_dwell_s: int = 0
        total_wait_s: int = 0

        # Departure from new origin (current location)
        origin_stop = new_route_solution.stops[0]
        dep_time = current_time + timedelta(seconds=origin_stop.dwell_seconds)
        updated_stops.append(
            origin_stop.model_copy(
                update={
                    "planned_arrival_at": current_time,
                    "planned_departure_at": dep_time,
                    "wait_seconds": 0,
                }
            )
        )
        current_time = dep_time
        total_dwell_s += origin_stop.dwell_seconds

        for leg in new_route_solution.legs:
            from_stop = stops_by_id.get(leg.from_stop_id) or stops_by_stop_id.get(leg.from_stop_id)
            to_stop = stops_by_id.get(leg.to_stop_id) or stops_by_stop_id.get(leg.to_stop_id)
            if from_stop is None or to_stop is None:
                continue

            route_res = await client.route(
                locations=[from_stop.location, to_stop.location],
                mode=leg.transport_mode,
                exclude_polygons=leg_polys or None,
            )
            leg_dist = route_res.total_distance_m
            leg_dur = route_res.total_travel_seconds
            leg_geom = route_res.legs[0].geometry if route_res.legs else route_res.geometry

            leg_dep = current_time
            arr_time = leg_dep + timedelta(seconds=leg_dur)

            target_input = input_stops_by_id.get(to_stop.id) or (
                input_stops_by_id.get(to_stop.stop_id) if to_stop.stop_id is not None else None
            )
            dwell = target_input.dwell_seconds if target_input else to_stop.dwell_seconds
            wait_s = 0
            if target_input and target_input.window_start:
                w_start = _ensure_utc(target_input.window_start)
                if arr_time < w_start:
                    wait_s = int((w_start - arr_time).total_seconds())

            service_start = arr_time + timedelta(seconds=wait_s)
            target_dep = service_start + timedelta(seconds=dwell)

            # Hard service window revalidation: dwell completion before closing
            if target_input and target_input.window_end:
                w_end = _ensure_utc(target_input.window_end)
                if w_end and target_dep > w_end:
                    if target_input.is_must_visit:
                        return new_route_solution.model_copy(
                            update={
                                "status": RouteStatus.INFEASIBLE,
                                "solver_status": SolverStatus.INFEASIBLE,
                                "actual_return_at": target_dep,
                                "stops": [],
                                "dropped_stops": [
                                    s.model_copy(
                                        update={
                                            "is_skipped": True,
                                            "skip_reason": (
                                                f"Service window breached at mandatory stop '{to_stop.name}'"
                                                if s.name == to_stop.name
                                                else "INFEASIBLE_SCHEDULE"
                                            ),
                                        }
                                    )
                                    for s in updated_stops
                                ],
                                "legs": [],
                                "diagnostics": new_route_solution.diagnostics
                                + [
                                    f"SCHEDULE_INFEASIBLE: Service at mandatory stop '{to_stop.name}' finishes at {target_dep.isoformat()}, exceeding closing time {w_end.isoformat()}"
                                ],
                                "warnings": new_route_solution.warnings
                                + [f"Service window breached at mandatory stop '{to_stop.name}'"],
                            }
                        )
                    else:
                        # Optional stop breached closing time: drop and re-solve with remaining stops
                        reduced_stops = [s for s in sub_request.stops if s.id != target_input.id]
                        reduced_sub_request = sub_request.model_copy(
                            update={"stops": reduced_stops}
                        )
                        reduced_locs = [s.location for s in reduced_stops]
                        reduced_matrix = await client.sources_to_targets(
                            reduced_locs, reduced_locs, mode=sub_request.transport_mode
                        )
                        reduced_solution = self._solver.solve(
                            reduced_sub_request,
                            reduced_matrix.durations_s,
                            reduced_matrix.distances_m,
                        )
                        if hasattr(client, "route") and reduced_solution.status in (
                            RouteStatus.FEASIBLE,
                            RouteStatus.PARTIAL,
                        ):
                            reduced_solution = await self._hydrate_new_legs(
                                new_route_solution=reduced_solution,
                                sub_request=reduced_sub_request,
                                client=client,
                                restrictions=restrictions,
                            )
                        dropped_stop = ScheduledStopOutput(
                            id=target_input.id,
                            stop_id=target_input.id,
                            place_id=target_input.place_id,
                            stop_order=len(reduced_solution.stops),
                            name=target_input.name,
                            location=target_input.location,
                            stop_type=target_input.stop_type,
                            is_must_visit=False,
                            is_skipped=True,
                            skip_reason="TIME_WINDOW_OR_DROP_PENALTY",
                            window_start=target_input.window_start,
                            window_end=target_input.window_end,
                            dwell_seconds=target_input.dwell_seconds,
                        )
                        final_status = (
                            RouteStatus.PARTIAL
                            if reduced_solution.status == RouteStatus.FEASIBLE
                            else reduced_solution.status
                        )
                        final_solver_status = (
                            SolverStatus.FEASIBLE
                            if reduced_solution.status == RouteStatus.FEASIBLE
                            else reduced_solution.solver_status
                        )
                        return reduced_solution.model_copy(
                            update={
                                "status": final_status,
                                "solver_status": final_solver_status,
                                "stops": reduced_solution.stops + [dropped_stop],
                                "dropped_stops": reduced_solution.dropped_stops + [dropped_stop],
                                "diagnostics": reduced_solution.diagnostics
                                + [
                                    f"Optional stop '{to_stop.name}' dropped after detour exceeded closing time {w_end.isoformat()}"
                                ],
                                "warnings": reduced_solution.warnings
                                + [f"Optional stop '{to_stop.name}' dropped due to closure detour"],
                            }
                        )

            updated_stops.append(
                to_stop.model_copy(
                    update={
                        "planned_arrival_at": arr_time,
                        "planned_departure_at": target_dep,
                        "wait_seconds": wait_s,
                        "dwell_seconds": dwell,
                    }
                )
            )
            total_dwell_s += dwell
            total_wait_s += wait_s
            current_time = target_dep

            hydrated_legs.append(
                leg.model_copy(
                    update={
                        "departure_at": leg_dep,
                        "arrival_at": arr_time,
                        "distance_m": leg_dist,
                        "travel_seconds": leg_dur,
                        "geometry": leg_geom,
                    }
                )
            )
            total_distance_m += leg_dist
            total_travel_s += leg_dur

        # Preserve dropped stops in updated_stops
        for s in new_route_solution.stops:
            if getattr(s, "is_skipped", False) and s.id not in {us.id for us in updated_stops}:
                updated_stops.append(s)

        if sub_request.return_deadline is not None:
            ret_dl = _ensure_utc(sub_request.return_deadline)
            if ret_dl and current_time > ret_dl:
                return new_route_solution.model_copy(
                    update={
                        "status": RouteStatus.INFEASIBLE,
                        "solver_status": SolverStatus.INFEASIBLE,
                        "actual_return_at": current_time,
                        "stops": [],
                        "dropped_stops": [
                            s.model_copy(
                                update={
                                    "is_skipped": True,
                                    "skip_reason": "Return deadline breached",
                                }
                            )
                            for s in updated_stops
                        ],
                        "legs": [],
                        "diagnostics": new_route_solution.diagnostics
                        + [
                            f"SCHEDULE_INFEASIBLE: Replan route completes at {current_time.isoformat()}, exceeding return deadline {ret_dl.isoformat()}"
                        ],
                        "warnings": new_route_solution.warnings + ["Return deadline breached"],
                    }
                )

        return new_route_solution.model_copy(
            update={
                "stops": updated_stops,
                "legs": hydrated_legs,
                "total_distance_m": total_distance_m,
                "total_travel_seconds": total_travel_s,
                "total_dwell_seconds": total_dwell_s,
                "total_wait_seconds": total_wait_s,
            }
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
        restrictions: list[RestrictionClaim] | None = None,
        execution_id: uuid.UUID | None = None,
    ) -> RouteModel:
        """Loads previous route, verifies tenant ownership, performs replan, and persists new route version atomically."""
        # 1. Tenant-isolated lookup
        prev_route_model = await route_repo.get_route_by_id(route_id, owner_id=owner_id)
        if prev_route_model is None:
            raise RouteNotFoundError(f"Route {route_id} not found for owner {owner_id}")

        previous_route = prev_route_model.to_optimized_route()

        # 2. Perform replanning calculation
        replanned_route = await self.replan_route(
            previous_route,
            request,
            durations_s=durations_s,
            distances_m=distances_m,
            restrictions=restrictions,
        )

        if replanned_route.status == RouteStatus.INFEASIBLE:
            infeasible_reason = (
                "MANDATORY_STOP_WINDOW_BREACH"
                if any(
                    "cannot complete service" in d or "window" in d.lower()
                    for d in replanned_route.diagnostics
                )
                else (
                    replanned_route.diagnostics[0][:64]
                    if replanned_route.diagnostics
                    else "MANDATORY_CONSTRAINT_INFEASIBLE"
                )
            )
            completed_ids = set(request.completed_stop_ids or [])
            existing_stops_by_name = {
                s.name: s
                for s in (list(replanned_route.stops) + list(replanned_route.dropped_stops))
            }
            source_stops = previous_route.stops or replanned_route.stops or []
            infeasible_completed_stops = []
            infeasible_dropped_stops = []
            for idx, s in enumerate(source_stops):
                prev_s = existing_stops_by_name.get(s.name)
                s_id = (
                    getattr(prev_s, "id", None)
                    or getattr(s, "id", None)
                    or uuid.uuid4()
                )
                was_completed = (
                    s_id in completed_ids
                    or getattr(s, "id", None) in completed_ids
                    or getattr(s, "stop_id", None) in completed_ids
                    or (getattr(prev_s, "is_completed", False) if prev_s else False)
                    or getattr(s, "is_completed", False)
                )
                stop_out = ScheduledStopOutput(
                    id=s_id,
                    stop_id=getattr(s, "stop_id", None) or s_id,
                    place_id=s.place_id,
                    stop_order=idx + 1,
                    name=s.name,
                    location=s.location,
                    stop_type=s.stop_type,
                    is_must_visit=s.is_must_visit,
                    is_completed=was_completed,
                    is_skipped=not was_completed,
                    skip_reason=None if was_completed else (getattr(prev_s, "skip_reason", None) or infeasible_reason),
                    window_start=s.window_start,
                    window_end=s.window_end,
                    dwell_seconds=s.dwell_seconds,
                    planned_arrival_at=getattr(prev_s, "planned_arrival_at", None) or getattr(s, "planned_arrival_at", None),
                    planned_departure_at=getattr(prev_s, "planned_departure_at", None) or getattr(s, "planned_departure_at", None),
                )
                if was_completed:
                    infeasible_completed_stops.append(stop_out)
                else:
                    infeasible_dropped_stops.append(stop_out)

            replanned_route = replanned_route.model_copy(
                update={
                    "stops": infeasible_completed_stops,
                    "dropped_stops": infeasible_dropped_stops,
                    "legs": [],
                }
            )

        new_route_model = await route_repo.save_route(
            replanned_route,
            owner_id=owner_id,
            trip_id=previous_route.trip_id,
            execution_id=execution_id or previous_route.execution_id,
            parent_route_id=previous_route.id,
            route_version=replanned_route.route_version,
            previous_route_version=replanned_route.previous_route_version,
        )

        return new_route_model


__all__ = [
    "ReplanError",
    "ReplanValidationError",
    "RouteReplanService",
]
