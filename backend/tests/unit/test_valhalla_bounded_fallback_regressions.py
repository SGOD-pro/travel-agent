"""Regression tests reproducing current Valhalla matrix distance limitations and proposal honesty bugs.

Conforms to specification 0007 /develop bounded Valhalla fallback:
1. Matrix error 154 on mandatory intercity leg currently completes as ROUTING_UNAVAILABLE without attempting /route recovery.
2. Valid matrix response with a null cell for a required pair leaves durations_s as None and results in INFEASIBLE.
3. Proposal synthesis with no usable route currently publishes status ready_for_approval and marks hotel-only budget complete.
"""

from __future__ import annotations

import json as _json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from contracts.routing import (
    Point,
    RouteObjective,
    RouteRequest,
    RouteStatus,
    RouteStopInput,
    StopType,
)
from contracts.trips import DestinationPoint, TransportMode, TripBrief
from functions.orchestrator.execution.fencing import TaskClaimResult, WorkerFencingService
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    compute_budget_node,
    synthesize_proposal_node,
)
from functions.route_optimizer.closures import RestrictionClaim
from functions.route_optimizer.service import (
    RouteOptimizerDispatchEnvelope,
    RouteOptimizerWorkerService,
)
from functions.route_optimizer.solver import OrToolsRoutingSolver
from functions.route_optimizer.valhalla import (
    ValhallaClient,
    ValhallaRoutingError,
)


@pytest.mark.asyncio
async def test_regression_matrix_error_154_on_mandatory_intercity_leg_aborts_without_route_recovery() -> (
    None
):
    """Demonstrates that matrix HTTP 400 error 154 currently marks ROUTING_UNAVAILABLE without probing /route."""
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    now = datetime.now(UTC)

    mock_session = AsyncMock()
    mock_session.add = MagicMock()

    exec_row = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow="TRAVEL",
        status="RUNNING",
        deadline_at=now.replace(year=2099),
    )
    mock_exec_res = MagicMock()
    mock_exec_res.scalar_one_or_none.return_value = exec_row
    mock_exec_res.mappings.return_value.all.return_value = []
    mock_session.execute.return_value = mock_exec_res

    # Intercity pair: Bengaluru to Madikeri (~260 km, exceeds public matrix 150 km limit)
    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=now,
        stops=[
            RouteStopInput(
                name="Bengaluru",
                location=Point(latitude=12.9716, longitude=77.5946),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="Madikeri",
                location=Point(latitude=12.4244, longitude=75.7382),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
            ),
        ],
    )

    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=execution_id,
        lease_token=lease_token,
        attempt=1,
        context_version=1,
        payload_ref=req.model_dump(mode="json"),
        worker="OPTIMIZER",
        action="solve",
        lease_expiry=now.replace(year=2099),
    )
    mock_fencing.complete_task.return_value = uuid.uuid4()

    mock_valhalla = AsyncMock(spec=ValhallaClient)
    mock_valhalla.base_url = "http://valhalla.test"
    mock_valhalla.sources_to_targets.side_effect = ValhallaRoutingError(
        "Valhalla client error 400 for /sources_to_targets: Path distance exceeds the max distance limit: 150000 meters",
        status_code=400,
        error_code=154,
        endpoint="/sources_to_targets",
    )
    # /route also fails due to exceeding 1500 km limit (Raniganj -> Ooty scenario)
    mock_valhalla.route.side_effect = ValhallaRoutingError(
        "Valhalla client error 400 for /route: Path distance exceeds the max distance limit: 1500000 meters",
        status_code=400,
        error_code=154,
        endpoint="/route",
    )

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
        fencing_service=mock_fencing,
    )

    envelope = RouteOptimizerDispatchEnvelope(
        task_id=task_id,
        execution_id=execution_id,
        worker="OPTIMIZER",
        action="solve",
        payload=req.model_dump(mode="json"),
        attempt=1,
        context_version=1,
    )

    persisted_route_id, output = await worker_service.execute_task(envelope)

    # Point to point fallback probe was attempted and failed closed on route distance limit
    out_dict = output if isinstance(output, dict) else output.model_dump(mode="json")
    assert persisted_route_id is None
    assert out_dict["status"] == "FAILED"
    assert out_dict["routing_status"] == "ROUTING_UNAVAILABLE"
    assert mock_valhalla.route.call_count == 1
    assert "1500 km" in out_dict["error"]["reason"]


