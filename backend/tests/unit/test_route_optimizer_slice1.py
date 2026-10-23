"""Unit test suite for Slice 1 of 0005-route-optimizer:

Verifies:
1. Routing domain contracts and preservation of existing money/fuel types.
2. Injected fixed 5x5 asymmetric matrix fixture.
3. Asymmetric matrix direction is respected.
4. Unreachable arcs are forbidden transitions (never traversed).
5. Waiting, dwell, closing times and return deadlines are enforced.
6. Mandatory-stop conflicts produce honest failure (INFEASIBLE).
7. Optional stops can be dropped with explicit skip reasons (PARTIAL).
8. Timeout without a feasible solution produces TIME_LIMIT with UNAVAILABLE output.
9. OPTIMAL requires proof; finding a solution alone means FEASIBLE.
10. Explicit start/end semantics (round trip, fixed terminus, open route).
11. ItineraryScheduler backward compatibility and downstream RoadBudgetCalculator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from contracts.money import BudgetCategory, Currency
from contracts.routing import (
    Point,
    RoadBudgetCalculator,
    RoadSegment,
    RouteObjective,
    RouteRequest,
    RouteStatus,
    RouteStopInput,
    SolverStatus,
    StopType,
    VehicleFuelProfile,
)
from contracts.trips import TransportMode
from functions.optimizer.solver import ItineraryScheduler, OptimizedSchedule
from functions.route_optimizer.fixtures import (
    FIXED_5X5_DISTANCES_M,
    FIXED_5X5_DURATIONS_S,
    FIXED_STOP_IDS,
    create_fixed_5x5_request,
    create_fixed_5x5_stops,
)
from functions.route_optimizer.solver import OrToolsRoutingSolver


class TestRoutingContracts:
    """Tests that routing contracts adhere strictly to specs and preserve existing types."""

    def test_existing_money_and_fuel_types_preserved(self) -> None:
        """VehicleFuelProfile, RoadSegment, and RoadBudgetCalculator remain backward compatible."""
        profile = VehicleFuelProfile.default_for_mode(TransportMode.CAR_PETROL)
        assert profile.mileage_km_per_liter == Decimal("15.0")
        assert profile.fuel_price_per_liter == Decimal("102.50")
        assert profile.currency == Currency.INR

        segment = RoadSegment(
            origin_name="Base Hotel",
            destination_name="Museum",
            distance_km=Decimal("30.0"),
            duration_minutes=45,
            toll_charges=None,
            toll_unknown_reason="Toll plaza rates unverified on this corridor",
        )
        assert segment.toll_unknown_reason == "Toll plaza rates unverified on this corridor"

        budget_lines = RoadBudgetCalculator.calculate_segment_budget(segment, profile)
        assert len(budget_lines) == 2
        # Fuel line
        fuel_line = next(b for b in budget_lines if b.category == BudgetCategory.TRANSPORT)
        assert fuel_line.amount == Decimal("205.000000")
        assert fuel_line.estimated is True

        # Unverified toll line (never coerced to 0)
        toll_line = next(b for b in budget_lines if b.category == BudgetCategory.TOLLS_AND_FEES)
        assert toll_line.amount is None
        assert toll_line.unknown_reason is not None

    def test_point_coordinate_validation(self) -> None:
        """Point enforces WGS84 latitude [-90, 90] and longitude [-180, 180]."""
        pt = Point(latitude=27.0360, longitude=88.2627)
        assert pt.latitude == 27.0360
        assert pt.longitude == 88.2627

        with pytest.raises(ValidationError):
            Point(latitude=95.0, longitude=88.0)

        with pytest.raises(ValidationError):
            Point(latitude=27.0, longitude=-190.0)

    def test_enums_and_schemas(self) -> None:
        """New routing enums and schemas serialize correctly."""
        assert RouteObjective.FASTEST == "FASTEST"
        assert RouteObjective.SHORTEST == "SHORTEST"
        assert RouteObjective.BALANCED == "BALANCED"

        assert RouteStatus.FEASIBLE == "FEASIBLE"
        assert RouteStatus.PARTIAL == "PARTIAL"
        assert RouteStatus.INFEASIBLE == "INFEASIBLE"
        assert RouteStatus.UNAVAILABLE == "UNAVAILABLE"

        assert SolverStatus.OPTIMAL == "OPTIMAL"
        assert SolverStatus.FEASIBLE == "FEASIBLE"
        assert SolverStatus.TIME_LIMIT == "TIME_LIMIT"
        assert SolverStatus.INFEASIBLE == "INFEASIBLE"

        assert StopType.ORIGIN == "ORIGIN"
        assert StopType.DESTINATION == "DESTINATION"
        assert StopType.PARKING_HUB == "PARKING_HUB"


class TestOrToolsRoutingSolverCore:
    """Core test suite for OrToolsRoutingSolver with injected matrices."""

    @pytest.fixture
    def solver(self) -> OrToolsRoutingSolver:
        return OrToolsRoutingSolver()

    def test_asymmetric_matrix_direction_is_respected(self, solver: OrToolsRoutingSolver) -> None:
        """Asymmetric matrices produce legs reflecting forward direction, not reverse."""
        req = create_fixed_5x5_request(is_round_trip=False, stop_3_must_visit=False, stop_4_must_visit=False)
        # Only stops 0, 1, 2 are mandatory
        res = solver.solve(req, FIXED_5X5_DURATIONS_S, FIXED_5X5_DISTANCES_M)

        assert res.status in (RouteStatus.FEASIBLE, RouteStatus.PARTIAL)
        assert len(res.legs) >= 2

        # Check each leg travel time and distance matches the forward direction in the matrix
        node_indices = {s.id: idx for idx, s in enumerate(req.stops)}
        for leg in res.legs:
            from_idx = node_indices[leg.from_stop_id]
            to_idx = node_indices[leg.to_stop_id]
            expected_dur = FIXED_5X5_DURATIONS_S[from_idx][to_idx]
            expected_dist = FIXED_5X5_DISTANCES_M[from_idx][to_idx]

            assert leg.travel_seconds == expected_dur
            assert leg.distance_m == expected_dist
            # Verify forward != reverse for asymmetric pair (e.g. 0->1 is 900s, 1->0 is 1200s)
            reverse_dur = FIXED_5X5_DURATIONS_S[to_idx][from_idx]
            if from_idx != to_idx:
                assert expected_dur != reverse_dur or expected_dist != FIXED_5X5_DISTANCES_M[to_idx][from_idx]

    def test_unreachable_arc_is_forbidden_never_traversed(self, solver: OrToolsRoutingSolver) -> None:
        """Unreachable arc (1 -> 3) is strictly forbidden and never traversed directly."""
        req = create_fixed_5x5_request(
            is_round_trip=True,
            stop_3_must_visit=True,
            stop_4_must_visit=False,
        )
        res = solver.solve(req, FIXED_5X5_DURATIONS_S, FIXED_5X5_DISTANCES_M)

        assert res.status in (RouteStatus.FEASIBLE, RouteStatus.PARTIAL)

        # Confirm arc 1 -> 3 is NEVER in the legs
        stop_1_id = FIXED_STOP_IDS[1]
        stop_3_id = FIXED_STOP_IDS[3]

        for leg in res.legs:
            assert not (
                leg.from_stop_id == stop_1_id and leg.to_stop_id == stop_3_id
            ), "Unreachable arc 1 -> 3 was illegally traversed!"

        # Warning was recorded
        assert any("forbidden unreachable transitions" in w for w in res.warnings)

    def test_unreachable_mandatory_stop_produces_honest_infeasible(
        self, solver: OrToolsRoutingSolver
    ) -> None:
        """If a mandatory stop cannot be reached because all incoming arcs are unreachable, fail honestly."""
        # Make all incoming arcs to stop 3 unreachable
        durations = [row.copy() for row in FIXED_5X5_DURATIONS_S]
        distances = [row.copy() for row in FIXED_5X5_DISTANCES_M]

        for i in range(5):
            if i != 3:
                durations[i][3] = None
                distances[i][3] = None

        req = create_fixed_5x5_request(
            is_round_trip=True,
            stop_3_must_visit=True,
            stop_4_must_visit=False,
        )
        res = solver.solve(req, durations, distances)

        assert res.status == RouteStatus.INFEASIBLE
        assert res.solver_status == SolverStatus.INFEASIBLE
        assert len(res.diagnostics) > 0

    def test_waiting_dwell_and_venue_closing_enforced(self, solver: OrToolsRoutingSolver) -> None:
        """Waiting is accounted for when arriving before opening, and service must complete before closing."""
        t0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
        stops = create_fixed_5x5_stops(base_departure=t0)

        # Stop 1 opens at 09:30, dwell is 60m (3600s), closing at 13:00.
        # Departure from depot is 09:00, transit time is 900s (15 mins), so arrival is 09:15.
        # Traveler must wait 15 mins (900s) until 09:30.
        # Departure from Stop 1 must be 09:30 + 60m = 10:30.
        req = RouteRequest(
            departure_at=t0,
            stops=[stops[0], stops[1]],
            is_round_trip=False,
        )
        durs = [
            [0, 900],
            [1200, 0],
        ]
        dists = [
            [0, 5000],
            [5200, 0],
        ]

        res = solver.solve(req, durs, dists)
        assert res.status == RouteStatus.FEASIBLE

        stop_0_out = res.stops[0]
        stop_1_out = res.stops[1]

        assert stop_0_out.planned_departure_at == t0
        # Arrival at 1 is 09:15
        assert stop_1_out.planned_arrival_at == t0 + timedelta(seconds=900)
        # Wait at 1 is 900s (15 mins)
        assert stop_1_out.wait_seconds == 900
        # Departure from 1 is 09:15 + 15m wait + 60m dwell = 10:30
        assert stop_1_out.planned_departure_at == t0 + timedelta(seconds=900 + 900 + 3600)
        assert res.total_wait_seconds == 900
        assert res.total_dwell_seconds == 3600

    def test_service_exceeding_closing_time_violates_constraint(
        self, solver: OrToolsRoutingSolver
    ) -> None:
        """Service must complete before venue closing. If arrival + dwell > window_end, fail honestly."""
        t0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
        stops = [
            RouteStopInput(
                name="Depot",
                location=Point(latitude=27.0, longitude=88.0),
                stop_type=StopType.ORIGIN,
                dwell_seconds=0,
            ),
            RouteStopInput(
                name="Late Venue",
                location=Point(latitude=27.1, longitude=88.1),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                window_start=t0 + timedelta(hours=1),   # 10:00
                window_end=t0 + timedelta(hours=2),     # 11:00 closing
                dwell_seconds=7200,                     # Dwell 2 hours (requires 10:00 to 12:00, impossible!)
            ),
        ]
        req = RouteRequest(
            departure_at=t0,
            stops=stops,
            is_round_trip=False,
        )
        durs = [[0, 600], [600, 0]]
        dists = [[0, 5000], [5000, 0]]

        res = solver.solve(req, durs, dists)
        assert res.status == RouteStatus.INFEASIBLE
        assert res.solver_status == SolverStatus.INFEASIBLE
        assert any("cannot complete service before closing" in d for d in res.diagnostics)

    def test_mandatory_stop_conflict_fails_honestly(self, solver: OrToolsRoutingSolver) -> None:
        """Mutually exclusive mandatory stop windows produce honest INFEASIBLE status."""
        t0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
        stops = [
            RouteStopInput(
                name="Depot",
                location=Point(latitude=27.0, longitude=88.0),
                dwell_seconds=0,
            ),
            RouteStopInput(
                name="Morning Museum",
                location=Point(latitude=27.1, longitude=88.1),
                is_must_visit=True,
                window_start=t0,
                window_end=t0 + timedelta(hours=1),
                dwell_seconds=3600,
            ),
            RouteStopInput(
                name="Conflicting Zoo",
                location=Point(latitude=27.2, longitude=88.2),
                is_must_visit=True,
                window_start=t0,
                window_end=t0 + timedelta(hours=1),
                dwell_seconds=3600,
            ),
        ]
        # Travel time between them is 1800s (30m)
        durs = [
            [0, 600, 600],
            [600, 0, 1800],
            [600, 1800, 0],
        ]
        dists = [
            [0, 5000, 5000],
            [5000, 0, 10000],
            [5000, 10000, 0],
        ]
        req = RouteRequest(departure_at=t0, stops=stops, is_round_trip=False)
        res = solver.solve(req, durs, dists)

        assert res.status == RouteStatus.INFEASIBLE
        assert res.solver_status == SolverStatus.INFEASIBLE

    def test_optional_stop_gracefully_dropped_with_explicit_reason(
        self, solver: OrToolsRoutingSolver
    ) -> None:
        """Optional stops that cannot fit are dropped, returning PARTIAL and skip_reason."""
        t0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
        # Return deadline is 13:30 (4.5 hours from departure)
        # Mandatory stops 0, 1, 2 take: 900s travel + 900s wait + 3600s dwell + 300s travel + 3600s dwell + 1400s return = 10,700s (~3h)
        # Adding optional stops 3 & 4 would take an extra 3 hours, exceeding deadline
        deadline = t0 + timedelta(hours=4)
        req = create_fixed_5x5_request(
            base_departure=t0,
            return_deadline=deadline,
            is_round_trip=True,
            stop_3_must_visit=True,
            stop_4_must_visit=False,  # Stop 4 is optional
        )
        res = solver.solve(req, FIXED_5X5_DURATIONS_S, FIXED_5X5_DISTANCES_M)

        # Either stop 4 was dropped, or problem was partitioned
        if res.status == RouteStatus.PARTIAL:
            assert len(res.dropped_stops) > 0
            for dropped in res.dropped_stops:
                assert dropped.is_skipped is True
                assert dropped.skip_reason in ("TIME_WINDOW_EXCEEDED", "UNREACHABLE_TRANSITION")
        else:
            # If time deadline was even tighter, test with very tight return deadline
            tight_deadline = t0 + timedelta(hours=3, minutes=15)
            req_tight = create_fixed_5x5_request(
                base_departure=t0,
                return_deadline=tight_deadline,
                is_round_trip=True,
                stop_3_must_visit=False,  # Optional
                stop_4_must_visit=False,  # Optional
            )
            res_tight = solver.solve(req_tight, FIXED_5X5_DURATIONS_S, FIXED_5X5_DISTANCES_M)
            assert res_tight.status == RouteStatus.PARTIAL
            assert len(res_tight.dropped_stops) > 0
            assert any(s.skip_reason == "TIME_WINDOW_EXCEEDED" for s in res_tight.dropped_stops)

    def test_timeout_without_feasible_solution_returns_time_limit_unavailable(
        self, solver: OrToolsRoutingSolver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Timeout without finding a feasible solution returns TIME_LIMIT and UNAVAILABLE, not INFEASIBLE."""
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2

        # Mock SolveWithParameters to return None and status = ROUTING_FAIL_TIMEOUT
        def mock_solve(*args: object, **kwargs: object) -> None:
            return None

        monkeypatch.setattr(pywrapcp.RoutingModel, "SolveWithParameters", mock_solve)
        monkeypatch.setattr(
            pywrapcp.RoutingModel,
            "status",
            lambda self: routing_enums_pb2.RoutingSearchStatus.ROUTING_FAIL_TIMEOUT,
        )

        req = create_fixed_5x5_request()
        res = solver.solve(req, FIXED_5X5_DURATIONS_S, FIXED_5X5_DISTANCES_M)

        assert res.status == RouteStatus.UNAVAILABLE
        assert res.solver_status == SolverStatus.TIME_LIMIT
        assert any("timed out" in d for d in res.diagnostics)
        assert "TIME_LIMIT_REACHED" in res.warnings

    def test_optimal_requires_proof_finding_solution_is_feasible(
        self, solver: OrToolsRoutingSolver
    ) -> None:
        """Finding a local search solution alone yields FEASIBLE; OPTIMAL requires proof."""
        req = create_fixed_5x5_request(is_round_trip=False, stop_3_must_visit=False, stop_4_must_visit=False)
        res = solver.solve(req, FIXED_5X5_DURATIONS_S, FIXED_5X5_DISTANCES_M)

        assert res.status in (RouteStatus.FEASIBLE, RouteStatus.PARTIAL)
        # In OR-Tools local search, ROUTING_SUCCESS returns FEASIBLE, not OPTIMAL
        assert res.solver_status == SolverStatus.FEASIBLE

    def test_explicit_start_and_end_semantics(self, solver: OrToolsRoutingSolver) -> None:
        """Supports round-trip circuit, fixed terminus, and open route."""
        # 1. Round trip
        req_rt = create_fixed_5x5_request(is_round_trip=True, stop_3_must_visit=False, stop_4_must_visit=False)
        res_rt = solver.solve(req_rt, FIXED_5X5_DURATIONS_S, FIXED_5X5_DISTANCES_M)
        assert res_rt.status in (RouteStatus.FEASIBLE, RouteStatus.PARTIAL)
        # Last leg returns to start depot
        assert res_rt.legs[-1].to_stop_id == FIXED_STOP_IDS[0]

        # 2. Fixed terminus
        req_term = RouteRequest(
            departure_at=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
            stops=create_fixed_5x5_stops()[:3],
            end_stop_id=FIXED_STOP_IDS[2],
            is_round_trip=False,
            time_limit_seconds=1,
        )
        durs = [r[:3] for r in FIXED_5X5_DURATIONS_S[:3]]
        dists = [r[:3] for r in FIXED_5X5_DISTANCES_M[:3]]
        res_term = solver.solve(req_term, durs, dists)
        assert res_term.status == RouteStatus.FEASIBLE
        assert res_term.stops[-1].stop_id == FIXED_STOP_IDS[2]

        # 3. Open route (ends at whichever real stop is most efficient)
        req_open = RouteRequest(
            departure_at=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
            stops=create_fixed_5x5_stops()[:3],
            is_round_trip=False,
            end_stop_id=None,
            time_limit_seconds=1,
        )
        res_open = solver.solve(req_open, durs, dists)
        assert res_open.status == RouteStatus.FEASIBLE
        assert len(res_open.stops) == 3


