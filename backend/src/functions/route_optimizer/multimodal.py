"""Multimodal transition service and parking hub injection.

Conforms strictly to specification 0005 AC-4:
- Identifies pedestrian-only or vehicle-restricted destinations.
- Automatically searches canonical_places for verified parking places within threshold.
- Injects PARKING_HUB stops and splits transit into driving and walking legs.
- Supports walk chaining between consecutive pedestrian destinations in the same cluster.
- Enforces returning to the vehicle before onward driving departures.
- Computes fuel strictly on driving distance (zero fuel for walking legs).
- Fails closed with status INFEASIBLE and diagnostic NO_VERIFIED_PARKING_HUB if missing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from config.config import settings
from contracts.routing import (
    OptimizedRoute,
    Point,
    RouteLegOutput,
    RouteRequest,
    RouteStatus,
    ScheduledStopOutput,
    SolverStatus,
    StopType,
    TrafficBasis,
    VehicleFuelProfile,
)
from contracts.trips import TransportMode
from functions.orchestrator.places.models import CanonicalPlaceModel
from functions.orchestrator.places.repository import CanonicalPlacesRepository
from functions.orchestrator.places.schemas import CoordinatePoint
from functions.route_optimizer.closures import (
    ClosureAvoidanceService,
    RestrictionClaim,
    UnsupportedRestrictionError,
    _ensure_utc,
)
from functions.route_optimizer.continuity import (
    VehicleContinuityTracker,
    haversine_distance_m,
)
from functions.route_optimizer.valhalla import (
    ValhallaClient,
    ValhallaConnectionError,
)

logger = logging.getLogger(__name__)


class MultimodalTransitionService:
    """Orchestrates parking hub identification, transit splitting, and multimodal continuity."""

    def __init__(
        self,
        places_repo: CanonicalPlacesRepository | None = None,
        valhalla_client: Any | None = None,
        search_radius_meters: float | None = None,
        search_parking_fn: (
            Callable[[Point, float], Awaitable[list[tuple[CanonicalPlaceModel, float]]]] | None
        ) = None,
        closure_service: ClosureAvoidanceService | None = None,
    ) -> None:
        self._places_repo = places_repo
        self._valhalla_client = valhalla_client or ValhallaClient()
        self._closure_service = closure_service or ClosureAvoidanceService()
        self._search_radius_meters = (
            search_radius_meters
            if search_radius_meters is not None
            else float(settings.DEFAULT_PARKING_SEARCH_RADIUS_METERS)
        )
        self._search_parking_fn = search_parking_fn

    def _ensure_provider(self) -> Any:
        client = self._valhalla_client
        if client is None:
            raise ValhallaConnectionError(
                "VALHALLA_ENDPOINT_URL is not configured; cannot query external routing service"
            )
        if hasattr(client, "base_url") and not client.base_url and not hasattr(client, "canned_matrix"):
            raise ValhallaConnectionError(
                "VALHALLA_ENDPOINT_URL is not configured; cannot query external routing service"
            )
        return client

    async def _route_leg(
        self,
        from_loc: Point,
        to_loc: Point,
        mode: TransportMode,
        restrictions: list[RestrictionClaim] | None = None,
    ) -> tuple[int, int, dict[str, Any]]:
        client = self._ensure_provider()

        leg_polys: list[list[list[float]]] = []
        if restrictions:
            active_restrs = [r for r in restrictions if r.is_active and r.is_verified]
            leg_polys, _, diags = self._closure_service.extract_valhalla_exclude_polygons(active_restrs)
            if diags:
                raise UnsupportedRestrictionError(diags[0])

        route_res = await client.route(
            locations=[from_loc, to_loc],
            mode=mode,
            exclude_polygons=leg_polys or None,
        )
        dist_m = route_res.total_distance_m
        if dist_m == 0 and (from_loc.latitude != to_loc.latitude or from_loc.longitude != to_loc.longitude):
            dist_m = max(1, haversine_distance_m(from_loc, to_loc))
        dur_s = max(1, route_res.total_travel_seconds)
        geom = route_res.legs[0].geometry if route_res.legs else route_res.geometry
        return dist_m, dur_s, geom

    async def find_verified_parking_hub(
        self,
        location: Point,
        search_radius_meters: float | None = None,
    ) -> tuple[CanonicalPlaceModel, float] | None:
        """Finds the nearest verified public parking hub in canonical_places."""
        radius = (
            search_radius_meters
            if search_radius_meters is not None
            else self._search_radius_meters
        )

        if self._search_parking_fn is not None:
            results = await self._search_parking_fn(location, radius)
            return results[0] if results else None

        if self._places_repo is not None:
            coord = CoordinatePoint(lng=location.longitude, lat=location.latitude)
            results = await self._places_repo.find_verified_parking_near(
                coord, max_distance_meters=radius, limit=5
            )
            return results[0] if results else None

        return None

    async def plan_multimodal_route(
        self,
        request: RouteRequest,
        restrictions: list[RestrictionClaim] | None = None,
    ) -> OptimizedRoute:
        """Plans a multimodal itinerary enforcing vehicle continuity and parking hub injection."""
        stops = request.stops
        if not stops:
            return OptimizedRoute(
                departure_at=request.departure_at,
                status=RouteStatus.UNAVAILABLE,
                solver_status=SolverStatus.ERROR,
                diagnostics=["At least one stop is required to plan a route"],
            )

        fuel_profile = request.fuel_profile or VehicleFuelProfile.default_for_mode(
            request.transport_mode
        )
        is_driving_trip = request.transport_mode in (
            TransportMode.CAR_PETROL,
            TransportMode.MOTORCYCLE_PETROL,
        )

        # 1. Inspect pedestrian stops and find verified parking hubs
        pedestrian_stops = [s for s in stops if (s.is_pedestrian_only or s.vehicle_restricted)]
        hub_for_stop: dict[uuid.UUID, tuple[CanonicalPlaceModel, float]] = {}

        if is_driving_trip and pedestrian_stops:
            for s in pedestrian_stops:
                hub_match = await self.find_verified_parking_hub(s.location)
                if hub_match is None:
                    diagnostic = (
                        f"NO_VERIFIED_PARKING_HUB: Destination '{s.name}' lies in a pedestrian-only "
                        f"or vehicle-restricted precinct, but zero verified parking places exist "
                        f"in canonical_places within {int(self._search_radius_meters)} meters"
                    )
                    logger.warning(diagnostic)
                    return OptimizedRoute(
                        owner_id=request.owner_id,
                        trip_id=request.trip_id,
                        execution_id=request.execution_id,
                        route_version=1,
                        status=RouteStatus.INFEASIBLE,
                        solver_status=SolverStatus.INFEASIBLE,
                        objective=request.objective,
                        departure_at=request.departure_at,
                        return_deadline=request.return_deadline,
                        diagnostics=[diagnostic],
                        warnings=["Route cannot be completed by vehicle without verified parking"],
                        assumptions=["Fails closed without inventing street parking"],
                    )
                hub_for_stop[s.id] = hub_match

        # 2. Build multimodal stop and leg schedule
        scheduled_stops: list[ScheduledStopOutput] = []
        scheduled_legs: list[RouteLegOutput] = []

        current_time = request.departure_at
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        total_driving_m: int = 0
        total_walking_m: int = 0
        total_travel_s: int = 0
        total_dwell_s: int = 0
        total_wait_s: int = 0

        # Cluster and schedule stops
        active_parking_hub: ScheduledStopOutput | None = None
        current_traveler_stop: ScheduledStopOutput | None = None

        # Add origin stop
        origin_input = stops[0]
        origin_scheduled = ScheduledStopOutput(
            id=origin_input.id,
            stop_id=origin_input.id,
            place_id=origin_input.place_id,
            stop_order=0,
            name=origin_input.name,
            location=origin_input.location,
            stop_type=origin_input.stop_type,
            is_must_visit=origin_input.is_must_visit,
            planned_arrival_at=current_time,
            planned_departure_at=current_time + timedelta(seconds=origin_input.dwell_seconds),
            dwell_seconds=origin_input.dwell_seconds,
            wait_seconds=0,
        )
        scheduled_stops.append(origin_scheduled)
        current_traveler_stop = origin_scheduled
        current_time = origin_scheduled.planned_departure_at or current_time
        total_dwell_s += origin_input.dwell_seconds

        idx = 1
        while idx < len(stops):
            target = stops[idx]
            is_pedestrian = is_driving_trip and (
                target.is_pedestrian_only or target.vehicle_restricted
            )

            if not is_pedestrian:
                # Standard driving destination
                # If traveler is currently on foot away from their car, they must return to the hub first
                if active_parking_hub is not None:
                    walk_back_dist, walk_back_dur, walk_back_geom = await self._route_leg(
                        from_loc=current_traveler_stop.location,
                        to_loc=active_parking_hub.location,
                        mode=TransportMode.WALKING,
                        restrictions=restrictions,
                    )
                    walk_back_arr = current_time + timedelta(seconds=walk_back_dur)
                    scheduled_legs.append(
                        RouteLegOutput(
                            leg_order=len(scheduled_legs),
                            from_stop_id=current_traveler_stop.id,
                            to_stop_id=active_parking_hub.id,
                            from_stop_name=current_traveler_stop.name,
                            to_stop_name=active_parking_hub.name,
                            transport_mode=TransportMode.WALKING,
                            departure_at=current_time,
                            arrival_at=walk_back_arr,
                            distance_m=walk_back_dist,
                            travel_seconds=walk_back_dur,
                            geometry=walk_back_geom,
                            traffic_basis=TrafficBasis.MODELED,
                            parking_hub_id=active_parking_hub.id,
                        )
                    )
                    total_walking_m += walk_back_dist
                    total_travel_s += walk_back_dur
                    current_time = walk_back_arr
                    current_traveler_stop = active_parking_hub
                    active_parking_hub = None  # Reunited with vehicle, ready to drive

                # Transit to target
                transit_dist, transit_dur, transit_geom = await self._route_leg(
                    from_loc=current_traveler_stop.location,
                    to_loc=target.location,
                    mode=request.transport_mode,
                    restrictions=restrictions,
                )
                arr_time = current_time + timedelta(seconds=transit_dur)

                # Dwell & window accounting
                wait_s = 0
                if target.window_start:
                    w_start = _ensure_utc(target.window_start)
                    if w_start and arr_time < w_start:
                        wait_s = int((w_start - arr_time).total_seconds())

                service_start = arr_time + timedelta(seconds=wait_s)
                dep_time = service_start + timedelta(seconds=target.dwell_seconds)

                if target.window_end:
                    w_end = _ensure_utc(target.window_end)
                    if w_end and dep_time > w_end:
                        if target.is_must_visit:
                            return OptimizedRoute(
                                owner_id=request.owner_id,
                                trip_id=request.trip_id,
                                execution_id=request.execution_id,
                                route_version=1,
                                status=RouteStatus.INFEASIBLE,
                                solver_status=SolverStatus.INFEASIBLE,
                                objective=request.objective,
                                departure_at=request.departure_at,
                                return_deadline=request.return_deadline,
                                actual_return_at=dep_time,
                                diagnostics=[
                                    f"SCHEDULE_INFEASIBLE: Service at mandatory stop '{target.name}' finishes at {dep_time.isoformat()}, exceeding closing time {w_end.isoformat()}"
                                ],
                                warnings=[f"Hard service window breached at mandatory stop '{target.name}'"],
                            )
                        else:
                            skipped_stop = ScheduledStopOutput(
                                id=target.id,
                                stop_id=target.id,
                                place_id=target.place_id,
                                stop_order=len(scheduled_stops),
                                name=target.name,
                                location=target.location,
                                stop_type=target.stop_type,
                                is_must_visit=False,
                                is_skipped=True,
                                skip_reason="TIME_WINDOW_OR_DROP_PENALTY",
                                window_start=target.window_start,
                                window_end=target.window_end,
                                dwell_seconds=target.dwell_seconds,
                            )
                            scheduled_stops.append(skipped_stop)
                            continue

                target_scheduled = ScheduledStopOutput(
                    id=target.id,
                    stop_id=target.id,
                    place_id=target.place_id,
                    stop_order=len(scheduled_stops),
                    name=target.name,
                    location=target.location,
                    stop_type=target.stop_type,
                    is_must_visit=target.is_must_visit,
                    window_start=target.window_start,
                    window_end=target.window_end,
                    planned_arrival_at=arr_time,
                    planned_departure_at=dep_time,
                    dwell_seconds=target.dwell_seconds,
                    wait_seconds=wait_s,
                )
                scheduled_stops.append(target_scheduled)

                scheduled_legs.append(
                    RouteLegOutput(
                        leg_order=len(scheduled_legs),
                        from_stop_id=current_traveler_stop.id,
                        to_stop_id=target.id,
                        from_stop_name=current_traveler_stop.name,
                        to_stop_name=target.name,
                        transport_mode=request.transport_mode,
                        departure_at=current_time,
                        arrival_at=arr_time,
                        distance_m=transit_dist,
                        travel_seconds=transit_dur,
                        geometry=transit_geom,
                        traffic_basis=TrafficBasis.MODELED,
                    )
                )

                if is_driving_trip:
                    total_driving_m += transit_dist
                else:
                    total_walking_m += transit_dist
                total_travel_s += transit_dur
                total_wait_s += wait_s
                total_dwell_s += target.dwell_seconds
                current_time = dep_time
                current_traveler_stop = target_scheduled
                idx += 1

            else:
                # Pedestrian destination cluster
                # Check if we can reuse the active parking hub (walk chaining!)
                parking_place, _ = hub_for_stop[target.id]
                parking_loc = Point(
                    latitude=parking_place.point.y
                    if hasattr(parking_place.point, "y")
                    else float(CoordinatePoint.from_geo(parking_place.point).lat),
                    longitude=parking_place.point.x
                    if hasattr(parking_place.point, "x")
                    else float(CoordinatePoint.from_geo(parking_place.point).lng),
                )

                need_new_hub = (
                    active_parking_hub is None
                    or haversine_distance_m(active_parking_hub.location, target.location)
                    > self._search_radius_meters
                )

                if need_new_hub:
                    if active_parking_hub is not None:
                        # Return to old parking hub before driving to new one
                        walk_back_dist, walk_back_dur, walk_back_geom = await self._route_leg(
                            from_loc=current_traveler_stop.location,
                            to_loc=active_parking_hub.location,
                            mode=TransportMode.WALKING,
                            restrictions=restrictions,
                        )
                        walk_back_arr = current_time + timedelta(seconds=walk_back_dur)

                        scheduled_legs.append(
                            RouteLegOutput(
                                leg_order=len(scheduled_legs),
                                from_stop_id=current_traveler_stop.id,
                                to_stop_id=active_parking_hub.id,
                                from_stop_name=current_traveler_stop.name,
                                to_stop_name=active_parking_hub.name,
                                transport_mode=TransportMode.WALKING,
                                departure_at=current_time,
                                arrival_at=walk_back_arr,
                                distance_m=walk_back_dist,
                                travel_seconds=walk_back_dur,
                                geometry=walk_back_geom,
                                traffic_basis=TrafficBasis.MODELED,
                                parking_hub_id=active_parking_hub.id,
                            )
                        )
                        total_walking_m += walk_back_dist
                        total_travel_s += walk_back_dur
                        current_time = walk_back_arr
                        current_traveler_stop = active_parking_hub

                    # Drive to new parking hub
                    hub_stop_id = uuid.uuid4()
                    drive_to_hub_dist, drive_to_hub_dur, drive_to_hub_geom = await self._route_leg(
                        from_loc=current_traveler_stop.location,
                        to_loc=parking_loc,
                        mode=request.transport_mode,
                        restrictions=restrictions,
                    )
                    hub_arr_time = current_time + timedelta(seconds=drive_to_hub_dur)
                    hub_dwell_s = 180  # 3 minutes parking dwell

                    hub_scheduled = ScheduledStopOutput(
                        id=hub_stop_id,
                        stop_id=hub_stop_id,
                        place_id=parking_place.id,
                        stop_order=len(scheduled_stops),
                        name=f"Parking Hub ({parking_place.primary_name})",
                        location=parking_loc,
                        stop_type=StopType.PARKING_HUB,
                        is_must_visit=True,
                        planned_arrival_at=hub_arr_time,
                        planned_departure_at=hub_arr_time + timedelta(seconds=hub_dwell_s),
                        dwell_seconds=hub_dwell_s,
                        wait_seconds=0,
                    )
                    scheduled_stops.append(hub_scheduled)

                    scheduled_legs.append(
                        RouteLegOutput(
                            leg_order=len(scheduled_legs),
                            from_stop_id=current_traveler_stop.id,
                            to_stop_id=hub_scheduled.id,
                            from_stop_name=current_traveler_stop.name,
                            to_stop_name=hub_scheduled.name,
                            transport_mode=request.transport_mode,
                            departure_at=current_time,
                            arrival_at=hub_arr_time,
                            distance_m=drive_to_hub_dist,
                            travel_seconds=drive_to_hub_dur,
                            geometry=drive_to_hub_geom,
                            traffic_basis=TrafficBasis.MODELED,
                        )
                    )
                    total_driving_m += drive_to_hub_dist
                    total_travel_s += drive_to_hub_dur
                    total_dwell_s += hub_dwell_s
                    current_time = hub_scheduled.planned_departure_at or hub_arr_time
                    current_traveler_stop = hub_scheduled
                    active_parking_hub = hub_scheduled

                # Walk from current position (hub or previous chained pedestrian stop) to target
                walk_dist, walk_dur, walk_geom = await self._route_leg(
                    from_loc=current_traveler_stop.location,
                    to_loc=target.location,
                    mode=TransportMode.WALKING,
                    restrictions=restrictions,
                )
                arr_time = current_time + timedelta(seconds=walk_dur)

                # Window check
                wait_s = 0
                if target.window_start:
                    w_start = _ensure_utc(target.window_start)
                    if w_start and arr_time < w_start:
                        wait_s = int((w_start - arr_time).total_seconds())

                service_start = arr_time + timedelta(seconds=wait_s)
                dep_time = service_start + timedelta(seconds=target.dwell_seconds)

                if target.window_end:
                    w_end = _ensure_utc(target.window_end)
                    if w_end and dep_time > w_end:
                        if target.is_must_visit:
                            return OptimizedRoute(
                                owner_id=request.owner_id,
                                trip_id=request.trip_id,
                                execution_id=request.execution_id,
                                route_version=1,
                                status=RouteStatus.INFEASIBLE,
                                solver_status=SolverStatus.INFEASIBLE,
                                objective=request.objective,
                                departure_at=request.departure_at,
                                return_deadline=request.return_deadline,
                                actual_return_at=dep_time,
                                diagnostics=[
                                    f"SCHEDULE_INFEASIBLE: Service at mandatory stop '{target.name}' finishes at {dep_time.isoformat()}, exceeding closing time {w_end.isoformat()}"
                                ],
                                warnings=[f"Hard service window breached at mandatory stop '{target.name}'"],
                            )
                        else:
                            skipped_stop = ScheduledStopOutput(
                                id=target.id,
                                stop_id=target.id,
                                place_id=target.place_id,
                                stop_order=len(scheduled_stops),
                                name=target.name,
                                location=target.location,
                                stop_type=target.stop_type,
                                is_must_visit=False,
                                is_skipped=True,
                                skip_reason="TIME_WINDOW_OR_DROP_PENALTY",
                                window_start=target.window_start,
                                window_end=target.window_end,
                                dwell_seconds=target.dwell_seconds,
                            )
                            scheduled_stops.append(skipped_stop)
                            continue

                target_scheduled = ScheduledStopOutput(
                    id=target.id,
                    stop_id=target.id,
                    place_id=target.place_id,
                    stop_order=len(scheduled_stops),
                    name=target.name,
                    location=target.location,
                    stop_type=target.stop_type,
                    is_must_visit=target.is_must_visit,
                    window_start=target.window_start,
                    window_end=target.window_end,
                    planned_arrival_at=arr_time,
                    planned_departure_at=dep_time,
                    dwell_seconds=target.dwell_seconds,
                    wait_seconds=wait_s,
                )
                scheduled_stops.append(target_scheduled)

                scheduled_legs.append(
                    RouteLegOutput(
                        leg_order=len(scheduled_legs),
                        from_stop_id=current_traveler_stop.id,
                        to_stop_id=target.id,
                        from_stop_name=current_traveler_stop.name,
                        to_stop_name=target.name,
                        transport_mode=TransportMode.WALKING,
                        departure_at=current_time,
                        arrival_at=arr_time,
                        distance_m=walk_dist,
                        travel_seconds=walk_dur,
                        geometry=walk_geom,
                        traffic_basis=TrafficBasis.MODELED,
                        parking_hub_id=active_parking_hub.id if active_parking_hub is not None else None,
                    )
                )

                total_walking_m += walk_dist
                total_travel_s += walk_dur
                total_wait_s += wait_s
                total_dwell_s += target.dwell_seconds
                current_time = dep_time
                current_traveler_stop = target_scheduled
                idx += 1

        # 3. Handle return to parking hub and/or return to origin for round trips
        if active_parking_hub is not None:
            # Walk back to parking hub to retrieve vehicle
            walk_back_dist, walk_back_dur, walk_back_geom = await self._route_leg(
                from_loc=current_traveler_stop.location,
                to_loc=active_parking_hub.location,
                mode=TransportMode.WALKING,
                restrictions=restrictions,
            )
            walk_back_arr = current_time + timedelta(seconds=walk_back_dur)

            scheduled_legs.append(
                RouteLegOutput(
                    leg_order=len(scheduled_legs),
                    from_stop_id=current_traveler_stop.id,
                    to_stop_id=active_parking_hub.id,
                    from_stop_name=current_traveler_stop.name,
                    to_stop_name=active_parking_hub.name,
                    transport_mode=TransportMode.WALKING,
                    departure_at=current_time,
                    arrival_at=walk_back_arr,
                    distance_m=walk_back_dist,
                    travel_seconds=walk_back_dur,
                    geometry=walk_back_geom,
                    traffic_basis=TrafficBasis.MODELED,
                    parking_hub_id=active_parking_hub.id,
                )
            )
            total_walking_m += walk_back_dist
            total_travel_s += walk_back_dur
            current_time = walk_back_arr
            current_traveler_stop = active_parking_hub
            active_parking_hub = None

        actual_return_at = current_time
        if request.is_round_trip and len(scheduled_stops) > 1:
            return_dist, return_dur, return_geom = await self._route_leg(
                from_loc=current_traveler_stop.location,
                to_loc=origin_scheduled.location,
                mode=request.transport_mode,
                restrictions=restrictions,
            )
            return_arr = current_time + timedelta(seconds=return_dur)

            scheduled_legs.append(
                RouteLegOutput(
                    leg_order=len(scheduled_legs),
                    from_stop_id=current_traveler_stop.id,
                    to_stop_id=origin_scheduled.id,
                    from_stop_name=current_traveler_stop.name,
                    to_stop_name=origin_scheduled.name,
                    transport_mode=request.transport_mode,
                    departure_at=current_time,
                    arrival_at=return_arr,
                    distance_m=return_dist,
                    travel_seconds=return_dur,
                    geometry=return_geom,
                    traffic_basis=TrafficBasis.MODELED,
                )
            )
            if request.transport_mode == TransportMode.WALKING:
                total_walking_m += return_dist
            else:
                total_driving_m += return_dist
            total_travel_s += return_dur
            actual_return_at = return_arr

        # Schedule consistency check against return_deadline
        if request.return_deadline is not None and actual_return_at is not None:
            ret_dl = _ensure_utc(request.return_deadline)
            if ret_dl and actual_return_at > ret_dl:
                return OptimizedRoute(
                    owner_id=request.owner_id,
                    trip_id=request.trip_id,
                    execution_id=request.execution_id,
                    route_version=1,
                    status=RouteStatus.INFEASIBLE,
                    solver_status=SolverStatus.INFEASIBLE,
                    objective=request.objective,
                    departure_at=request.departure_at,
                    return_deadline=request.return_deadline,
                    actual_return_at=actual_return_at,
                    diagnostics=[
                        f"SCHEDULE_INFEASIBLE: Route completes at {actual_return_at.isoformat()}, exceeding return deadline {ret_dl.isoformat()}"
                    ],
                    warnings=["Return deadline breached"],
                )

        total_distance_m = total_driving_m + total_walking_m
        total_elapsed_s = total_travel_s + total_dwell_s + total_wait_s

        # 4. Fuel calculation strictly on driving distance (zero fuel for walking legs)
        driving_dist_km = (Decimal(str(total_driving_m)) / Decimal("1000")).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )
        fuel_liters = (driving_dist_km / fuel_profile.mileage_km_per_liter).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        fuel_cost = (fuel_liters * fuel_profile.fuel_price_per_liter).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        fuel_basis = (
            f"fuel_{fuel_profile.mode.value} "
            f"({driving_dist_km}km @ {fuel_profile.mileage_km_per_liter}km/l; walking excluded)"
        )

        has_skipped = any(s.is_skipped for s in scheduled_stops)
        optimized_route = OptimizedRoute(
            owner_id=request.owner_id,
            trip_id=request.trip_id,
            execution_id=request.execution_id,
            route_version=1,
            status=RouteStatus.PARTIAL if has_skipped else RouteStatus.FEASIBLE,
            solver_status=SolverStatus.FEASIBLE,
            objective=request.objective,
            departure_at=request.departure_at,
            return_deadline=request.return_deadline,
            actual_return_at=actual_return_at,
            total_distance_m=total_distance_m,
            total_travel_seconds=total_travel_s,
            total_dwell_seconds=total_dwell_s,
            total_wait_seconds=total_wait_s,
            total_elapsed_seconds=total_elapsed_s,
            driving_distance_m=total_driving_m,
            walking_distance_m=total_walking_m,
            fuel_amount=fuel_cost,
            fuel_currency=fuel_profile.currency,
            fuel_basis=fuel_basis,
            stops=scheduled_stops,
            legs=scheduled_legs,
            dropped_stops=[s for s in scheduled_stops if s.is_skipped],
            warnings=[],
            assumptions=[
                "Multimodal itinerary with verified parking hub injection",
                "Fuel calculation strictly isolates driving distance",
            ],
            diagnostics=[],
        )

        # 5. Run vehicle continuity verification pass
        tracker = VehicleContinuityTracker()
        continuity_violations = tracker.validate_route(optimized_route)
        if continuity_violations:
            logger.error("Continuity violations detected: %s", continuity_violations)
            return OptimizedRoute(
                owner_id=request.owner_id,
                trip_id=request.trip_id,
                execution_id=request.execution_id,
                route_version=1,
                status=RouteStatus.INFEASIBLE,
                solver_status=SolverStatus.INFEASIBLE,
                objective=request.objective,
                departure_at=request.departure_at,
                return_deadline=request.return_deadline,
                diagnostics=continuity_violations,
                warnings=["Itinerary violated vehicle continuity constraints"],
            )

        # 6. Apply closure avoidance and spatial revalidation
        if restrictions:
            optimized_route = self._closure_service.apply_and_revalidate(
                optimized_route, restrictions
            )

        return optimized_route