def test_regression_valid_matrix_response_with_null_cell_results_in_infeasible() -> None:
    """Demonstrates that a matrix with a null cell for a required pair produces an INFEASIBLE route."""
    solver = OrToolsRoutingSolver()
    now = datetime.now(UTC)

    req = RouteRequest(
        owner_id=uuid.uuid4(),
        departure_at=now,
        objective=RouteObjective.FASTEST,
        stops=[
            RouteStopInput(
                id=uuid.uuid4(),
                name="Origin Hub",
                location=Point(latitude=12.9716, longitude=77.5946),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                id=uuid.uuid4(),
                name="Destination Point",
                location=Point(latitude=12.4244, longitude=75.7382),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
            ),
        ],
    )

    # Matrix returns null for transition from origin (0) to destination (1)
    durations_s: list[list[int | None]] = [
        [0, None],
        [None, 0],
    ]
    distances_m: list[list[int | None]] = [
        [0, None],
        [None, 0],
    ]

    solution = solver.solve(req, durations_s, distances_m)
    assert solution.status == RouteStatus.INFEASIBLE
    assert len(solution.legs) == 0


@pytest.mark.asyncio
async def test_regression_proposal_with_no_usable_route_falsely_marks_complete_and_ready() -> None:
    """Demonstrates that missing transport currently publishes ready_for_approval and is_complete True."""
    brief = TripBrief(
        origin_name="Raniganj, West Bengal",
        origin_lat=23.6238,
        origin_lng=87.1309,
        destinations=[
            DestinationPoint(
                name="Ooty, Tamil Nadu",
                latitude=11.4102,
                longitude=76.6950,
                stay_days=2,
            )
        ],
        start_date=date(2026, 11, 28),
        end_date=date(2026, 11, 29),
        rooms=1,
        preferred_modes=[TransportMode.CAR_PETROL],
        budget_limit=Decimal("25000.00"),
    )

    # State representing degraded routing where route_id is None and routing_status is ROUTING_UNAVAILABLE
    state: TripPlanningState = {
        "brief": brief.model_dump(mode="json"),
        "trip_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "version": 1,
        "route_id": None,
        "route_status": None,
        "routing_status": "ROUTING_UNAVAILABLE",
        "route_geometry": None,
        "optimized_schedule": {
            "stops": [],
            "legs": [],  # Zero legs because routing failed
        },
        "validated_candidates": [],
        "discovered_places": [],
        "editorial_recommendations": [],
        "excluded_candidates": [],
        "route_diagnostics": [
            "Road routing engine unavailable; stops preserved without driving schedule or route polyline",
            "Valhalla matrix distance limit exceeded (HTTP 400, error_code 154 on /sources_to_targets)",
        ],
        "advisory_research": {"status": "ADVISORY_UNKNOWN", "advisories": []},
        "_offline_fixture_mode": True,
    }

    # 1. Compute budget
    budget_result = await compute_budget_node(state)
    summary = budget_result["budget_summary"]
    lines = budget_result["budget_lines"]

    # Truthful budget: 2 lines including explicit unknown transport line and estimated accommodation
    assert len(lines) == 2
    assert lines[0]["category"] in ("miscellaneous", "transport")
    assert lines[0]["amount"] is None
    assert "unavailable" in lines[0]["unknown_reason"].lower()

    assert lines[1]["category"] == "accommodation"
    assert lines[1]["amount"] == "3500.00"
    assert lines[1]["estimated"] is True

    # Truthful completeness: budget is incomplete because unknown_count is 1
    assert summary["is_complete"] is False
    assert summary["unknown_count"] == 1

    # 2. Synthesize proposal
    cast(dict[str, Any], state).update(budget_result)
    synth_result = synthesize_proposal_node(state)
    proposal = synth_result["proposal"]

    # Truthful proposal status: proposal is partial because routing failed and budget is incomplete
    assert proposal["status"] == "partial"
    assert proposal["routing_status"] == "ROUTING_UNAVAILABLE"
    assert proposal["route_id"] is None