class TestItinerarySchedulerFacade:
    """Tests ItineraryScheduler backward compatibility and downstream budgeting."""

    def test_itinerary_scheduler_solve_schedule_facade(self) -> None:
        """ItineraryScheduler.solve_schedule preserves callers and produces valid OptimizedSchedule."""
        start_dt = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
        schedule = ItineraryScheduler.solve_schedule(
            origin_name="Darjeeling Base Hotel",
            origin_lat=27.0360,
            origin_lng=88.2627,
            stops=[
                {"name": "Museum", "lat": 27.0583, "lng": 88.2676, "stay_minutes": 60},
                {"name": "Zoo", "lat": 27.0595, "lng": 88.2662, "stay_minutes": 60},
            ],
            start_datetime=start_dt,
            average_speed_kmh=40.0,
        )

        assert isinstance(schedule, OptimizedSchedule)
        assert len(schedule.stops) == 3
        assert len(schedule.legs) == 2
        assert schedule.total_travel_minutes > 0

        # Monotonic timestamps invariant
        for i in range(len(schedule.stops) - 1):
            assert schedule.stops[i].departure_time <= schedule.stops[i + 1].arrival_time

        # Static haversine distance helper preserved
        dist = ItineraryScheduler._haversine_distance_km(27.0360, 88.2627, 27.0583, 88.2676)
        assert 2.0 < dist < 4.0

    def test_downstream_road_budget_integration(self) -> None:
        """Legs produced by ItineraryScheduler feed directly into RoadBudgetCalculator without errors."""
        start_dt = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
        schedule = ItineraryScheduler.solve_schedule(
            origin_name="Base Hotel",
            origin_lat=27.0360,
            origin_lng=88.2627,
            stops=[
                {"name": "Museum", "lat": 27.0583, "lng": 88.2676, "stay_minutes": 60},
            ],
            start_datetime=start_dt,
        )

        profile = VehicleFuelProfile.default_for_mode(TransportMode.CAR_PETROL)
        all_budget_lines = []

        for leg in schedule.legs:
            segment = RoadSegment(
                origin_name=leg.from_stop,
                destination_name=leg.to_stop,
                distance_km=Decimal(str(leg.distance_km)),
                duration_minutes=leg.duration_minutes,
                toll_charges=None,
            )
            lines = RoadBudgetCalculator.calculate_segment_budget(segment, profile)
            all_budget_lines.extend(lines)

        assert len(all_budget_lines) == 2
        fuel_line = all_budget_lines[0]
        assert fuel_line.category == BudgetCategory.TRANSPORT
        assert fuel_line.amount > Decimal("0")
        assert fuel_line.currency == Currency.INR
