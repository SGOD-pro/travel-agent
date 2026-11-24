"""Unit tests for bounded Valhalla fallback and truthful partial proposals.

Conforms to specification 0007 /develop bounded Valhalla fallback:
1. Matrix error 154 recovery on mandatory intercity leg using point to point /route.
2. Geometry reuse during hydration without redundant /route calls.
3. Multi stop recovery with local destination cluster matrix.
4. Matrix null cell recovery for required pairs.
5. Per execution call cap (max 3 probes) enforcement.
6. Execution deadline barrier enforcement before probes.
7. Deduplication of probe attempts for identical pairs.
8. Return deadline revalidation marking long legs INFEASIBLE.
9. Truthful partial proposal and incomplete budget publication.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from contracts.money import Currency
from contracts.routing import (
    Point,
    RouteObjective,
    RouteOptimizerWorkerOutput,
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
from functions.route_optimizer.service import (
    ExecutionDeadlineExceededError,
    RouteOptimizerDispatchEnvelope,
    RouteOptimizerWorkerService,
)
from functions.route_optimizer.valhalla import (
    MatrixResponse,
    ValhallaClient,
    ValhallaLegResult,
    ValhallaRouteResult,
    ValhallaRoutingError,
)


def _make_dummy_route_result(
    distance_m: int = 260000, travel_seconds: int = 18000
) -> ValhallaRouteResult:
    """Helper creating a valid mock ValhallaRouteResult."""
    leg = ValhallaLegResult(
        distance_m=distance_m,
        travel_seconds=travel_seconds,
        geometry={"type": "LineString", "coordinates": [[77.5946, 12.9716], [75.7382, 12.4244]]},
        turn_instructions=[],
    )
    return ValhallaRouteResult(
        total_distance_m=distance_m,
        total_travel_seconds=travel_seconds,
        legs=[leg],
        geometry={"type": "LineString", "coordinates": [[77.5946, 12.9716], [75.7382, 12.4244]]},
    )


@pytest.mark.asyncio
async def test_valhalla_bounded_fallback_recovers_intercity_leg_on_matrix_154() -> None:
    """Tests that matrix HTTP 400 error 154 triggers /route fallback and recovers route."""
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
    mock_valhalla.route.return_value = _make_dummy_route_result(
        distance_m=260977, travel_seconds=20107
    )

    mock_route_repo = AsyncMock()
    mock_persisted = MagicMock()
    mock_persisted.id = uuid.uuid4()
    mock_persisted.route_version = 1
    mock_persisted.trip_id = trip_id
    mock_persisted.total_distance_m = 260977
    mock_persisted.total_travel_seconds = 20107
    mock_persisted.stops = [MagicMock(), MagicMock()]
    mock_persisted.legs = [MagicMock()]
    mock_persisted.fuel_amount = Decimal("3000.00")
    mock_persisted.fuel_currency = Currency.INR
    mock_route_repo.save_route.return_value = mock_persisted

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
        fencing_service=mock_fencing,
        route_repo=mock_route_repo,
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

    # Route recovered successfully via fallback
    assert persisted_route_id == mock_persisted.id
    assert isinstance(output, RouteOptimizerWorkerOutput)
    assert output.status == RouteStatus.FEASIBLE
    assert output.total_distance_m == 260977
    assert output.total_travel_seconds == 20107

    # Proves exactly 1 /route probe call occurred (hydration reused the cached result)
    assert mock_valhalla.route.call_count == 1


@pytest.mark.asyncio
async def test_valhalla_bounded_fallback_null_cell_recovery() -> None:
    """Tests that a null matrix cell for a required pair is recovered via /route probe."""
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
        stops=[
            RouteStopInput(
                name="Origin Hub",
                location=Point(latitude=12.9716, longitude=77.5946),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="Destination Point",
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
    # Matrix returns null for transition from origin (0) to destination (1)
    mock_valhalla.sources_to_targets.return_value = MatrixResponse(
        durations_s=[[0, None], [None, 0]],
        distances_m=[[0, None], [None, 0]],
    )
    mock_valhalla.route.return_value = _make_dummy_route_result(
        distance_m=260977, travel_seconds=20107
    )

    mock_route_repo = AsyncMock()
    mock_persisted = MagicMock()
    mock_persisted.id = uuid.uuid4()
    mock_persisted.route_version = 1
    mock_persisted.trip_id = trip_id
    mock_persisted.total_distance_m = 260977
    mock_persisted.total_travel_seconds = 20107
    mock_persisted.stops = [MagicMock(), MagicMock()]
    mock_persisted.legs = [MagicMock()]
    mock_persisted.fuel_amount = Decimal("3000.00")
    mock_persisted.fuel_currency = Currency.INR
    mock_route_repo.save_route.return_value = mock_persisted

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
        fencing_service=mock_fencing,
        route_repo=mock_route_repo,
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

    assert persisted_route_id == mock_persisted.id
    assert isinstance(output, RouteOptimizerWorkerOutput)
    assert output.status == RouteStatus.FEASIBLE
    # Both directional pairs (0 -> 1 and 1 -> 0) were null and probed
    assert mock_valhalla.route.call_count == 2


@pytest.mark.asyncio
async def test_valhalla_bounded_fallback_call_cap_enforced() -> None:
    """Tests that per execution call cap of 3 point to point probes is strictly enforced."""
    mock_session = AsyncMock()
    mock_valhalla = AsyncMock(spec=ValhallaClient)
    mock_valhalla.route.return_value = _make_dummy_route_result()

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
    )

    p0 = Point(latitude=12.0, longitude=77.0)
    p1 = Point(latitude=12.1, longitude=77.1)
    p2 = Point(latitude=12.2, longitude=77.2)
    p3 = Point(latitude=12.3, longitude=77.3)
    p4 = Point(latitude=12.4, longitude=77.4)

    # 3 allowed probes
    await worker_service._probe_route_point_to_point(
        p0, p1, mode=TransportMode.CAR_PETROL, client=mock_valhalla
    )
    await worker_service._probe_route_point_to_point(
        p0, p2, mode=TransportMode.CAR_PETROL, client=mock_valhalla
    )
    await worker_service._probe_route_point_to_point(
        p0, p3, mode=TransportMode.CAR_PETROL, client=mock_valhalla
    )
    assert mock_valhalla.route.call_count == 3

    # 4th probe exceeds call cap
    with pytest.raises(ValhallaRoutingError) as exc_info:
        await worker_service._probe_route_point_to_point(
            p0, p4, mode=TransportMode.CAR_PETROL, client=mock_valhalla
        )

    assert "call cap exceeded" in str(exc_info.value).lower()
    assert mock_valhalla.route.call_count == 3


@pytest.mark.asyncio
async def test_valhalla_bounded_fallback_deduplication() -> None:
    """Tests that duplicate probe requests for the same pair reuse cached outcome."""
    mock_session = AsyncMock()
    mock_valhalla = AsyncMock(spec=ValhallaClient)
    mock_valhalla.route.return_value = _make_dummy_route_result(
        distance_m=12345, travel_seconds=678
    )

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
    )

    p0 = Point(latitude=12.9716, longitude=77.5946)
    p1 = Point(latitude=12.4244, longitude=75.7382)

    res1 = await worker_service._probe_route_point_to_point(
        p0, p1, mode=TransportMode.CAR_PETROL, client=mock_valhalla
    )
    res2 = await worker_service._probe_route_point_to_point(
        p0, p1, mode=TransportMode.CAR_PETROL, client=mock_valhalla
    )

    assert res1.total_distance_m == 12345
    assert res2.total_distance_m == 12345
    assert mock_valhalla.route.call_count == 1


@pytest.mark.asyncio
async def test_valhalla_bounded_fallback_deadline_enforced() -> None:
    """Tests that probe execution halts when parent execution deadline is passed."""
    mock_session = AsyncMock()
    mock_valhalla = AsyncMock(spec=ValhallaClient)

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
    )

    expired_exec = ExecutionModel(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        workflow="TRAVEL",
        status="RUNNING",
        deadline_at=datetime.now(UTC) - timedelta(seconds=10),
    )

    p0 = Point(latitude=12.0, longitude=77.0)
    p1 = Point(latitude=12.1, longitude=77.1)

    with pytest.raises(ExecutionDeadlineExceededError):
        await worker_service._probe_route_point_to_point(
            p0,
            p1,
            mode=TransportMode.CAR_PETROL,
            client=mock_valhalla,
            exec_model=expired_exec,
        )

    assert mock_valhalla.route.call_count == 0


@pytest.mark.asyncio
async def test_long_road_leg_exceeding_return_deadline_marked_infeasible() -> None:
    """Tests that a road leg exceeding trip dates return deadline is marked INFEASIBLE."""
    dep_time = datetime(2026, 11, 28, 9, 0, tzinfo=UTC)
    # Trip ends Sunday 20:00 (35 hours after departure)
    ret_deadline = datetime(2026, 11, 29, 20, 0, tzinfo=UTC)

    # Road leg takes 40 hours (> return deadline of 35 hours)
    giant_drive_seconds = 40 * 3600
    giant_distance_m = 1400000

    req = RouteRequest(
        owner_id=uuid.uuid4(),
        departure_at=dep_time,
        return_deadline=ret_deadline,
        stops=[
            RouteStopInput(
                name="Start Origin",
                location=Point(latitude=12.0, longitude=77.0),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="Far Destination",
                location=Point(latitude=15.0, longitude=75.0),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                window_end=datetime(2026, 12, 1, 18, 0, tzinfo=UTC),
            ),
        ],
    )

    mock_session = AsyncMock()
    mock_valhalla = AsyncMock(spec=ValhallaClient)
    mock_valhalla.base_url = "http://valhalla.test"
    mock_valhalla.sources_to_targets.return_value = MatrixResponse(
        durations_s=[[0, giant_drive_seconds], [giant_drive_seconds, 0]],
        distances_m=[[0, giant_distance_m], [giant_distance_m, 0]],
    )
    mock_valhalla.route.return_value = _make_dummy_route_result(
        distance_m=giant_distance_m,
        travel_seconds=giant_drive_seconds,
    )

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
    )

    solved = await worker_service._solve_route_with_provider(req)

    assert solved.status == RouteStatus.INFEASIBLE
    assert any("deadlines" in d or "SCHEDULE_INFEASIBLE" in d for d in solved.diagnostics)


@pytest.mark.asyncio
async def test_full_pipeline_proposal_and_budget_marked_partial_when_routing_unavailable() -> None:
    """Tests that missing mandatory routing enforces partial proposal and incomplete budget."""
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
        preferred_modes=[TransportMode.CAR_PETROL, TransportMode.TRAIN],
        budget_limit=None,
    )

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
            "legs": [],
        },
        "validated_candidates": [],
        "discovered_places": [],
        "editorial_recommendations": [],
        "excluded_candidates": [],
        "route_diagnostics": [
            "Road routing engine unavailable; stops preserved without driving schedule or route polyline",
            "Valhalla route distance limit exceeded (HTTP 400, error_code 154 on /route): leg distance exceeds provider threshold (1500 km)",
        ],
        "advisory_research": {"status": "ADVISORY_UNKNOWN", "advisories": []},
        "_offline_fixture_mode": True,
    }

    # 1. Compute budget
    budget_result = await compute_budget_node(state)
    summary = budget_result["budget_summary"]
    lines = budget_result["budget_lines"]

    assert len(lines) == 2
    assert lines[0]["category"] in ("miscellaneous", "transport")
    assert lines[0]["amount"] is None
    assert lines[1]["category"] == "accommodation"
    assert lines[1]["amount"] == "3500.00"
    assert lines[1]["estimated"] is True

    # Budget completeness is False
    assert summary["is_complete"] is False
    assert summary["unknown_count"] == 1

    # 2. Synthesize proposal
    cast(dict[str, Any], state).update(budget_result)
    synth_result = synthesize_proposal_node(state)
    proposal = synth_result["proposal"]

    # Proposal status is partial
    assert proposal["status"] == "partial"
    assert proposal["routing_status"] == "ROUTING_UNAVAILABLE"
    assert proposal["route_id"] is None


@pytest.mark.asyncio
async def test_valhalla_bounded_fallback_multistop_cluster_recovery() -> None:
    """Tests multi stop recovery with local destination cluster matrix when intercity matrix fails with 154."""
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

    # 3 stops: Origin (Bengaluru), Hub (Madikeri), Local Attraction (Abbey Falls)
    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=now,
        objective=RouteObjective.FASTEST,
        is_round_trip=True,
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
            RouteStopInput(
                name="Abbey Falls",
                location=Point(latitude=12.4517, longitude=75.7177),
                stop_type=StopType.WAYPOINT,
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

    # 1. Global matrix fails with error 154
    # 2. Local matrix for [Madikeri, Abbey Falls] succeeds
    local_matrix = MatrixResponse(
        durations_s=[
            [0, 900],
            [900, 0],
        ],
        distances_m=[
            [0, 8000],
            [8000, 0],
        ],
    )

    async def mock_sources_to_targets(
        *args: Any, origins: list[Point], **kwargs: Any
    ) -> MatrixResponse:
        if len(origins) == 3:
            raise ValhallaRoutingError(
                "Valhalla client error 400 for /sources_to_targets: Path distance exceeds the max distance limit: 150000 meters",
                status_code=400,
                error_code=154,
                endpoint="/sources_to_targets",
            )
        return local_matrix

    mock_valhalla.sources_to_targets.side_effect = mock_sources_to_targets

    # Point to point route probes return realistic legs
    intercity_route = _make_dummy_route_result(distance_m=260000, travel_seconds=20000)
    return_route = _make_dummy_route_result(distance_m=260000, travel_seconds=20000)
    local_route = _make_dummy_route_result(distance_m=8000, travel_seconds=900)

    async def mock_route(*args: Any, locations: list[Point], **kwargs: Any) -> ValhallaRouteResult:
        if len(locations) == 3:
            leg1 = ValhallaLegResult(
                distance_m=8000,
                travel_seconds=900,
                geometry={
                    "type": "LineString",
                    "coordinates": [
                        [locations[0].longitude, locations[0].latitude],
                        [locations[1].longitude, locations[1].latitude],
                    ],
                },
                turn_instructions=[],
            )
            leg2 = ValhallaLegResult(
                distance_m=8000,
                travel_seconds=900,
                geometry={
                    "type": "LineString",
                    "coordinates": [
                        [locations[1].longitude, locations[1].latitude],
                        [locations[2].longitude, locations[2].latitude],
                    ],
                },
                turn_instructions=[],
            )
            return ValhallaRouteResult(
                total_distance_m=16000,
                total_travel_seconds=1800,
                legs=[leg1, leg2],
                geometry={
                    "type": "LineString",
                    "coordinates": [
                        [locations[0].longitude, locations[0].latitude],
                        [locations[1].longitude, locations[1].latitude],
                        [locations[2].longitude, locations[2].latitude],
                    ],
                },
            )
        loc0, loc1 = locations[0], locations[1]
        if loc0.latitude == 12.9716 and loc1.latitude == 12.4244:
            return intercity_route
        elif loc0.latitude == 12.4244 and loc1.latitude == 12.9716:
            return return_route
        return local_route

    mock_valhalla.route.side_effect = mock_route

    mock_persisted = MagicMock()
    mock_persisted.id = uuid.uuid4()
    mock_persisted.route_version = 1
    mock_persisted.trip_id = trip_id
    mock_persisted.total_distance_m = 528000
    mock_persisted.total_travel_seconds = 40900
    mock_persisted.stops = [MagicMock(), MagicMock(), MagicMock()]
    mock_persisted.legs = [MagicMock(), MagicMock(), MagicMock()]
    mock_persisted.fuel_amount = Decimal("3500.00")
    mock_persisted.fuel_currency = "INR"

    mock_repo = AsyncMock()
    mock_repo.save_route.return_value = mock_persisted

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
        fencing_service=mock_fencing,
        route_repo=mock_repo,
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

    assert persisted_route_id == mock_persisted.id
    assert isinstance(output, RouteOptimizerWorkerOutput)
    assert output.status == RouteStatus.FEASIBLE
    assert mock_repo.save_route.call_count == 1
    solved_route = mock_repo.save_route.call_args[0][0]
    assert len(solved_route.legs) >= 2
    # Verify return leg from final stop has non-zero duration and distance
    return_leg = solved_route.legs[-1]
    assert return_leg.travel_seconds > 0
    assert return_leg.distance_m > 0