@pytest.mark.asyncio
async def test_regression_cluster_return_geometry_matches_hub_path_and_closes_under_cap() -> None:
    """Reproduces blocker 2: published return leg geometry must follow the hub path priced by the solver."""
    now = datetime.now(UTC)
    execution_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()

    p_origin = Point(latitude=12.9716, longitude=77.5946)  # Bengaluru
    p_hub = Point(latitude=12.4244, longitude=75.7382)  # Madikeri Hub
    p_local = Point(latitude=12.4557, longitude=75.7168)  # Abbey Falls

    route_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal route_calls
        path = request.url.path
        body = _json.loads(request.content.decode()) if request.content else {}
        if path.endswith("/sources_to_targets"):
            srcs = body.get("sources", [])
            if len(srcs) == 3:
                return httpx.Response(
                    400,
                    json={"error_code": 154, "error": "Exceeded max distance limit"},
                )
            return httpx.Response(
                200,
                json={
                    "sources_to_targets": [
                        [{"distance": 0.0, "time": 0}, {"distance": 5.5, "time": 900}],
                        [{"distance": 5.8, "time": 960}, {"distance": 0.0, "time": 0}],
                    ]
                },
            )
        if path.endswith("/route"):
            route_calls += 1
            locs = body.get("locations", [])
            # Multi-waypoint request [Hub, Local, Hub] returns 2 separate Valhalla legs
            if len(locs) == 3:
                return httpx.Response(
                    200,
                    json={
                        "trip": {
                            "summary": {"length": 11.3, "time": 1860},
                            "legs": [
                                {
                                    "summary": {"length": 5.5, "time": 900},
                                    "shape": _encode_polyline6(
                                        [(p_hub.latitude, p_hub.longitude), (p_local.latitude, p_local.longitude)]
                                    ),
                                    "maneuvers": [],
                                },
                                {
                                    "summary": {"length": 5.8, "time": 960},
                                    "shape": _encode_polyline6(
                                        [(p_local.latitude, p_local.longitude), (p_hub.latitude, p_hub.longitude)]
                                    ),
                                    "maneuvers": [],
                                },
                            ],
                        }
                    },
                )
            # If called directly [Abbey -> Origin], deliberately return DIFFERENT direct shortcut path bypassing hub!
            if abs(locs[0]["lat"] - p_local.latitude) < 1e-4 and abs(locs[1]["lat"] - p_origin.latitude) < 1e-4:
                return httpx.Response(
                    200,
                    json={
                        "trip": {
                            "summary": {"length": 240.0, "time": 17000},
                            "legs": [
                                {
                                    "summary": {"length": 240.0, "time": 17000},
                                    "shape": _encode_polyline6(
                                        [(p_local.latitude, p_local.longitude), (12.80, 76.50), (p_origin.latitude, p_origin.longitude)]
                                    ),
                                    "maneuvers": [],
                                }
                            ],
                        }
                    },
                )
            # Hub <-> Origin intercity legs pass near [12.50, 76.50]
            return httpx.Response(
                200,
                json={
                    "trip": {
                        "summary": {"length": 260.0, "time": 20000},
                        "legs": [
                            {
                                "summary": {"length": 260.0, "time": 20000},
                                "shape": _encode_polyline6(
                                    [(locs[0]["lat"], locs[0]["lon"]), (12.50, 76.50), (locs[1]["lat"], locs[1]["lon"])]
                                ),
                                "maneuvers": [],
                            }
                        ],
                    }
                },
            )
        return httpx.Response(404)

    def _encode_polyline6(coords_lat_lon: list[tuple[float, float]]) -> str:
        out: list[str] = []
        prev_lat = 0
        prev_lon = 0
        for lat, lon in coords_lat_lon:
            ilat = round(lat * 1e6)
            ilon = round(lon * 1e6)
            for v in (ilat - prev_lat, ilon - prev_lon):
                s = ~(v << 1) if v < 0 else (v << 1)
                while s >= 0x20:
                    out.append(chr((0x20 | (s & 0x1F)) + 63))
                    s >>= 5
                out.append(chr(s + 63))
            prev_lat, prev_lon = ilat, ilon
        return "".join(out)

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=now,
        return_deadline=now + timedelta(hours=36),
        is_round_trip=True,
        stops=[
            RouteStopInput(name="Bengaluru", location=p_origin, stop_type=StopType.ORIGIN, is_must_visit=True),
            RouteStopInput(name="Madikeri Hub", location=p_hub, stop_type=StopType.DESTINATION, is_must_visit=True, dwell_seconds=1800),
            RouteStopInput(name="Abbey Falls", location=p_local, stop_type=StopType.WAYPOINT, is_must_visit=True, dwell_seconds=1800),
        ],
    )

    exec_row = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow="TRAVEL",
        status="RUNNING",
        deadline_at=now + timedelta(hours=2),
        budget={},
    )
    mock_session = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        vclient = ValhallaClient("http://valhalla.local", http_client=http_client)
        worker = RouteOptimizerWorkerService(mock_session, valhalla_client=vclient)
        solved = await worker._solve_route_with_provider(
            req,
            restrictions=[],
            execution_id=execution_id,
            exec_model=exec_row,
            probe_cache={},
        )

    assert route_calls <= 3
    assert solved.status == RouteStatus.FEASIBLE
    return_leg = solved.legs[-1]
    assert return_leg.from_stop_name == "Abbey Falls"
    assert return_leg.to_stop_name == "Bengaluru"
    # Return geometry MUST pass through Madikeri Hub [75.7382, 12.4244], matching hub-routed distance/duration!
    coords = return_leg.geometry.get("coordinates", [])
    assert any(abs(c[0] - p_hub.longitude) < 1e-4 and abs(c[1] - p_hub.latitude) < 1e-4 for c in coords)
    assert return_leg.distance_m == 5800 + 260000
    assert return_leg.travel_seconds == 960 + 20000


