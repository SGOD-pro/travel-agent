"""Unit tests for trip planner route optimizer task dispatch, interrupt boundary, and result consumption.

Conforms to specification 0007 AC-3, AC-4, AC-5, AC-9, and Slice 4 requirements:
1. Maps verified eligible stops, assumed or verified opening windows, trip owner, execution and trip IDs
   into the existing RouteRequest contract. Unverified candidate proposals and brief destinations are excluded.
2. Formulates pending task specifications for worker OPTIMIZER with action solve and deterministic idempotency key.
3. Preserves replay safety: await_route_results_node clears pending_tasks on graph resumption.
4. Preserves 0005 fail closed routing: Valhalla outages and provider failures produce ROUTING_UNAVAILABLE
   with honest diagnostic notice and no guessed road routes.
5. Infeasible route outcomes preserve route_id and dropped stops diagnostics without failing parent job.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from contracts.routing import (
    RouteOptimizerWorkerOutput,
    RouteRequest,
    RouteStatus,
    StopType,
)
from contracts.trips import TransportMode
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    await_route_results_node,
    consume_route_result_node,
    dispatch_route_optimizer_node,
)


class TestTripPlannerRouteOptimizerDispatch:
    """Tests for formulating RouteRequest payloads and emitting task specifications."""

    def test_dispatch_maps_verified_stops_and_opening_windows(self) -> None:
        exec_id = uuid.uuid4()
        owner_id = uuid.uuid4()
        trip_id = uuid.uuid4()

        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "owner_id": str(owner_id),
            "trip_id": str(trip_id),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 2,
                        "confidence": 1.0,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-03",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "20000.00", "currency": "INR"},
                "preferred_modes": ["car_petrol"],
            },
            "eligible_stops": [
                {
                    "primary_name": "Digha",
                    "place_id": str(uuid.uuid4()),
                    "point": {"lat": 21.6266, "lng": 87.5074},
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "is_hours_assumed": False,
                    "window_start": "10:00",
                    "window_end": "17:00",
                },
                {
                    "primary_name": "Marine Research Aquarium",
                    "place_id": str(uuid.uuid4()),
                    "point": {"lat": 21.6300, "lng": 87.5100},
                    "suggested_stay_minutes": 90,
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                    "is_hours_assumed": True,
                    "window_start": None,
                    "window_end": None,
                },
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)

        assert outcome["current_step"] == "route_optimizer_dispatched"
        assert outcome["status"] == "RUNNING"
        assert len(outcome["pending_tasks"]) == 1

        task_spec = outcome["pending_tasks"][0]
        assert task_spec["worker"] == "OPTIMIZER"
        assert task_spec["action"] == "solve"
        assert task_spec["idempotency_key"] == f"{exec_id}:optimizer:solve"
        assert task_spec["deadline_seconds"] == 3600
        assert task_spec["max_attempts"] == 3

        payload = task_spec["payload"]
        route_req = RouteRequest.model_validate(payload)

        assert route_req.owner_id == owner_id
        assert route_req.execution_id == exec_id
        assert route_req.trip_id == trip_id
        assert route_req.transport_mode == TransportMode.CAR_PETROL

        stops = route_req.stops
        assert len(stops) == 3

        # Origin stop
        origin = stops[0]
        assert origin.name == "Kolkata"
        assert origin.stop_type == StopType.ORIGIN
        assert origin.location.latitude == pytest.approx(22.5726)
        assert origin.location.longitude == pytest.approx(88.3639)
        assert origin.dwell_seconds == 0

        # Verified destination with exact verified hours in Asia/Kolkata (IST = UTC+05:30)
        dest_stop = stops[1]
        assert dest_stop.name == "Digha"
        assert dest_stop.stop_type == StopType.DESTINATION
        assert dest_stop.dwell_seconds == 2 * 180 * 60
        assert dest_stop.window_start == datetime(2026, 11, 1, 4, 30, tzinfo=UTC)
        assert dest_stop.window_end == datetime(2026, 11, 1, 11, 30, tzinfo=UTC)

        # Verified POI with assumed daytime window (09:00 to 18:00 IST)
        poi_stop = stops[2]
        assert poi_stop.name == "Marine Research Aquarium"
        assert poi_stop.stop_type == StopType.DESTINATION
        assert poi_stop.dwell_seconds == 90 * 60
        assert poi_stop.window_start == datetime(2026, 11, 1, 3, 30, tzinfo=UTC)
        assert poi_stop.window_end == datetime(2026, 11, 1, 12, 30, tzinfo=UTC)

        assert outcome["optimizer_task_id"] == task_spec["task_id"]
        assert outcome["timezone"] == "Asia/Kolkata"

    def test_dispatch_excludes_unverified_proposals_and_brief_destinations(self) -> None:
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Unverified Seaside Village",
                        "latitude": 21.6500,
                        "longitude": 87.5500,
                        "stay_days": 1,
                        "confidence": 0.5,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-02",
                "travelers": 1,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "discovered_places": [
                {
                    "name": "Unverified Seaside Village",
                    "lat": 21.6500,
                    "lng": 87.5500,
                },
                {
                    "name": "Unverified Beach Shack Proposal",
                    "lat": 21.6600,
                    "lng": 87.5600,
                },
            ],
            "eligible_stops": [
                {
                    "primary_name": "Verified Historical Lighthouse",
                    "point": {"lat": 21.6400, "lng": 87.5300},
                    "suggested_stay_minutes": 45,
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                    "is_hours_assumed": True,
                }
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        task_spec = outcome["pending_tasks"][0]
        route_req = RouteRequest.model_validate(task_spec["payload"])

        stop_names = [s.name for s in route_req.stops]
        assert "Kolkata" in stop_names
        assert "Verified Historical Lighthouse" in stop_names

        # Invariant AC-3: Unverified brief destination and proposals must NOT enter the route stops
        assert "Unverified Seaside Village" not in stop_names
        assert "Unverified Beach Shack Proposal" not in stop_names

        # Both unverified places are preserved in editorial recommendations
        recs = outcome["editorial_recommendations"]
        rec_names = [r.get("primary_name") for r in recs]
        assert "Unverified Seaside Village" in rec_names

    def test_dispatch_excludes_contradicted_places(self) -> None:
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Contradicted Beach Spot",
                        "latitude": 21.6100,
                        "longitude": 87.5100,
                        "stay_days": 1,
                        "confidence": 0.8,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-02",
                "travelers": 1,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [],
            "excluded_candidates": [
                {
                    "primary_name": "Contradicted Beach Spot",
                    "diagnostics": {"rejection_reason": "Place permanently closed by evidence"},
                }
            ],
            "editorial_recommendations": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        task_spec = outcome["pending_tasks"][0]
        route_req = RouteRequest.model_validate(task_spec["payload"])

        stop_names = [s.name for s in route_req.stops]
        assert "Contradicted Beach Spot" not in stop_names
        assert len(stop_names) == 1
        assert stop_names[0] == "Kolkata"

    def test_dispatch_safe_path_when_gate_output_missing(self) -> None:
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 1,
                        "confidence": 0.9,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-02",
                "travelers": 1,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "discovered_places": [
                {"name": "Digha", "lat": 21.6266, "lng": 87.5074},
                {"name": "Unverified Discovered Point", "lat": 21.6400, "lng": 87.5200},
            ],
            # Eligibility gate output is completely missing (None)
            "eligible_stops": cast(Any, None),
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        task_spec = outcome["pending_tasks"][0]
        route_req = RouteRequest.model_validate(task_spec["payload"])

        stop_names = [s.name for s in route_req.stops]
        # Only origin is scheduled; unverified brief destination and proposals excluded
        assert stop_names == ["Kolkata"]
        assert "Unverified Discovered Point" not in stop_names

        # Unverified places relegated to editorial recommendations with diagnostic notice
        recs = outcome["editorial_recommendations"]
        disc_rec = next(
            (r for r in recs if r.get("primary_name") == "Unverified Discovered Point"), None
        )
        assert disc_rec is not None
        assert "Eligibility gate output missing" in disc_rec["diagnostics"]["rejection_reason"]

    def test_dispatch_custom_timezone_resolution(self) -> None:
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "London",
                "origin_lat": 51.5074,
                "origin_lng": -0.1278,
                "destinations": [
                    {
                        "name": "Big Ben",
                        "latitude": 51.5007,
                        "longitude": -0.1246,
                        "stay_days": 1,
                        "confidence": 1.0,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-02",
                "travelers": 1,
                "rooms": 1,
                "timezone": "UTC",
                "budget": {"amount": "1000.00", "currency": "GBP"},
            },
            "eligible_stops": [
                {
                    "primary_name": "Big Ben",
                    "point": {"lat": 51.5007, "lng": -0.1246},
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "is_hours_assumed": False,
                    "window_start": "10:00",
                    "window_end": "16:00",
                }
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        assert outcome["timezone"] == "UTC"
        task_spec = outcome["pending_tasks"][0]
        route_req = RouteRequest.model_validate(task_spec["payload"])
        assert route_req.departure_at == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        dest = route_req.stops[1]
        assert dest.window_start == datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
        assert dest.window_end == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)

    def test_dispatch_date_boundary_midnight_crossing(self) -> None:
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Late Night Lounge",
                        "latitude": 22.5500,
                        "longitude": 88.3500,
                        "stay_days": 1,
                        "confidence": 1.0,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-02",
                "travelers": 2,
                "rooms": 1,
                "timezone": "Asia/Kolkata",
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {
                    "primary_name": "Late Night Lounge",
                    "point": {"lat": 22.5500, "lng": 88.3500},
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "is_hours_assumed": False,
                    "window_start": "20:00",
                    "window_end": "02:00",
                }
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        task_spec = outcome["pending_tasks"][0]
        route_req = RouteRequest.model_validate(task_spec["payload"])
        dest = route_req.stops[1]
        # In IST (UTC+05:30): 20:00 on 2026-11-01 is 14:30 UTC
        assert dest.window_start == datetime(2026, 11, 1, 14, 30, tzinfo=UTC)
        # Closes at 02:00 next day (2026-11-02), which in UTC is 2026-11-01 20:30 UTC
        assert dest.window_end == datetime(2026, 11, 1, 20, 30, tzinfo=UTC)
        assert dest.window_end > dest.window_start

    def test_dispatch_multi_day_offsets(self) -> None:
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Day 1 Heritage Park",
                        "latitude": 22.5600,
                        "longitude": 88.3400,
                        "stay_days": 1,
                        "confidence": 1.0,
                        "day_offset": 0,
                    },
                    {
                        "name": "Day 2 Coastal Sanctuary",
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 1,
                        "confidence": 1.0,
                        "day_offset": 1,
                    },
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-03",
                "travelers": 2,
                "rooms": 1,
                "timezone": "Asia/Kolkata",
                "budget": {"amount": "20000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {
                    "primary_name": "Day 1 Heritage Park",
                    "point": {"lat": 22.5600, "lng": 88.3400},
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "is_hours_assumed": False,
                    "window_start": "10:00",
                    "window_end": "16:00",
                },
                {
                    "primary_name": "Day 2 Coastal Sanctuary",
                    "point": {"lat": 21.6266, "lng": 87.5074},
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "is_hours_assumed": False,
                    "window_start": "10:00",
                    "window_end": "16:00",
                },
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        task_spec = outcome["pending_tasks"][0]
        route_req = RouteRequest.model_validate(task_spec["payload"])

        day1_stop = route_req.stops[1]
        day2_stop = route_req.stops[2]
        # Day 1 is on 2026-11-01: 10:00 IST -> 04:30 UTC
        assert day1_stop.window_start == datetime(2026, 11, 1, 4, 30, tzinfo=UTC)
        # Day 2 is on 2026-11-02: 10:00 IST -> 04:30 UTC next day
        assert day2_stop.window_start == datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
        assert day2_stop.window_start > day1_stop.window_start


class TestTripPlannerAwaitRouteResults:
    """Tests for the interrupt boundary node."""

    def test_await_route_results_clears_pending_tasks_for_replay_safety(self) -> None:
        state: TripPlanningState = {
            "execution_id": str(uuid.uuid4()),
            "pending_tasks": [{"worker": "OPTIMIZER", "action": "solve", "payload": {}}],
            "current_step": "route_optimizer_dispatched",
        }

        res = await_route_results_node(state)
        # AC-4 & AC-9 replay safety invariant: must clear pending_tasks on resume
        assert res["pending_tasks"] == []
        assert res["current_step"] == "awaiting_route_results"


class TestTripPlannerConsumeRouteResult:
    """Tests for consuming worker output and constructing scheduled itinerary."""

    @pytest.mark.asyncio
    async def test_consume_feasible_route_result(self) -> None:
        route_id = uuid.uuid4()
        worker_output = RouteOptimizerWorkerOutput(
            status=RouteStatus.FEASIBLE,
            route_id=route_id,
            route_version=1,
            total_distance_m=185000,
            total_travel_seconds=14400,
            stops_count=2,
            legs_count=1,
            diagnostics=["Optimal route found within time limit"],
        )

        state: TripPlanningState = {
            "execution_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "_offline_fixture_mode": True,
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 1,
                        "confidence": 1.0,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-02",
                "travelers": 1,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {
                    "primary_name": "Digha",
                    "point": {"lat": 21.6266, "lng": 87.5074},
                    "suggested_stay_minutes": 120,
                    "is_hours_assumed": False,
                    "hours_status": "VERIFIED_HOURS",
                }
            ],
            "task_outputs": {
                "optimizer-task-1": worker_output.model_dump(mode="json"),
            },
        }

        res = await consume_route_result_node(state)

        assert res["current_step"] == "route_result_consumed"
        assert res["route_id"] == str(route_id)
        assert res["route_status"] == "FEASIBLE"
        assert res["routing_status"] == "FEASIBLE"
        assert "optimized_schedule" in res

        schedule = res["optimized_schedule"]
        stops = schedule["stops"]
        assert len(stops) == 2
        assert stops[0]["name"] == "Kolkata"
        assert stops[0]["hours_status"] == "ORIGIN"
        assert stops[1]["name"] == "Digha"
        assert stops[1]["hours_status"] == "VERIFIED_HOURS"

    @pytest.mark.asyncio
    async def test_consume_infeasible_route_result(self) -> None:
        route_id = uuid.uuid4()
        worker_output = RouteOptimizerWorkerOutput(
            status=RouteStatus.INFEASIBLE,
            route_id=route_id,
            route_version=1,
            total_distance_m=0,
            total_travel_seconds=0,
            stops_count=0,
            legs_count=0,
            diagnostics=[
                "MANDATORY_STOP_WINDOW_BREACH: Digha cannot be visited within opening window"
            ],
        )

        state: TripPlanningState = {
            "execution_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "_offline_fixture_mode": True,
            "task_outputs": {
                "optimizer-task-1": worker_output.model_dump(mode="json"),
            },
        }

        res = await consume_route_result_node(state)

        assert res["current_step"] == "route_result_consumed"
        assert res["route_id"] == str(route_id)
        assert res["route_status"] == "INFEASIBLE"
        assert res["routing_status"] == "INFEASIBLE"
        assert "MANDATORY_STOP_WINDOW_BREACH" in res["route_diagnostics"][0]

        schedule = res["optimized_schedule"]
        assert schedule["stops"] == []
        assert schedule["legs"] == []
        assert schedule["is_optimal"] is False

    @pytest.mark.asyncio
    async def test_consume_valhalla_outage_fail_closed_policy(self) -> None:
        state: TripPlanningState = {
            "execution_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "task_outputs": {
                "optimizer-task-1": {
                    "worker": "OPTIMIZER",
                    "action": "solve",
                    "status": "FAILED",
                    "routing_status": "ROUTING_UNAVAILABLE",
                    "error": {
                        "reason": "ValhallaConnectionError: VALHALLA_ENDPOINT_URL is unreachable",
                    },
                }
            },
        }

        res = await consume_route_result_node(state)

        # Conforms to 0005 fail closed routing: route_id is None, no guessed polylines or straight line speeds
        assert res["route_id"] is None
        assert res["route_status"] == "ROUTING_UNAVAILABLE"
        assert res["routing_status"] == "ROUTING_UNAVAILABLE"
        assert any("Road routing engine unavailable" in d for d in res["route_diagnostics"])
        assert res["optimized_schedule"]["legs"] == []

    @pytest.mark.asyncio
    async def test_consume_binds_to_specific_optimizer_task_ignoring_browser_failures(
        self,
    ) -> None:
        """Earlier browser or evidence failures must not be mistaken for route result."""
        route_id = uuid.uuid4()
        opt_task_id = str(uuid.uuid4())
        worker_output = RouteOptimizerWorkerOutput(
            status=RouteStatus.FEASIBLE,
            route_id=route_id,
            route_version=1,
            total_distance_m=150000,
            total_travel_seconds=10800,
            stops_count=2,
            legs_count=1,
            diagnostics=["Found route"],
        )

        state: TripPlanningState = {
            "execution_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "_offline_fixture_mode": True,
            "optimizer_task_id": opt_task_id,
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 1,
                        "confidence": 1.0,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-02",
                "travelers": 1,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {
                    "primary_name": "Digha",
                    "point": {"lat": 21.6266, "lng": 87.5074},
                    "suggested_stay_minutes": 120,
                    "is_hours_assumed": False,
                    "hours_status": "VERIFIED_HOURS",
                }
            ],
            "task_outputs": {
                "browser_task_scrape_failure": {
                    "worker": "BROWSER",
                    "action": "scrape",
                    "status": "FAILED",
                    "error": "Scrape timed out on target page",
                },
                "evidence_task_llm_failure": {
                    "worker": "EVIDENCE",
                    "action": "extract",
                    "status": "FAILED",
                    "error": "Rate limit exceeded",
                },
                opt_task_id: worker_output.model_dump(mode="json"),
            },
        }

        res = await consume_route_result_node(state)

        # Optimizer output must be consumed cleanly despite prior browser and evidence task failures
        assert res["current_step"] == "route_result_consumed"
        assert res["route_id"] == str(route_id)
        assert res["route_status"] == "FEASIBLE"
        assert res["routing_status"] == "FEASIBLE"

    @pytest.mark.asyncio
    async def test_consume_production_fails_closed_without_session_factory(self) -> None:
        """In production mode, missing session_factory fails closed without inventing a schedule."""
        route_id = uuid.uuid4()
        opt_task_id = str(uuid.uuid4())
        worker_output = RouteOptimizerWorkerOutput(
            status=RouteStatus.FEASIBLE,
            route_id=route_id,
            route_version=1,
            total_distance_m=150000,
            total_travel_seconds=10800,
            stops_count=2,
            legs_count=1,
            diagnostics=["Found route"],
        )

        state: TripPlanningState = {
            "execution_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "_offline_fixture_mode": False,
            "optimizer_task_id": opt_task_id,
            "task_outputs": {
                opt_task_id: worker_output.model_dump(mode="json"),
            },
        }

        res = await consume_route_result_node(state)

        assert res["route_id"] is None
        assert res["route_status"] == "ROUTING_UNAVAILABLE"
        assert res["routing_status"] == "ROUTING_UNAVAILABLE"
        assert (
            "Database session factory and owner ID required in production mode"
            in res["route_diagnostics"][0]
        )
        assert res["optimized_schedule"]["stops"] == []
        assert res["optimized_schedule"]["legs"] == []

    @pytest.mark.asyncio
    async def test_consume_production_fails_closed_when_persisted_route_missing_or_tenant_mismatch(
        self,
    ) -> None:
        """In production mode, if route repository lookup returns None, fail closed."""
        route_id = uuid.uuid4()
        opt_task_id = str(uuid.uuid4())
        owner_id = uuid.uuid4()
        worker_output = RouteOptimizerWorkerOutput(
            status=RouteStatus.FEASIBLE,
            route_id=route_id,
            route_version=1,
            total_distance_m=150000,
            total_travel_seconds=10800,
            stops_count=2,
            legs_count=1,
            diagnostics=["Found route"],
        )

        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session
        mock_factory.return_value.__aexit__.return_value = None

        state: TripPlanningState = {
            "execution_id": str(uuid.uuid4()),
            "owner_id": str(owner_id),
            "_offline_fixture_mode": False,
            "_session_factory": mock_factory,
            "optimizer_task_id": opt_task_id,
            "task_outputs": {
                opt_task_id: worker_output.model_dump(mode="json"),
            },
        }

        with patch(
            "functions.orchestrator.graphs.trip_planner.graph.RouteRepository"
        ) as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.get_route_by_id = AsyncMock(return_value=None)
            mock_repo_cls.return_value = mock_repo

            res = await consume_route_result_node(state)

        assert res["route_id"] is None
        assert res["route_status"] == "ROUTING_UNAVAILABLE"
        assert res["routing_status"] == "ROUTING_UNAVAILABLE"
        assert "tenant isolation barrier rejected access" in res["route_diagnostics"][0]
        assert res["optimized_schedule"]["stops"] == []

    @pytest.mark.asyncio
    async def test_consume_production_fails_closed_when_execution_mismatched(self) -> None:
        """In production mode, execution ID mismatch on RouteModel triggers fail closed."""
        route_id = uuid.uuid4()
        opt_task_id = str(uuid.uuid4())
        owner_id = uuid.uuid4()
        exec_id = uuid.uuid4()
        other_exec_id = uuid.uuid4()

        worker_output = RouteOptimizerWorkerOutput(
            status=RouteStatus.FEASIBLE,
            route_id=route_id,
            route_version=1,
            total_distance_m=150000,
            total_travel_seconds=10800,
            stops_count=2,
            legs_count=1,
            diagnostics=["Found route"],
        )

        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session
        mock_factory.return_value.__aexit__.return_value = None

        mock_route = MagicMock()
        mock_route.owner_id = owner_id
        mock_route.execution_id = other_exec_id
        mock_route.trip_id = None
        mock_route.status = "FEASIBLE"

        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "owner_id": str(owner_id),
            "_offline_fixture_mode": False,
            "_session_factory": mock_factory,
            "optimizer_task_id": opt_task_id,
            "task_outputs": {
                opt_task_id: worker_output.model_dump(mode="json"),
            },
        }

        with patch(
            "functions.orchestrator.graphs.trip_planner.graph.RouteRepository"
        ) as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.get_route_by_id = AsyncMock(return_value=mock_route)
            mock_repo_cls.return_value = mock_repo

            res = await consume_route_result_node(state)

        assert res["route_id"] is None
        assert res["route_status"] == "ROUTING_UNAVAILABLE"
        assert "does not match current execution" in res["route_diagnostics"][0]

    @pytest.mark.asyncio
    async def test_consume_production_fails_closed_when_stop_timestamps_missing(self) -> None:
        """In production mode, route stops lacking planned schedule timestamps must fail closed."""
        route_id = uuid.uuid4()
        opt_task_id = str(uuid.uuid4())
        owner_id = uuid.uuid4()
        exec_id = uuid.uuid4()

        worker_output = RouteOptimizerWorkerOutput(
            status=RouteStatus.FEASIBLE,
            route_id=route_id,
            route_version=1,
            total_distance_m=150000,
            total_travel_seconds=10800,
            stops_count=2,
            legs_count=1,
            diagnostics=["Found route"],
        )

        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session
        mock_factory.return_value.__aexit__.return_value = None

        mock_stop = MagicMock()
        mock_stop.name = "Digha"
        mock_stop.planned_arrival_at = None
        mock_stop.planned_departure_at = None

        mock_route = MagicMock()
        mock_route.owner_id = owner_id
        mock_route.execution_id = exec_id
        mock_route.trip_id = None
        mock_route.status = "FEASIBLE"
        mock_route.stops = [mock_stop]

        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "owner_id": str(owner_id),
            "_offline_fixture_mode": False,
            "_session_factory": mock_factory,
            "optimizer_task_id": opt_task_id,
            "task_outputs": {
                opt_task_id: worker_output.model_dump(mode="json"),
            },
        }

        with patch(
            "functions.orchestrator.graphs.trip_planner.graph.RouteRepository"
        ) as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.get_route_by_id = AsyncMock(return_value=mock_route)
            mock_repo_cls.return_value = mock_repo

            res = await consume_route_result_node(state)

        assert res["route_id"] is None
        assert res["route_status"] == "ROUTING_UNAVAILABLE"
        assert (
            "lacks planned arrival or departure schedule timestamps" in res["route_diagnostics"][0]
        )

    @pytest.mark.asyncio
    async def test_consume_production_validates_infeasible_route_in_db(self) -> None:
        """In production mode, infeasible route results are validated against RouteModel in DB."""
        route_id = uuid.uuid4()
        opt_task_id = str(uuid.uuid4())
        owner_id = uuid.uuid4()
        exec_id = uuid.uuid4()

        worker_output = RouteOptimizerWorkerOutput(
            status=RouteStatus.INFEASIBLE,
            route_id=route_id,
            route_version=1,
            total_distance_m=0,
            total_travel_seconds=0,
            stops_count=0,
            legs_count=0,
            diagnostics=["MANDATORY_STOP_WINDOW_BREACH: Impossible window"],
        )

        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session
        mock_factory.return_value.__aexit__.return_value = None

        mock_route = MagicMock()
        mock_route.owner_id = owner_id
        mock_route.execution_id = exec_id
        mock_route.trip_id = None
        mock_route.status = "INFEASIBLE"

        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "owner_id": str(owner_id),
            "_offline_fixture_mode": False,
            "_session_factory": mock_factory,
            "optimizer_task_id": opt_task_id,
            "task_outputs": {
                opt_task_id: worker_output.model_dump(mode="json"),
            },
        }

        with patch(
            "functions.orchestrator.graphs.trip_planner.graph.RouteRepository"
        ) as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.get_route_by_id = AsyncMock(return_value=mock_route)
            mock_repo_cls.return_value = mock_repo

            res = await consume_route_result_node(state)

        assert res["route_id"] == str(route_id)
        assert res["route_status"] == "INFEASIBLE"
        assert res["routing_status"] == "INFEASIBLE"
        assert "MANDATORY_STOP_WINDOW_BREACH" in res["route_diagnostics"][0]
        assert res["optimized_schedule"]["stops"] == []
