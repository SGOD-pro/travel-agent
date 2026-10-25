"""Core constraint-based route solver using Google OR-Tools VRPTW.

Conforms strictly to specification 0005:
- Injected asymmetric distance and duration matrices.
- Forbidden unreachable transitions (never merely expensive).
- Explicit start/end semantics (round trip, fixed terminus, open route).
- Hard service windows and dwell/wait accounting.
- Strict closing enforcement (service must finish before closing).
- Mandatory stops and optional-stop dropping with explicit reasons.
- Concrete integer objective weights and drop penalties.
- Honest solver statuses (OPTIMAL requires proof, timeout returns TIME_LIMIT/UNAVAILABLE).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from contracts.routing import (
    OptimizedRoute,
    RouteLegOutput,
    RouteObjective,
    RouteRequest,
    RouteStatus,
    ScheduledStopOutput,
    SolverStatus,
    TrafficBasis,
    VehicleFuelProfile,
)
from contracts.trips import TransportMode

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensures a datetime is timezone-aware in UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class OrToolsRoutingSolver:
    """Google OR-Tools VRPTW routing solver with honest statuses and hard constraints."""

    # Concrete integer objective weights
    DEFAULT_WEIGHT_TIME: int = 1
    DEFAULT_WEIGHT_DISTANCE: int = 1
    DEFAULT_WEIGHT_FUEL: int = 1

    # Base drop penalty for optional stops
    # Calibrated to dominate realistic arc costs so optional stops are included whenever feasible
    DEFAULT_DROP_PENALTY: int = 10_000_000

    # Large horizon in seconds (7 days)
    HORIZON_SECONDS: int = 86400 * 7

    def solve(
        self,
        request: RouteRequest,
        durations_s: list[list[int | None]],
        distances_m: list[list[int | None]],
    ) -> OptimizedRoute:
        """Solves route request given injected duration and distance matrices."""
        stops = request.stops
        n = len(stops)

        # 1. Input validation
        if n == 0:
            return OptimizedRoute(
                departure_at=request.departure_at,
                status=RouteStatus.UNAVAILABLE,
                solver_status=SolverStatus.ERROR,
                diagnostics=["At least one stop is required to solve an itinerary"],
            )

        if len(durations_s) != n or len(distances_m) != n:
            return OptimizedRoute(
                departure_at=request.departure_at,
                status=RouteStatus.UNAVAILABLE,
                solver_status=SolverStatus.ERROR,
                diagnostics=[
                    f"Matrix dimension mismatch: expected {n}x{n}, got "
                    f"durations={len(durations_s)}, distances={len(distances_m)}"
                ],
            )

        for row_idx in range(n):
            if len(durations_s[row_idx]) != n or len(distances_m[row_idx]) != n:
                return OptimizedRoute(
                    departure_at=request.departure_at,
                    status=RouteStatus.UNAVAILABLE,
                    solver_status=SolverStatus.ERROR,
                    diagnostics=[f"Matrix row {row_idx} length mismatch: expected {n}"],
                )

        dep_at = _ensure_utc(request.departure_at)

        # Single stop edge case
        if n == 1:
            only_stop = stops[0]
            scheduled = ScheduledStopOutput(
                id=only_stop.id,
                stop_id=only_stop.id,
                place_id=only_stop.place_id,
                stop_order=0,
                name=only_stop.name,
                location=only_stop.location,
                stop_type=only_stop.stop_type,
                is_must_visit=only_stop.is_must_visit,
                dwell_seconds=only_stop.dwell_seconds,
                planned_arrival_at=dep_at,
                planned_departure_at=dep_at + timedelta(seconds=only_stop.dwell_seconds),
                wait_seconds=0,
            )
            return OptimizedRoute(
                departure_at=dep_at,
                actual_return_at=dep_at + timedelta(seconds=only_stop.dwell_seconds),
                status=RouteStatus.FEASIBLE,
                solver_status=SolverStatus.FEASIBLE,
                objective=request.objective,
                stops=[scheduled],
                legs=[],
                total_distance_m=0,
                total_travel_seconds=0,
                total_dwell_seconds=only_stop.dwell_seconds,
                total_wait_seconds=0,
                total_elapsed_seconds=only_stop.dwell_seconds,
            )

        # 2. Determine start and end node semantics
        start_node = 0
        if request.start_stop_id is not None:
            for idx, s in enumerate(stops):
                if s.id == request.start_stop_id:
                    start_node = idx
                    break

        is_open_route = False
        dummy_end_node: int | None = None

        if request.is_round_trip:
            # Round trip circuit: start and end at start_node
            num_nodes = n
            starts = [start_node]
            ends = [start_node]
        elif request.end_stop_id is not None:
            # Fixed terminus: start at start_node, end at designated end_stop
            end_node = 0
            for idx, s in enumerate(stops):
                if s.id == request.end_stop_id:
                    end_node = idx
                    break
            num_nodes = n
            starts = [start_node]
            ends = [end_node]
        else:
            # Open route: tour can end at any stop.
            # Add a dummy end node N with 0 cost/time from all stops.
            is_open_route = True
            dummy_end_node = n
            num_nodes = n + 1
            starts = [start_node]
            ends = [dummy_end_node]

        # 3. Create OR-Tools Routing Model
        manager = pywrapcp.RoutingIndexManager(num_nodes, 1, starts, ends)
        routing = pywrapcp.RoutingModel(manager)

        # 4. Objective Arc Cost Callback
        fuel_profile = request.fuel_profile or VehicleFuelProfile.default_for_mode(
            TransportMode.CAR_PETROL
        )

        def arc_cost_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)

            # Dummy end node transitions have zero cost
            if from_node == dummy_end_node or to_node == dummy_end_node:
                return 0

            # Guard unreachable pair
            dur = durations_s[from_node][to_node]
            dist = distances_m[from_node][to_node]
            if dur is None or dist is None:
                return 100_000_000

            if request.objective == RouteObjective.SHORTEST:
                return int(dist)
            elif request.objective == RouteObjective.BALANCED:
                # Balanced: w_time * duration_s + w_dist * distance_m + w_fuel * fuel_minor
                # fuel_minor: paise (1/100 INR)
                dist_km = Decimal(str(dist)) / Decimal("1000")
                fuel_liters = dist_km / fuel_profile.mileage_km_per_liter
                fuel_cost = fuel_liters * fuel_profile.fuel_price_per_liter
                fuel_minor = int((fuel_cost * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

                cost = (
                    self.DEFAULT_WEIGHT_TIME * dur
                    + self.DEFAULT_WEIGHT_DISTANCE * dist
                    + self.DEFAULT_WEIGHT_FUEL * fuel_minor
                )
                return int(cost)
            else:
                # Default FASTEST: travel duration in seconds
                return int(dur)

        cost_cb_idx = routing.RegisterTransitCallback(arc_cost_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(cost_cb_idx)

        # Helper to resolve routing node index for CumulVar and NextVar targets
        def get_node_index(node: int) -> int:
            idx = int(manager.NodeToIndex(node))
            if idx == -1 and node in ends:
                return int(routing.End(ends.index(node)))
            return idx

        # 5. Forbid unreachable arcs strictly
        # Unreachable arcs are forbidden transitions, never merely expensive.
        unreachable_pairs: list[tuple[int, int]] = []
        for i in range(n):
            for j in range(n):
                if i != j and (durations_s[i][j] is None or distances_m[i][j] is None):
                    unreachable_pairs.append((i, j))
                    if i in ends:
                        continue
                    from_idx = manager.NodeToIndex(i)
                    to_idx = get_node_index(j)
                    if from_idx != -1 and to_idx != -1:
                        # NextVar cannot take to_idx: strictly removes transition from domain
                        routing.NextVar(from_idx).RemoveValue(to_idx)

        # 6. Time Dimension & Window Accounting
        dwells = [s.dwell_seconds for s in stops]
        if is_open_route:
            dwells.append(0)  # Dummy end node has 0 dwell

        def time_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)

            if from_node == dummy_end_node or to_node == dummy_end_node:
                return 0

            dur = durations_s[from_node][to_node]
            if dur is None:
                return 86400  # Fallback large value, though arc is forbidden

            # Transit time includes travel duration plus dwell time at departure stop
            return int(dur + dwells[from_node])

        time_cb_idx = routing.RegisterTransitCallback(time_callback)
        time_dim_name = "Time"
        routing.AddDimension(
            time_cb_idx,
            self.HORIZON_SECONDS,  # Allowed waiting slack at stops
            self.HORIZON_SECONDS,  # Maximum horizon
            False,                 # Do not fix start to zero automatically
            time_dim_name,
        )
        time_dim = routing.GetDimensionOrDie(time_dim_name)

        # Set start node departure at t0 = 0
        time_dim.CumulVar(routing.Start(0)).SetRange(0, 0)

        # 7. Apply time windows and enforce closing times
        # Spec rule: service must finish before closing (arrival + dwell <= window_end)
        for i, s in enumerate(stops):
            node_idx = get_node_index(i)
            if node_idx == -1:
                continue

            rel_start = 0
            if s.window_start is not None:
                start_utc = _ensure_utc(s.window_start)
                rel_start = max(0, int((start_utc - dep_at).total_seconds()))

            rel_end = self.HORIZON_SECONDS
            if s.window_end is not None:
                end_utc = _ensure_utc(s.window_end)
                rel_end = int((end_utc - dep_at).total_seconds())

            # Enforce that service finishes before closing
            # Start of service is bounded by: rel_end - dwell_seconds
            latest_service_start = rel_end - s.dwell_seconds

            if latest_service_start < rel_start:
                # Stop cannot complete service before window closing
                if s.is_must_visit:
                    return OptimizedRoute(
                        departure_at=dep_at,
                        status=RouteStatus.INFEASIBLE,
                        solver_status=SolverStatus.INFEASIBLE,
                        diagnostics=[
                            f"Mandatory stop '{s.name}' cannot complete service before closing: "
                            f"dwell={s.dwell_seconds}s exceeds window [{rel_start}s, {rel_end}s]"
                        ],
                    )
                else:
                    # Optional stop with impossible window: force drop by setting tight unachievable range
                    latest_service_start = rel_start

            time_dim.CumulVar(node_idx).SetRange(rel_start, max(rel_start, latest_service_start))

        # 8. Apply return deadline if specified
        if request.return_deadline is not None:
            deadline_utc = _ensure_utc(request.return_deadline)
            rel_deadline = int((deadline_utc - dep_at).total_seconds())
            if rel_deadline < 0:
                return OptimizedRoute(
                    departure_at=dep_at,
                    return_deadline=request.return_deadline,
                    status=RouteStatus.INFEASIBLE,
                    solver_status=SolverStatus.INFEASIBLE,
                    diagnostics=["Return deadline is earlier than departure time"],
                )
            time_dim.CumulVar(routing.End(0)).SetRange(0, rel_deadline)

        # 9. Priority Partitioning & Drop Penalties for Optional Stops
        # Must-visit stops have no disjunction (hard constraint).
        # Optional stops have disjunction with drop penalty.
        for i, s in enumerate(stops):
            if not s.is_must_visit and i != start_node and i not in ends:
                idx = manager.NodeToIndex(i)
                if idx != -1:
                    routing.AddDisjunction([idx], self.DEFAULT_DROP_PENALTY)

        # 10. Search parameters & execution
        search_params = pywrapcp.DefaultRoutingSearchParameters()
        search_params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        search_params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_params.time_limit.seconds = request.time_limit_seconds

        solution = routing.SolveWithParameters(search_params)
        raw_status = routing.status()

        # 11. Honest Solver Status Interpretation
        if solution is None:
            # Timeout without finding a solution is TIME_LIMIT with UNAVAILABLE, not proven INFEASIBLE
            if raw_status == routing_enums_pb2.RoutingSearchStatus.ROUTING_FAIL_TIMEOUT:
                return OptimizedRoute(
                    departure_at=dep_at,
                    return_deadline=request.return_deadline,
                    status=RouteStatus.UNAVAILABLE,
                    solver_status=SolverStatus.TIME_LIMIT,
                    diagnostics=[
                        f"Solver timed out after {request.time_limit_seconds}s without finding a feasible solution"
                    ],
                    warnings=["TIME_LIMIT_REACHED"],
                )
            else:
                return OptimizedRoute(
                    departure_at=dep_at,
                    return_deadline=request.return_deadline,
                    status=RouteStatus.INFEASIBLE,
                    solver_status=SolverStatus.INFEASIBLE,
                    diagnostics=[
                        "Constraints cannot be satisfied: mandatory stops conflict or exceed deadlines"
                    ],
                )

        # Finding a solution alone means FEASIBLE; OPTIMAL requires proof
        solver_status = (
            SolverStatus.OPTIMAL
            if raw_status == routing_enums_pb2.RoutingSearchStatus.ROUTING_OPTIMAL
            else SolverStatus.FEASIBLE
        )

        warnings: list[str] = []
        if unreachable_pairs:
            warnings.append(
                f"Enforced {len(unreachable_pairs)} forbidden unreachable transitions in routing network"
            )

        # 12. Extract Visited Stops and Legs
        visited_nodes: list[int] = []
        curr = routing.Start(0)
        while not routing.IsEnd(curr):
            node = manager.IndexToNode(curr)
            if node != dummy_end_node:
                visited_nodes.append(node)
            curr = solution.Value(routing.NextVar(curr))

        # Check end node
        end_node_idx = manager.IndexToNode(curr)
        if end_node_idx != dummy_end_node and end_node_idx not in visited_nodes:
            visited_nodes.append(end_node_idx)

        visited_set = set(visited_nodes)

        # Identify dropped optional stops
        dropped_stops: list[ScheduledStopOutput] = []
        for i, s in enumerate(stops):
            if i not in visited_set:
                # Determine explicit skip reason
                skip_reason = "TIME_WINDOW_EXCEEDED"
                # Check if all incoming arcs were unreachable
                incoming_unreachable = all(
                    durations_s[j][i] is None for j in range(n) if j != i
                )
                if incoming_unreachable:
                    skip_reason = "UNREACHABLE_TRANSITION"

                dropped_stops.append(
                    ScheduledStopOutput(
                        id=s.id,
                        stop_id=s.id,
                        place_id=s.place_id,
                        stop_order=-1,
                        name=s.name,
                        location=s.location,
                        stop_type=s.stop_type,
                        is_must_visit=s.is_must_visit,
                        is_skipped=True,
                        skip_reason=skip_reason,
                        window_start=s.window_start,
                        window_end=s.window_end,
                        dwell_seconds=s.dwell_seconds,
                    )
                )

        route_status = RouteStatus.PARTIAL if dropped_stops else RouteStatus.FEASIBLE

        # Build scheduled stops and legs with exact dwell and wait accounting
        scheduled_stops: list[ScheduledStopOutput] = []
        scheduled_legs: list[RouteLegOutput] = []

        total_travel_seconds = 0
        total_dwell_seconds = 0
        total_wait_seconds = 0
        total_distance_m = 0

        current_time = dep_at

        for order, node in enumerate(visited_nodes):
            s = stops[node]
            dwell = s.dwell_seconds

            if order == 0:
                arr_time = current_time
                wait = 0
            else:
                prev_node = visited_nodes[order - 1]
                leg_dur = durations_s[prev_node][node] or 0
                leg_dist = distances_m[prev_node][node] or 0

                arr_time = current_time + timedelta(seconds=leg_dur)
                total_travel_seconds += leg_dur
                total_distance_m += leg_dist

                # Wait accounting if arriving before window start
                wait = 0
                if s.window_start is not None:
                    win_start_utc = _ensure_utc(s.window_start)
                    if arr_time < win_start_utc:
                        wait = int((win_start_utc - arr_time).total_seconds())

                total_wait_seconds += wait

                scheduled_legs.append(
                    RouteLegOutput(
                        leg_order=order - 1,
                        from_stop_id=stops[prev_node].id,
                        to_stop_id=s.id,
                        from_stop_name=stops[prev_node].name,
                        to_stop_name=s.name,
                        transport_mode=TransportMode.CAR_PETROL,
                        departure_at=current_time,
                        arrival_at=arr_time,
                        distance_m=leg_dist,
                        travel_seconds=leg_dur,
                        geometry={
                            "type": "LineString",
                            "coordinates": [
                                [stops[prev_node].location.longitude, stops[prev_node].location.latitude],
                                [s.location.longitude, s.location.latitude],
                            ],
                        },
                        traffic_basis=TrafficBasis.MODELED,
                    )
                )

            total_dwell_seconds += dwell
            service_start = arr_time + timedelta(seconds=wait)
            dep_time = service_start + timedelta(seconds=dwell)
            current_time = dep_time

            scheduled_stops.append(
                ScheduledStopOutput(
                    id=s.id,
                    stop_id=s.id,
                    place_id=s.place_id,
                    stop_order=order,
                    name=s.name,
                    location=s.location,
                    stop_type=s.stop_type,
                    is_must_visit=s.is_must_visit,
                    window_start=s.window_start,
                    window_end=s.window_end,
                    dwell_seconds=dwell,
                    planned_arrival_at=arr_time,
                    planned_departure_at=dep_time,
                    wait_seconds=wait,
                )
            )

        # Handle return leg for round trips
        actual_return_at: datetime | None = current_time
        if request.is_round_trip and len(visited_nodes) > 1:
            last_node = visited_nodes[-1]
            return_dur = durations_s[last_node][start_node] or 0
            return_dist = distances_m[last_node][start_node] or 0

            return_arr_time = current_time + timedelta(seconds=return_dur)
            total_travel_seconds += return_dur
            total_distance_m += return_dist

            scheduled_legs.append(
                RouteLegOutput(
                    leg_order=len(scheduled_legs),
                    from_stop_id=stops[last_node].id,
                    to_stop_id=stops[start_node].id,
                    from_stop_name=stops[last_node].name,
                    to_stop_name=stops[start_node].name,
                    transport_mode=TransportMode.CAR_PETROL,
                    departure_at=current_time,
                    arrival_at=return_arr_time,
                    distance_m=return_dist,
                    travel_seconds=return_dur,
                    geometry={
                        "type": "LineString",
                        "coordinates": [
                            [stops[last_node].location.longitude, stops[last_node].location.latitude],
                            [stops[start_node].location.longitude, stops[start_node].location.latitude],
                        ],
                    },
                    traffic_basis=TrafficBasis.MODELED,
                )
            )
            actual_return_at = return_arr_time

        total_elapsed_seconds = total_travel_seconds + total_dwell_seconds + total_wait_seconds

        # Compute fuel budget via canonical RoadBudgetCalculator math
        driving_dist_km = (Decimal(str(total_distance_m)) / Decimal("1000")).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )
        fuel_liters = (driving_dist_km / fuel_profile.mileage_km_per_liter).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        fuel_amount = (fuel_liters * fuel_profile.fuel_price_per_liter).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        fuel_basis = (
            f"fuel_{fuel_profile.mode.value} "
            f"({driving_dist_km}km @ {fuel_profile.mileage_km_per_liter}km/l)"
        )

        return OptimizedRoute(
            owner_id=request.owner_id,
            trip_id=request.trip_id,
            execution_id=request.execution_id,
            route_version=1,
            status=route_status,
            solver_status=solver_status,
            objective=request.objective,
            departure_at=dep_at,
            return_deadline=request.return_deadline,
            actual_return_at=actual_return_at,
            total_distance_m=total_distance_m,
            total_travel_seconds=total_travel_seconds,
            total_dwell_seconds=total_dwell_seconds,
            total_wait_seconds=total_wait_seconds,
            total_elapsed_seconds=total_elapsed_seconds,
            driving_distance_m=total_distance_m,
            walking_distance_m=0,
            fuel_amount=fuel_amount,
            fuel_currency=fuel_profile.currency,
            fuel_basis=fuel_basis,
            stops=scheduled_stops,
            legs=scheduled_legs,
            dropped_stops=dropped_stops,
            warnings=warnings,
            assumptions=[
                "Calculated on road transit network",
                "Fuel calculation assumes constant consumption rate",
            ],
            diagnostics=[],
        )