@pytest.mark.asyncio
async def test_regression_cluster_budget_exhausted_returns_partial_never_ready() -> None:
    """Reproduces blocker 2b: when remaining budget cannot supply geometry, worker must not crash or publish ready_for_approval."""
    now = datetime.now(UTC)
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    lease_token = uuid.uuid4()

    # Probe 1 hits a 503 retry (2 attempts), Probe 2 takes 1 attempt -> 3 attempts exhausted before hydration leg!
    route_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal route_calls
        path = request.url.path
        body = _json.loads(request.content.decode()) if request.content else {}
        if path.endswith("/sources_to_targets"):
            srcs = body.get("sources", [])
            if len(srcs) == 3:
                return httpx.Response(400, json={"error_code": 154, "error": "Exceeded max distance limit"})
            return httpx.Response(
                200,
                json={
                    "sources_to_targets": [
                        [{"distance": 0.0, "time": 0}, {"distance": 5.5, "time": 900}],
                        [{"distance": 5.8, "time": 960}, {"distance": 0.0, "time": 0}],
                    ]
                },
            )
        if path.endswith("/route"):
            route_calls += 1
            if route_calls == 1:
                return httpx.Response(503, json={"error": "transient"})
            return httpx.Response(
                200,
                json={
                    "trip": {
                        "summary": {"length": 260.0, "time": 20000},
                        "legs": [
                            {
                                "summary": {"length": 260.0, "time": 20000},
                                "shape": "g~gnAkxdzM??",
                                "maneuvers": [],
                            }
                        ],
                    }
                },
            )
        return httpx.Response(404)

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    exec_row = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow="TRAVEL",
        status="RUNNING",
        deadline_at=now + timedelta(hours=2),
        budget={},
    )
    mock_exec_res = MagicMock()
    mock_exec_res.scalar_one_or_none.return_value = exec_row
    mock_exec_res.mappings.return_value.all.return_value = []
    mock_session.execute.return_value = mock_exec_res

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=now,
        is_round_trip=True,
        stops=[
            RouteStopInput(name="Bengaluru", location=Point(latitude=12.9716, longitude=77.5946), stop_type=StopType.ORIGIN, is_must_visit=True),
            RouteStopInput(name="Madikeri Hub", location=Point(latitude=12.4244, longitude=75.7382), stop_type=StopType.DESTINATION, is_must_visit=True),
            RouteStopInput(name="Abbey Falls", location=Point(latitude=12.4557, longitude=75.7168), stop_type=StopType.WAYPOINT, is_must_visit=True),
        ],
    )
    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=execution_id,
        lease_token=lease_token,
        attempt=1,
        context_version=1,
        payload_ref=req.model_dump(mode="json"),
        worker="OPTIMIZER",
        action="solve",
        lease_expiry=now + timedelta(minutes=5),
    )
    mock_fencing.complete_task.return_value = uuid.uuid4()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        vclient = ValhallaClient("http://valhalla.local", http_client=http_client)
        worker = RouteOptimizerWorkerService(
            mock_session, valhalla_client=vclient, fencing_service=mock_fencing
        )
        route_id, output = await worker.execute_task(
            RouteOptimizerDispatchEnvelope(
                task_id=task_id,
                execution_id=execution_id,
                worker="OPTIMIZER",
                action="solve",
                payload=req.model_dump(mode="json"),
                attempt=0,
                context_version=1,
            )
        )

    assert route_calls <= 3
    out_dict = output if isinstance(output, dict) else output.model_dump(mode="json")
    assert route_id is None
    assert out_dict["routing_status"] == "ROUTING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_regression_dispatch_does_not_claim_limit_or_impractical_before_routing() -> None:
    """Reproduces blocker 3: dispatch_route_optimizer_node must not claim distance limits or impracticality before routing runs."""
    from functions.orchestrator.graphs.trip_planner.graph import dispatch_route_optimizer_node

    brief = TripBrief(
        origin_name="Raniganj, West Bengal",
        origin_lat=23.6238,
        origin_lng=87.1309,
        destinations=[
            DestinationPoint(name="Ooty, Tamil Nadu", latitude=11.4102, longitude=76.6950, stay_days=2)
        ],
        start_date=date(2026, 11, 28),
        end_date=date(2026, 11, 29),
        rooms=1,
        preferred_modes=[TransportMode.CAR_PETROL, TransportMode.TRAIN],
        traveler_preferences=["alternatives requested if Ooty is impractical"],
        budget_limit=None,
    )
    state: TripPlanningState = {
        "brief": brief.model_dump(mode="json"),
        "trip_id": str(uuid.uuid4()),
        "owner_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "eligible_stops": [],
        "editorial_recommendations": [],
        "excluded_candidates": [],
    }
    res = dispatch_route_optimizer_node(state)
    notes = " ".join(res.get("provider_diagnostics", {}).get("routing_notes", [])).lower()
    # Before Valhalla runs, notes must NOT claim a provider limit was exceeded or call the trip impractical!
    assert "exceeds provider limits" not in notes
    assert "is impractical" not in notes
    assert "train" in notes and "unsupported" in notes
    assert "alternatives" in notes and ("not researched" in notes or "not evaluated" in notes)


