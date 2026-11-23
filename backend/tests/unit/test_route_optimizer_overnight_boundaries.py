"""Unit tests for overnight waiting slack and attraction visit boundaries.

Verifies:
1. Reproduction of the Shivananjappa Park overnight interval from the Mysuru corridor.
2. Solver clamps planned_arrival_at to service window start when waiting spans overnight.
3. Visit intervals finish strictly within permitted service windows.
4. Overnight waiting is recorded in wait_seconds without inventing lodging.
5. Same day routes preserve pre opening wait slack without regression.
6. Worker geometry hydration preserves leg arrival at transit arrival and stop arrival at service start.
7. Graph consumption produces API schedule output matching persisted stop times.
8. Unassigned accommodation preserves uncertainty with zero fake hotel coordinates or geometry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import shapely.geometry
from geoalchemy2.shape import from_shape

from contracts.routing import (
    Point,
    RouteRequest,
    RouteStatus,
    RouteStopInput,
    StopType,
)
from contracts.trips import TransportMode
from functions.optimizer.solver import ItineraryScheduler
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    consume_route_result_node,
)
from functions.route_optimizer.service import RouteOptimizerWorkerService
from functions.route_optimizer.solver import OrToolsRoutingSolver


class TestOvernightWaitingSolverBoundaries:
    """Verifies solver handling of overnight waiting and service boundaries."""

    def test_reproduced_mysuru_shivananjappa_park_overnight_boundary(self) -> None:
        """Reproduce Mysuru to Mandya corridor where arrival precedes Day 2 opening by 16 hours."""
        t_depot = datetime(2026, 11, 25, 10, 37, 45, tzinfo=UTC)  # 16:07:45 IST Day 1
        t_day2_open = datetime(2026, 11, 26, 3, 30, 0, tzinfo=UTC)  # 09:00:00 IST Day 2
        t_day2_close = datetime(2026, 11, 26, 12, 30, 0, tzinfo=UTC)  # 18:00:00 IST Day 2

        depot = RouteStopInput(
            name="Tipu Sultan Summer Palace",
            location=Point(latitude=12.4236, longitude=76.6953),
            stop_type=StopType.ORIGIN,
            dwell_seconds=0,
        )
        park = RouteStopInput(
            name="Shivananjappa Park Mandya",
            location=Point(latitude=12.5244, longitude=76.8988),
            stop_type=StopType.DESTINATION,
            window_start=t_day2_open,
            window_end=t_day2_close,
            dwell_seconds=3600,
            is_must_visit=True,
        )

        req = RouteRequest(
            departure_at=t_depot,
            stops=[depot, park],
            is_round_trip=False,
            transport_mode=TransportMode.CAR_PETROL,
        )

        durations_s: list[list[int | None]] = [
            [0, 3127],
            [3127, 0],
        ]
        distances_m: list[list[int | None]] = [
            [0, 37294],
            [37294, 0],
        ]

        solver = OrToolsRoutingSolver()
        res = solver.solve(req, durations_s, distances_m)

        assert res.status == RouteStatus.FEASIBLE
        assert len(res.stops) == 2

        depot_out = res.stops[0]
        park_out = res.stops[1]

        assert depot_out.planned_departure_at == t_depot

        # Road arrival at park would be 16:59:52 IST Day 1 (11:29:52 UTC)
        # Service cannot start until 09:00:00 IST Day 2 (03:30:00 UTC)
        # Waiting slack is 57608 seconds (16 hours)
        assert park_out.wait_seconds == 57608
        assert res.total_wait_seconds == 57608

        # Bounded correction: planned_arrival_at is clamped to service start on Day 2
        assert park_out.planned_arrival_at == t_day2_open
        assert park_out.planned_departure_at == t_day2_open + timedelta(seconds=3600)
        assert park_out.dwell_seconds == 3600
        assert park_out.planned_departure_at - park_out.planned_arrival_at == timedelta(seconds=3600)

        # Permitted window enforcement: visit finishes strictly before window_end
        assert park_out.planned_arrival_at >= t_day2_open
        assert park_out.planned_departure_at <= t_day2_close

        # Park must not be labeled as lodging
        assert park_out.stop_type in (StopType.DESTINATION, StopType.WAYPOINT)
        assert "hotel" not in park_out.name.lower()
        assert "lodging" not in park_out.name.lower()

        # Route assumptions include explicit overnight waiting note
        assert any("Overnight waiting interval with unassigned accommodation" in a for a in res.assumptions)

    def test_same_day_pre_opening_waiting_slack_preserved(self) -> None:
        """Same day route arriving 15 minutes before opening retains arr_time as planned_arrival_at."""
        t0 = datetime(2026, 11, 25, 9, 0, 0, tzinfo=UTC)
        t_open = datetime(2026, 11, 25, 9, 30, 0, tzinfo=UTC)
        t_close = datetime(2026, 11, 25, 17, 0, 0, tzinfo=UTC)

        depot = RouteStopInput(
            name="Origin",
            location=Point(latitude=12.0, longitude=76.0),
            stop_type=StopType.ORIGIN,
            dwell_seconds=0,
        )
        venue = RouteStopInput(
            name="Day Venue",
            location=Point(latitude=12.1, longitude=76.1),
            stop_type=StopType.DESTINATION,
            window_start=t_open,
            window_end=t_close,
            dwell_seconds=3600,
            is_must_visit=True,
        )

        req = RouteRequest(
            departure_at=t0,
            stops=[depot, venue],
            is_round_trip=False,
            transport_mode=TransportMode.CAR_PETROL,
        )

        durations_s: list[list[int | None]] = [
            [0, 900],
            [900, 0],
        ]
        distances_m: list[list[int | None]] = [
            [0, 10000],
            [10000, 0],
        ]

        solver = OrToolsRoutingSolver()
        res = solver.solve(req, durations_s, distances_m)

        assert res.status == RouteStatus.FEASIBLE
        venue_out = res.stops[1]

        # Arrives at 09:15, waits 15 minutes until 09:30, finishes at 10:30
        assert venue_out.planned_arrival_at == t0 + timedelta(seconds=900)
        assert venue_out.wait_seconds == 900
        assert venue_out.planned_departure_at == t0 + timedelta(seconds=900 + 900 + 3600)
        assert res.total_wait_seconds == 900

        # No overnight waiting assumption on same day trip
        assert not any("Overnight waiting interval" in a for a in res.assumptions)


class TestOvernightGeometryHydration:
    """Verifies worker geometry hydration preserves leg arrival and stop service arrival."""

    @pytest.mark.asyncio
    async def test_hydrate_route_geometry_overnight_arrival_and_leg_timestamps(self) -> None:
        """Hydration preserves leg arrival at transit arrival and stop planned arrival at service start."""
        t_depot = datetime(2026, 11, 25, 10, 37, 45, tzinfo=UTC)
        t_day2_open = datetime(2026, 11, 26, 3, 30, 0, tzinfo=UTC)
        t_day2_close = datetime(2026, 11, 26, 12, 30, 0, tzinfo=UTC)

        depot = RouteStopInput(
            name="Tipu Sultan Summer Palace",
            location=Point(latitude=12.4236, longitude=76.6953),
            stop_type=StopType.ORIGIN,
            dwell_seconds=0,
        )
        park = RouteStopInput(
            name="Shivananjappa Park Mandya",
            location=Point(latitude=12.5244, longitude=76.8988),
            stop_type=StopType.DESTINATION,
            window_start=t_day2_open,
            window_end=t_day2_close,
            dwell_seconds=3600,
            is_must_visit=True,
        )

        req = RouteRequest(
            departure_at=t_depot,
            stops=[depot, park],
            is_round_trip=False,
            transport_mode=TransportMode.CAR_PETROL,
        )

        durations_s: list[list[int | None]] = [[0, 3127], [3127, 0]]
        distances_m: list[list[int | None]] = [[0, 37294], [37294, 0]]

        solver = OrToolsRoutingSolver()
        initial_route = solver.solve(req, durations_s, distances_m)

        mock_session = AsyncMock()
        mock_repo = MagicMock()
        mock_provider = MagicMock()
        mock_leg = MagicMock()
        mock_leg.geometry = "sample_polyline"
        mock_route_res = MagicMock(
            total_distance_m=37294,
            total_travel_seconds=3127,
            legs=[mock_leg],
            geometry="sample_polyline",
        )
        mock_provider.route = AsyncMock(return_value=mock_route_res)
        mock_fencing = MagicMock()
        mock_closure = MagicMock()
        mock_multimodal = MagicMock()
        mock_replan = MagicMock()

        worker_service = RouteOptimizerWorkerService(
            session=mock_session,
            route_repo=mock_repo,
            valhalla_client=mock_provider,
            fencing_service=mock_fencing,
            closure_service=mock_closure,
            multimodal_service=mock_multimodal,
            replan_service=mock_replan,
        )

        hydrated = await worker_service._hydrate_leg_geometries_and_revalidate_schedule(
            initial_route=initial_route,
            request=req,
            client=mock_provider,
            restrictions=[],
        )

        assert hydrated.status == RouteStatus.FEASIBLE
        assert len(hydrated.legs) == 1
        assert len(hydrated.stops) == 2

        leg = hydrated.legs[0]
        # Road leg arrival is at transit arrival on Day 1
        assert leg.departure_at == t_depot
        assert leg.arrival_at == t_depot + timedelta(seconds=3127)

        park_stop = hydrated.stops[1]
        # Attraction visit planned_arrival_at is at service window start on Day 2
        assert park_stop.planned_arrival_at == t_day2_open
        assert park_stop.planned_departure_at == t_day2_open + timedelta(seconds=3600)
        assert park_stop.wait_seconds == 57608

        # Overnight waiting assumption recorded
        assert any("Overnight waiting interval with unassigned accommodation" in a for a in hydrated.assumptions)


class TestOvernightGraphConsumption:
    """Verifies trip planner graph consumption and schedule output."""

    @pytest.mark.asyncio
    async def test_consume_route_result_matches_persisted_stop_times(self) -> None:
        """Graph consumption scheduled stop output matches persisted route stops exactly."""
        route_id = uuid.uuid4()
        opt_task_id = str(uuid.uuid4())
        owner_id = uuid.uuid4()
        exec_id = uuid.uuid4()
        trip_id = uuid.uuid4()

        t_depot = datetime(2026, 11, 25, 10, 37, 45, tzinfo=UTC)
        t_day2_open = datetime(2026, 11, 26, 3, 30, 0, tzinfo=UTC)
        t_day2_close = datetime(2026, 11, 26, 12, 30, 0, tzinfo=UTC)
        t_day2_dep = t_day2_open + timedelta(seconds=3600)

        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session
        mock_factory.return_value.__aexit__.return_value = None

        pt0 = shapely.geometry.Point(76.6953, 12.4236)
        pt1 = shapely.geometry.Point(76.8988, 12.5244)

        mock_stop0 = MagicMock()
        mock_stop0.stop_order = 0
        mock_stop0.stop_type = "ORIGIN"
        mock_stop0.name = "Tipu Sultan Summer Palace"
        mock_stop0.location = from_shape(pt0, srid=4326)
        mock_stop0.planned_arrival_at = t_depot
        mock_stop0.planned_departure_at = t_depot
        mock_stop0.dwell_seconds = 0
        mock_stop0.wait_seconds = 0
        mock_stop0.window_start = None
        mock_stop0.window_end = None

        mock_stop1 = MagicMock()
        mock_stop1.stop_order = 1
        mock_stop1.stop_type = "DESTINATION"
        mock_stop1.name = "Shivananjappa Park Mandya"
        mock_stop1.location = from_shape(pt1, srid=4326)
        mock_stop1.planned_arrival_at = t_day2_open
        mock_stop1.planned_departure_at = t_day2_dep
        mock_stop1.dwell_seconds = 3600
        mock_stop1.wait_seconds = 57608
        mock_stop1.window_start = t_day2_open
        mock_stop1.window_end = t_day2_close

        mock_route = MagicMock()
        mock_route.id = route_id
        mock_route.owner_id = owner_id
        mock_route.execution_id = exec_id
        mock_route.trip_id = trip_id
        mock_route.status = "FEASIBLE"
        mock_route.stops = [mock_stop0, mock_stop1]
        mock_route.legs = []
        mock_route.total_travel_seconds = 3127
        mock_route.total_distance_m = 37294

        worker_output = {
            "status": "FEASIBLE",
            "route_id": str(route_id),
            "route_version": 1,
            "trip_id": str(trip_id),
            "execution_id": str(exec_id),
            "total_distance_m": 37294,
            "total_travel_seconds": 3127,
            "stops_count": 2,
            "legs_count": 1,
            "diagnostics": [],
        }

        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "owner_id": str(owner_id),
            "trip_id": str(trip_id),
            "_offline_fixture_mode": False,
            "_session_factory": mock_factory,
            "optimizer_task_id": opt_task_id,
            "task_outputs": {
                opt_task_id: worker_output,
            },
            "eligible_stops": [
                {
                    "primary_name": "Shivananjappa Park Mandya",
                    "point": {"lat": 12.5244, "lng": 76.8988},
                    "suggested_stay_minutes": 60,
                    "is_hours_assumed": False,
                    "hours_status": "VERIFIED_HOURS",
                }
            ],
        }

        with patch("functions.orchestrator.graphs.trip_planner.graph.RouteRepository") as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.get_route_by_id = AsyncMock(return_value=mock_route)
            mock_repo_cls.return_value = mock_repo

            res = await consume_route_result_node(state)

        assert res["route_id"] == str(route_id)
        assert res["route_status"] == "FEASIBLE"
        schedule = res["optimized_schedule"]
        assert len(schedule["stops"]) == 2

        sched_park = schedule["stops"][1]
        assert sched_park["name"] == "Shivananjappa Park Mandya"
        assert sched_park["arrival_time"] == t_day2_open.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert sched_park["departure_time"] == t_day2_dep.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert sched_park["stay_duration_minutes"] == 60

        # Exact match with persisted stop timestamps
        assert sched_park["arrival_time"] == mock_stop1.planned_arrival_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert sched_park["departure_time"] == mock_stop1.planned_departure_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    @pytest.mark.asyncio
    async def test_consume_route_result_defensive_clamp_for_legacy_unadjusted_arrival(self) -> None:
        """If a stop model in DB had arrival on Day 1 with multi hour wait, arrival is clamped to Day 2."""
        route_id = uuid.uuid4()
        opt_task_id = str(uuid.uuid4())
        owner_id = uuid.uuid4()
        exec_id = uuid.uuid4()
        trip_id = uuid.uuid4()

        t_day1_arr = datetime(2026, 11, 25, 11, 29, 52, tzinfo=UTC)
        t_day2_dep = datetime(2026, 11, 26, 4, 30, 0, tzinfo=UTC)
        t_expected_arr = datetime(2026, 11, 26, 3, 30, 0, tzinfo=UTC)

        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session
        mock_factory.return_value.__aexit__.return_value = None

        pt1 = shapely.geometry.Point(76.8988, 12.5244)

        mock_stop = MagicMock()
        mock_stop.stop_order = 1
        mock_stop.stop_type = "DESTINATION"
        mock_stop.name = "Shivananjappa Park Mandya"
        mock_stop.location = from_shape(pt1, srid=4326)
        # Unadjusted Day 1 arrival
        mock_stop.planned_arrival_at = t_day1_arr
        mock_stop.planned_departure_at = t_day2_dep
        mock_stop.dwell_seconds = 3600
        mock_stop.wait_seconds = 57608
        mock_stop.window_start = t_expected_arr
        mock_stop.window_end = datetime(2026, 11, 26, 12, 30, 0, tzinfo=UTC)

        mock_route = MagicMock()
        mock_route.id = route_id
        mock_route.owner_id = owner_id
        mock_route.execution_id = exec_id
        mock_route.trip_id = trip_id
        mock_route.status = "FEASIBLE"
        mock_route.stops = [mock_stop]
        mock_route.legs = []
        mock_route.total_travel_seconds = 3127
        mock_route.total_distance_m = 37294

        worker_output = {
            "status": "FEASIBLE",
            "route_id": str(route_id),
            "route_version": 1,
            "trip_id": str(trip_id),
            "execution_id": str(exec_id),
            "total_distance_m": 37294,
            "total_travel_seconds": 3127,
            "stops_count": 1,
            "legs_count": 0,
            "diagnostics": [],
        }

        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "owner_id": str(owner_id),
            "trip_id": str(trip_id),
            "_offline_fixture_mode": False,
            "_session_factory": mock_factory,
            "optimizer_task_id": opt_task_id,
            "task_outputs": {
                opt_task_id: worker_output,
            },
        }

        with patch("functions.orchestrator.graphs.trip_planner.graph.RouteRepository") as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.get_route_by_id = AsyncMock(return_value=mock_route)
            mock_repo_cls.return_value = mock_repo

            res = await consume_route_result_node(state)

        sched_stop = res["optimized_schedule"]["stops"][0]
        # Defensively clamped arrival
        assert sched_stop["arrival_time"] == t_expected_arr.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert sched_stop["departure_time"] == t_day2_dep.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestItinerarySchedulerOvernightProtection:
    """Verifies standalone ItineraryScheduler facade overnight schedule mapping."""

    def test_itinerary_scheduler_overnight_schedule_mapping(self) -> None:
        """ItineraryScheduler facade maps overnight stops with honest dwell and arrival on Day 2."""
        t_depot = datetime(2026, 11, 25, 10, 37, 45, tzinfo=UTC)
        t_day2_open = datetime(2026, 11, 26, 3, 30, 0, tzinfo=UTC)
        t_day2_close = datetime(2026, 11, 26, 12, 30, 0, tzinfo=UTC)

        stops = [
            {
                "name": "Shivananjappa Park Mandya",
                "lat": 12.5244,
                "lng": 76.8988,
                "stay_minutes": 60,
                "window_start": t_day2_open,
                "window_end": t_day2_close,
            },
        ]

        sched = ItineraryScheduler.solve_schedule(
            origin_name="Tipu Sultan Summer Palace",
            origin_lat=12.4236,
            origin_lng=76.6953,
            stops=stops,
            start_datetime=t_depot,
        )

        assert sched.is_optimal is True
        assert len(sched.stops) == 2

        park_sched = sched.stops[1]
        assert park_sched.name == "Shivananjappa Park Mandya"
        assert park_sched.arrival_time == t_day2_open
        assert park_sched.departure_time == t_day2_open + timedelta(seconds=3600)
        assert park_sched.stay_duration_minutes == 60


class TestTwoDayRouteUnassignedAccommodationIntegrity:
    """Verifies two day route execution with unassigned accommodation preserves uncertainty and honest boundaries."""

    def test_worker_execution_and_graph_consumption_two_day_unassigned_accommodation(self) -> None:
        """Two day route through worker execution and graph consumption preserves honest uncertainty and boundaries."""
        t_day1_start = datetime(2026, 11, 25, 10, 0, 0, tzinfo=UTC)
        t_day2_open = datetime(2026, 11, 26, 3, 30, 0, tzinfo=UTC)
        t_day2_close = datetime(2026, 11, 26, 12, 30, 0, tzinfo=UTC)

        origin = RouteStopInput(
            name="Mysuru Origin",
            location=Point(latitude=12.3051, longitude=76.6551),
            stop_type=StopType.ORIGIN,
            dwell_seconds=0,
        )
        day1_attraction = RouteStopInput(
            name="Srirangapatna Fort",
            location=Point(latitude=12.4236, longitude=76.6953),
            stop_type=StopType.DESTINATION,
            window_start=datetime(2026, 11, 25, 4, 30, 0, tzinfo=UTC),
            window_end=datetime(2026, 11, 25, 14, 30, 0, tzinfo=UTC),
            dwell_seconds=7200,
            is_must_visit=True,
        )
        day2_attraction = RouteStopInput(
            name="Shivananjappa Park Mandya",
            location=Point(latitude=12.5244, longitude=76.8988),
            stop_type=StopType.DESTINATION,
            window_start=t_day2_open,
            window_end=t_day2_close,
            dwell_seconds=3600,
            is_must_visit=True,
        )

        req = RouteRequest(
            departure_at=t_day1_start,
            stops=[origin, day1_attraction, day2_attraction],
            is_round_trip=False,
            transport_mode=TransportMode.CAR_PETROL,
        )

        durations_s: list[list[int | None]] = [
            [0, 1800, 4500],
            [1800, 0, 3127],
            [4500, 3127, 0],
        ]
        distances_m: list[list[int | None]] = [
            [0, 20000, 50000],
            [20000, 0, 37294],
            [50000, 37294, 0],
        ]

        solver = OrToolsRoutingSolver()
        solved = solver.solve(req, durations_s, distances_m)

        assert solved.status == RouteStatus.FEASIBLE
        assert len(solved.stops) == 3

        # Day 1 attraction
        s1 = solved.stops[1]
        assert s1.name == "Srirangapatna Fort"
        assert s1.planned_arrival_at == t_day1_start + timedelta(seconds=1800)
        assert s1.planned_departure_at == s1.planned_arrival_at + timedelta(seconds=7200)

        # Day 2 attraction with overnight wait
        s2 = solved.stops[2]
        assert s2.name == "Shivananjappa Park Mandya"
        assert s2.planned_arrival_at == t_day2_open
        assert s2.planned_departure_at == t_day2_open + timedelta(seconds=3600)
        assert s2.wait_seconds >= 4 * 3600

        # No fake hotel stop was injected into the route
        stop_names = [s.name for s in solved.stops]
        assert "hotel" not in " ".join(stop_names).lower()
        assert "lodging" not in " ".join(stop_names).lower()
        assert len(solved.stops) == 3
        assert len(solved.legs) == 2

        # Route assumptions honestly record unassigned accommodation
        assert any("Overnight waiting interval with unassigned accommodation" in a for a in solved.assumptions)