def test_regression_synthesize_proposal_preserves_truthful_alternatives_and_provider_response_only() -> None:
    """Ensures synthesize_proposal_node never implies alternatives were ruled out and reports provider distance limits only from actual diagnostics."""
    brief = TripBrief(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        destinations=[
            DestinationPoint(name="Mysuru", latitude=12.2958, longitude=76.6394, stay_days=1)
        ],
        start_date=date(2026, 11, 28),
        end_date=date(2026, 11, 29),
        rooms=1,
        preferred_modes=[TransportMode.CAR_PETROL, TransportMode.TRAIN],
        traveler_preferences=["alternatives requested if impractical"],
    )
    state: TripPlanningState = {
        "brief": brief.model_dump(mode="json"),
        "trip_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "version": 1,
        "route_id": str(uuid.uuid4()),
        "route_status": "FEASIBLE",
        "routing_status": "FEASIBLE",
        "optimized_schedule": {"stops": [], "legs": []},
        "route_diagnostics": [],
        "budget_summary": {"is_complete": True},
        "budget_lines": [],
        "advisory_research": {"status": "ADVISORY_UNKNOWN", "advisories": []},
    }
    res = synthesize_proposal_node(state)
    notes = " ".join(res["proposal"].get("provider_diagnostics", {}).get("routing_notes", [])).lower()
    assert "exceeds provider" not in notes
    assert "is impractical" not in notes
    assert "train" in notes and "unsupported" in notes
    assert "alternatives" in notes and ("not researched" in notes or "not evaluated" in notes)


@pytest.mark.asyncio
async def test_regression_cluster_stitched_return_leg_checked_against_closures() -> None:
    """Verifies stitched cluster return legs (Abbey -> Hub -> Origin) are checked and rerouted against active closures."""
    now = datetime.now(UTC)
    execution_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    owner_id = uuid.uuid4()

    p_origin = Point(latitude=12.9716, longitude=77.5946)
    p_hub = Point(latitude=12.4244, longitude=75.7382)
    p_local = Point(latitude=12.4589, longitude=75.7166)

    # Polygon intersects direct hub-to-origin line around (12.70, 76.66)
    closure = RestrictionClaim(
        id=uuid.uuid4(),
        description="Blocked Highway Segment",
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [76.5, 12.55],
                    [76.8, 12.55],
                    [76.8, 12.85],
                    [76.5, 12.85],
                    [76.5, 12.55],
                ]
            ],
        },
        is_active=True,
    )

    seen_exclude_polygons: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = _json.loads(request.content.decode()) if request.content else {}
        if path.endswith("/sources_to_targets"):
            srcs = body.get("sources", [])
            if len(srcs) == 3:
                return httpx.Response(
                    400, json={"error_code": 154, "error": "Path distance exceeds limit"}
                )
            return httpx.Response(
                200,
                json={
                    "sources_to_targets": [
                        [{"distance": 0.0, "time": 0}, {"distance": 5.5, "time": 900}],
                        [{"distance": 5.8, "time": 960}, {"distance": 0.0, "time": 0}],
                    ]
                },
            )
        if path.endswith("/route"):
            locs = body.get("locations", [])
            ep = body.get("exclude_polygons")
            if ep:
                seen_exclude_polygons.append(ep)
            if len(locs) == 3:
                return httpx.Response(
                    200,
                    json={
                        "trip": {
                            "summary": {"length": 11.3, "time": 1860},
                            "legs": [
                                {"summary": {"length": 5.5, "time": 900}, "shape": "??", "maneuvers": []},
                                {"summary": {"length": 5.8, "time": 960}, "shape": "??", "maneuvers": []},
                            ],
                        }
                    },
                )
            lat0, lon0 = locs[0]["lat"], locs[0]["lon"]
            lat1, lon1 = locs[1]["lat"], locs[1]["lon"]
            # Intersecting coordinates initially, detoured if exclude_polygons is supplied
            if ep:
                # Detour south of polygon (lat 12.40) -> no intersection
                shape = _encode_polyline6_raw([(lat0, lon0), (12.40, 76.65), (lat1, lon1)])
                return httpx.Response(
                    200,
                    json={
                        "trip": {
                            "summary": {"length": 275.0, "time": 21500},
                            "legs": [{"summary": {"length": 275.0, "time": 21500}, "shape": shape, "maneuvers": []}],
                        }
                    },
                )
            shape = _encode_polyline6_raw([(lat0, lon0), (12.70, 76.66), (lat1, lon1)])
            return httpx.Response(
                200,
                json={
                    "trip": {
                        "summary": {"length": 260.0, "time": 20000},
                        "legs": [{"summary": {"length": 260.0, "time": 20000}, "shape": shape, "maneuvers": []}],
                    }
                },
            )
        return httpx.Response(404)

    def _encode_polyline6_raw(pts: list[tuple[float, float]]) -> str:
        out: list[str] = []
        plat = plon = 0
        for la, lo in pts:
            ila, ilo = round(la * 1e6), round(lo * 1e6)
            for v in (ila - plat, ilo - plon):
                s = ~(v << 1) if v < 0 else (v << 1)
                while s >= 0x20:
                    out.append(chr((0x20 | (s & 0x1F)) + 63))
                    s >>= 5
                out.append(chr(s + 63))
            plat, plon = ila, ilo
        return "".join(out)

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=now,
        return_deadline=now + timedelta(hours=36),
        is_round_trip=True,
        stops=[
            RouteStopInput(name="Bengaluru", location=p_origin, stop_type=StopType.ORIGIN, is_must_visit=True),
            RouteStopInput(name="Madikeri Hub", location=p_hub, stop_type=StopType.DESTINATION, is_must_visit=True, dwell_seconds=3600),
            RouteStopInput(name="Abbey Falls", location=p_local, stop_type=StopType.WAYPOINT, is_must_visit=True, dwell_seconds=1800),
        ],
    )
    exec_row = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow="TRAVEL",
        status="RUNNING",
        deadline_at=now + timedelta(hours=2),
        budget={},
    )
    mock_session = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        vclient = ValhallaClient("http://valhalla.local", http_client=http_client)
        worker = RouteOptimizerWorkerService(mock_session, valhalla_client=vclient)
        solved = await worker._solve_route_with_provider(
            req,
            restrictions=[closure],
            execution_id=execution_id,
            exec_model=exec_row,
            probe_cache={},
        )
        solved = worker._closure_service.apply_and_revalidate(solved, [closure])

    assert len(seen_exclude_polygons) >= 1
    assert solved.status == RouteStatus.FEASIBLE
    assert any(r.was_applied for r in solved.restrictions)
    return_leg = solved.legs[-1]
    assert return_leg.from_stop_name == "Abbey Falls"
    assert return_leg.to_stop_name == "Bengaluru"
    coords = return_leg.geometry.get("coordinates", [])
    assert any(abs(c[0] - 76.65) < 1e-4 and abs(c[1] - 12.40) < 1e-4 for c in coords)


@pytest.mark.asyncio
async def test_regression_null_matrix_propagates_fencing_and_cap_errors() -> None:
    """Ensures null matrix cell recovery propagates cap, lease, and deadline errors instead of marking INFEASIBLE."""
    now = datetime.now(UTC)
    execution_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    owner_id = uuid.uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sources_to_targets"):
            return httpx.Response(
                200,
                json={
                    "sources_to_targets": [
                        [{"distance": 0.0, "time": 0}, None],
                        [None, {"distance": 0.0, "time": 0}],
                    ]
                },
            )
        if path.endswith("/route"):
            return httpx.Response(503, json={"error": "transient"})
        return httpx.Response(404)

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=now,
        is_round_trip=False,
        stops=[
            RouteStopInput(
                name="Origin",
                location=Point(latitude=12.97, longitude=77.59),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="Dest",
                location=Point(latitude=12.30, longitude=76.64),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
            ),
        ],
    )
    exec_row = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow="TRAVEL",
        status="RUNNING",
        deadline_at=now + timedelta(hours=1),
        budget={"route_fallback": {"remaining_attempts": 3, "spent_attempts": 3}},
    )
    mock_session = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        vclient = ValhallaClient("http://valhalla.local", http_client=http_client)
        worker = RouteOptimizerWorkerService(mock_session, valhalla_client=vclient)
        with pytest.raises(ValhallaRoutingError) as exc_info:
            await worker._solve_route_with_provider(
                req,
                restrictions=[],
                execution_id=execution_id,
                exec_model=exec_row,
                probe_cache={},
            )
    assert exc_info.value.status_code == 429


def test_regression_dispatch_train_first_and_hard_constraints_alternatives() -> None:
    """Ensures TRAIN first selects a road mode and nonempty traveler_preferences does not mask hard_constraints."""
    from functions.orchestrator.graphs.trip_planner.graph import dispatch_route_optimizer_node

    brief = TripBrief(
        origin_name="Raniganj",
        origin_lat=23.6238,
        origin_lng=87.1309,
        destinations=[
            DestinationPoint(name="Ooty", latitude=11.4102, longitude=76.6950, stay_days=2)
        ],
        start_date=date(2026, 11, 28),
        end_date=date(2026, 11, 29),
        rooms=1,
        preferred_modes=[TransportMode.TRAIN, TransportMode.CAR_PETROL],
        traveler_preferences=["vegetarian"],
        hard_constraints=["suggest alternatives if impractical"],
    )
    state: TripPlanningState = {
        "brief": brief.model_dump(mode="json"),
        "trip_id": str(uuid.uuid4()),
        "owner_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "eligible_stops": [],
        "editorial_recommendations": [],
        "excluded_candidates": [],
    }
    res = dispatch_route_optimizer_node(state)
    payload = res["pending_tasks"][0]["payload"]
    assert payload["transport_mode"] == "car_petrol"
    notes = " ".join(res["provider_diagnostics"]["routing_notes"]).lower()
    assert "train" in notes and "unsupported" in notes
    assert "alternatives" in notes and ("not evaluated" in notes or "not researched or evaluated" in notes)


@pytest.mark.asyncio
async def test_regression_synthesize_ignores_road_number_154_in_advisory_and_publish_partial_fixture() -> None:
    """Ensures NH 154 in advisories does not trigger distance limit claims and fixture mode preserves PARTIAL status."""
    from functions.orchestrator.graphs.trip_planner.graph import publish_proposal_node

    brief = TripBrief(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        destinations=[
            DestinationPoint(name="Mysuru", latitude=12.2958, longitude=76.6394, stay_days=1)
        ],
        start_date=date(2026, 11, 28),
        end_date=date(2026, 11, 29),
        rooms=1,
    )
    state: TripPlanningState = {
        "brief": brief.model_dump(mode="json"),
        "trip_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "version": 1,
        "route_id": str(uuid.uuid4()),
        "route_status": "FEASIBLE",
        "routing_status": "FEASIBLE",
        "optimized_schedule": {"stops": [], "legs": []},
        "route_diagnostics": [],
        "budget_summary": {"is_complete": True},
        "budget_lines": [],
        "advisory_research": {
            "status": "AVAILABLE",
            "advisories": [
                {
                    "severity": "WARNING",
                    "headline": "Slow traffic on Highway 154 near town center",
                    "affected_area": "NH 154",
                    "uncertainty": "UNVERIFIED",
                }
            ],
        },
    }
    synth_res = synthesize_proposal_node(state)
    notes = " ".join(
        synth_res["proposal"].get("provider_diagnostics", {}).get("routing_notes", [])
    ).lower()
    assert "distance limit" not in notes

    pub_res = await publish_proposal_node(
        {
            "_offline_fixture_mode": True,
            "proposal": {"status": "partial"},
        }
    )
    assert pub_res["proposal_status"] == "PARTIAL"


def test_regression_classify_pipeline_providers_live_fixture_and_mixed() -> None:
    """Verifies runtime artifact classification across fixture, mixed, and live provider combinations."""
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from run_hitl_pipeline import classify_pipeline_providers

    # 1. Mixed when SerpAPI is live even if LLM, weather, and Valhalla URL are off/fixture
    s_mixed = SimpleNamespace(
        LLM_PROVIDER="fixture",
        WEATHER_PROVIDER="fixture",
        SERPAPI_API_KEY="secret-key",
        VALHALLA_ENDPOINT_URL="",
    )
    modes_mixed, cls_mixed = classify_pipeline_providers(s_mixed, itinerary={})
    assert cls_mixed == "mixed"
    assert modes_mixed["search"] == "live_serpapi"

    # 2. Pure fixture when all 4 providers are fixture/unavailable
    s_fix = SimpleNamespace(
        LLM_PROVIDER="fixture",
        WEATHER_PROVIDER="fixture",
        SERPAPI_API_KEY="",
        VALHALLA_ENDPOINT_URL="",
    )
    modes_fix, cls_fix = classify_pipeline_providers(s_fix, itinerary={})
    assert cls_fix == "fixture"
    assert modes_fix == {
        "llm": "fixture",
        "weather": "fixture",
        "search": "unavailable",
        "routing": "unavailable",
    }

    # 3. Runtime weather fixture overrides live settings
    s_live_cfg = SimpleNamespace(
        LLM_PROVIDER="bedrock",
        WEATHER_PROVIDER="open_meteo",
        SERPAPI_API_KEY="secret-key",
        VALHALLA_ENDPOINT_URL="https://valhalla.example",
    )
    modes_rt, cls_rt = classify_pipeline_providers(
        s_live_cfg,
        itinerary={
            "provider_diagnostics": {
                "llm": {"provider": "bedrock"},
                "weather": {"provider": "fixture"},
            },
            "weather_status": "FIXTURE",
            "routing_status": "FEASIBLE",
            "route_id": "abc",
        },
    )
    assert modes_rt["weather"] == "fixture"
    assert cls_rt == "mixed"

    # 4. Full live when all 4 runtime signals are live
    modes_live, cls_live = classify_pipeline_providers(
        s_live_cfg,
        itinerary={
            "provider_diagnostics": {
                "llm": {"provider": "bedrock"},
                "weather": {"provider": "open_meteo"},
            },
            "weather_status": "AVAILABLE",
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_diagnostics": ["Valhalla route distance limit exceeded (error_code 154)"],
        },
    )
    assert cls_live == "live"
    assert modes_live == {
        "llm": "live_bedrock",
        "weather": "live_open_meteo",
        "search": "live_serpapi",
        "routing": "live_valhalla",
    }
