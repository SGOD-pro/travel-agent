"""Focused production-path regression suite for route optimizer integration defects.

Verifies:
1. Provider failure:
   - Multimodal planning fails explicitly (ValhallaConnectionError) when provider is unconfigured.
   - Replan fails explicitly (ValhallaConnectionError) when provider is unconfigured.
   - RouteOptimizerWorkerService._load_active_restrictions fails explicitly (RuntimeError)
     when the database query fails.
2. Closure detour and geometry hydration:
   - Provider leg routing receives exclude_polygons for active verified road closures.
   - Solved and replanned legs persist actual detoured provider road geometries (not 2-point straight lines).
   - Leg and stop schedules (departure, arrival, wait, travel seconds) revalidate consistency with detour times.
   - Post-routing closure revalidation cleanly confirms zero spatial intersections.
3. Unrelated workflow reconciliation:
   - Non-optimizer workflows (e.g. TRAVEL) in WAITING_TASKS or RUNNING preserve RUNNING state,
     emit EXECUTION_RESUME, set graph_resumed=True, and are NOT terminalized to SUCCEEDED.
   - Standalone optimizer workflows (ROUTE_OPTIMIZATION, ROUTE_REPLAN) transition to terminal SUCCEEDED.
   - Terminal executions reject subsequent completions idempotently with PARENT_TERMINAL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.routing import (
    OptimizedRoute,
    Point,
    RestrictionType,
    RouteLegOutput,
    RouteObjective,
    RouteReplanRequest,
    RouteRequest,
    RouteStatus,
    RouteStopInput,
    ScheduledStopOutput,
    SolverStatus,
    StopType,
)
from contracts.trips import TransportMode
from functions.orchestrator.execution import (
    CompletionReconciliationService,
    ExecutionModel,
    OutboxEventModel,
    TaskCompletionEvent,
    TaskModel,
)
from functions.route_optimizer.closures import (
    ClosureAvoidanceService,
    RestrictionClaim,
)
from functions.route_optimizer.fixtures import MockValhallaClient
from functions.route_optimizer.multimodal import MultimodalTransitionService
from functions.route_optimizer.replan import RouteReplanService
from functions.route_optimizer.service import RouteOptimizerWorkerService
from functions.route_optimizer.valhalla import (
    ValhallaClient,
    ValhallaConnectionError,
)


@pytest.mark.asyncio
async def test_multimodal_fails_explicitly_when_valhalla_unconfigured() -> None:
    """Multimodal planning fails explicitly without fallback when Valhalla is unconfigured."""
    unconfigured_client = ValhallaClient(base_url="")
    service = MultimodalTransitionService(valhalla_client=unconfigured_client)

    t0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    request = RouteRequest(
        owner_id=uuid.uuid4(),
        trip_id=uuid.uuid4(),
        departure_at=t0,
        stops=[
            RouteStopInput(
                id=uuid.uuid4(),
                stop_order=0,
                name="Depot",
                location=Point(latitude=27.04, longitude=88.26),
                stop_type=StopType.ORIGIN,
                dwell_seconds=0,
            ),
            RouteStopInput(
                id=uuid.uuid4(),
                stop_order=1,
                name="Mall Pedestrian Area",
                location=Point(latitude=27.05, longitude=88.27),
                stop_type=StopType.DESTINATION,
                is_pedestrian_only=True,
                dwell_seconds=1800,
            ),
        ],
        transport_mode=TransportMode.CAR_PETROL,
        is_round_trip=True,
    )

    # Mock parking hub match to proceed to leg routing
    mock_place = MagicMock()
    mock_place.id = uuid.uuid4()
    mock_place.primary_name = "Central Parking"
    mock_place.point = MagicMock(x=88.268, y=27.048)
    service.find_verified_parking_hub = AsyncMock(return_value=(mock_place, 150.0))  # type: ignore[method-assign]

    with pytest.raises(ValhallaConnectionError, match="VALHALLA_ENDPOINT_URL is not configured"):
        await service.plan_multimodal_route(request)


@pytest.mark.asyncio
async def test_replan_fails_explicitly_when_valhalla_unconfigured() -> None:
    """Replan fails explicitly without fallback when Valhalla is unconfigured."""
    unconfigured_client = ValhallaClient(base_url="")
    service = RouteReplanService(valhalla_client=unconfigured_client)

    t0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    stop0_id = uuid.uuid4()
    stop1_id = uuid.uuid4()
    from contracts.routing import OptimizedRoute

    prev_route = OptimizedRoute(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        trip_id=uuid.uuid4(),
        route_version=1,
        status=RouteStatus.FEASIBLE,
        solver_status=SolverStatus.FEASIBLE,
        departure_at=t0,
        stops=[
            ScheduledStopOutput(
                id=stop0_id,
                stop_id=stop0_id,
                stop_order=0,
                name="Origin",
                location=Point(latitude=27.04, longitude=88.26),
                stop_type=StopType.ORIGIN,
                planned_arrival_at=t0,
                planned_departure_at=t0,
            ),
            ScheduledStopOutput(
                id=stop1_id,
                stop_id=stop1_id,
                stop_order=1,
                name="Stop 1",
                location=Point(latitude=27.05, longitude=88.27),
                stop_type=StopType.DESTINATION,
                planned_arrival_at=t0 + timedelta(minutes=15),
                planned_departure_at=t0 + timedelta(minutes=45),
            ),
        ],
        legs=[
            RouteLegOutput(
                leg_order=0,
                from_stop_id=stop0_id,
                to_stop_id=stop1_id,
                from_stop_name="Origin",
                to_stop_name="Stop 1",
                transport_mode=TransportMode.CAR_PETROL,
                departure_at=t0,
                arrival_at=t0 + timedelta(minutes=15),
                distance_m=3000,
                travel_seconds=900,
                geometry={"type": "LineString", "coordinates": [[88.26, 27.04], [88.27, 27.05]]},
            )
        ],
        total_distance_m=3000,
        total_travel_seconds=900,
        total_dwell_seconds=1800,
        total_wait_seconds=0,
        total_elapsed_seconds=2700,
    )

    replan_req = RouteReplanRequest(
        current_location=Point(latitude=27.042, longitude=88.262),
        replan_at=t0 + timedelta(minutes=10),
        completed_stop_ids=[stop0_id],
        expected_previous_version=1,
    )

    with pytest.raises(ValhallaConnectionError, match="VALHALLA_ENDPOINT_URL is not configured"):
        await service.replan_route(prev_route, replan_req)


@pytest.mark.asyncio
async def test_worker_load_active_restrictions_fails_explicitly_on_db_error() -> None:
    """Authoritative restriction lookup raises RuntimeError on database query failure."""
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = Exception("PostgreSQL connection terminated unexpectedly")

    worker = RouteOptimizerWorkerService(session, valhalla_client=MockValhallaClient())

    with pytest.raises(RuntimeError, match="Authoritative restriction lookup failed"):
        await worker._load_active_restrictions()


@pytest.mark.asyncio
async def test_closure_detour_hydrates_real_geometry_and_schedule() -> None:
    """Selected-leg routing passes exclude_polygons to provider and detours around road closures."""
    mock_valhalla = MockValhallaClient()
    session = AsyncMock(spec=AsyncSession)
    worker = RouteOptimizerWorkerService(session, valhalla_client=mock_valhalla)

    t0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    origin_loc = Point(latitude=27.040, longitude=88.260)
    dest_loc = Point(latitude=27.050, longitude=88.270)

    # Define a closure polygon intersecting the direct path (midpoint is around 27.045, 88.265)
    closure_poly = [
        [88.263, 27.043],
        [88.267, 27.043],
        [88.267, 27.047],
        [88.263, 27.047],
        [88.263, 27.043],
    ]
    restr_id = uuid.uuid4()
    restriction = RestrictionClaim(
        id=restr_id,
        claim_id=restr_id,
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry={"type": "Polygon", "coordinates": [closure_poly]},
        effective_start=t0 - timedelta(hours=1),
        effective_end=t0 + timedelta(hours=5),
        is_active=True,
        is_verified=True,
    )

    request = RouteRequest(
        owner_id=uuid.uuid4(),
        trip_id=uuid.uuid4(),
        departure_at=t0,
        stops=[
            RouteStopInput(
                id=uuid.uuid4(),
                stop_order=0,
                name="Start",
                location=origin_loc,
                stop_type=StopType.ORIGIN,
                dwell_seconds=0,
            ),
            RouteStopInput(
                id=uuid.uuid4(),
                stop_order=1,
                name="Destination",
                location=dest_loc,
                stop_type=StopType.DESTINATION,
                dwell_seconds=600,
            ),
        ],
        transport_mode=TransportMode.CAR_PETROL,
        is_round_trip=False,
    )

    # 1. Solve route without restrictions (direct path)
    direct_route = await worker._solve_route_with_provider(request, restrictions=[])

    # 2. Solve route with active road closure restriction
    detoured_route = await worker._solve_route_with_provider(request, restrictions=[restriction])

    # Provider must have received the exclude_polygons
    assert len(mock_valhalla.exclude_polygons_received) > 0

    # Detoured route must have more coordinates than a straight line (3 coordinates with detour midpoint)
    leg = detoured_route.legs[0]
    coords = leg.geometry.get("coordinates", [])
    assert len(coords) >= 3

    # Detour must increase travel seconds and distance compared to direct leg
    direct_leg = direct_route.legs[0]
    assert leg.travel_seconds > direct_leg.travel_seconds
    assert leg.distance_m > direct_leg.distance_m

    # Stop schedule consistency must reflect the detoured leg arrival time
    dest_stop = detoured_route.stops[1]
    assert dest_stop.planned_arrival_at == t0 + timedelta(seconds=leg.travel_seconds)
    assert dest_stop.planned_departure_at == dest_stop.planned_arrival_at + timedelta(seconds=dest_stop.dwell_seconds)

    # Post-routing closure revalidation must pass with zero intersections
    intersections = ClosureAvoidanceService.revalidate_leg_geometries(detoured_route.legs, [restriction])
    assert len(intersections) == 0


@pytest.mark.asyncio
async def test_reconciliation_preserves_running_for_unrelated_workflow() -> None:
    """Non-optimizer workflow (e.g. TRAVEL) in RUNNING is not terminalized to SUCCEEDED and emits EXECUTION_RESUME."""
    session_mock = AsyncMock(spec=AsyncSession)
    session_mock.__aenter__.return_value = session_mock
    session_mock.__aexit__.return_value = None
    session_mock.begin.return_value.__aenter__.return_value = session_mock
    session_mock.begin.return_value.__aexit__.return_value = None

    session_factory = MagicMock(return_value=session_mock)
    reconciler = CompletionReconciliationService(session_factory=session_factory)

    exec_id = uuid.uuid4()
    task_id = uuid.uuid4()
    event_id = uuid.uuid4()

    # Execution with workflow="TRAVEL" in status="WAITING_TASKS"
    execution = ExecutionModel(
        id=exec_id,
        workflow="TRAVEL",
        status="WAITING_TASKS",
        owner_id=uuid.uuid4(),
        context_version=1,
    )

    task = TaskModel(
        id=task_id,
        execution_id=exec_id,
        worker="DISCOVERY",
        action="search",
        status="RUNNING",
        attempt=1,
        context_version=1,
    )

    # Mock queries: 1. Execution, 2. Task, 3. Receipt insert, 4. Remaining tasks count (0), 5. Failed tasks count (0)
    def mock_execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        res = MagicMock()
        if "FROM executions" in str(stmt):
            res.scalar_one_or_none.return_value = execution
        elif "FROM tasks" in str(stmt) and "status IN" in str(stmt):
            # Count query for remaining tasks (0) or failed tasks (0)
            res.scalar.return_value = 0
        elif "FROM tasks" in str(stmt):
            res.scalar_one_or_none.return_value = task
        else:
            res.scalar.return_value = 1
            res.scalar_one_or_none.return_value = None
        return res

    session_mock.execute.side_effect = mock_execute

    event = TaskCompletionEvent(
        event_id=event_id,
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        output={"places": []},
    )

    result = await reconciler.reconcile_task_event(event=event)

    # Verification: execution status transitions from WAITING_TASKS to RUNNING, graph_resumed must be True
    assert execution.status == "RUNNING"
    assert result.graph_resumed is True
    assert result.status == "APPLIED"

    # Outbox must contain EXECUTION_RESUME event, NOT EXECUTION_SUCCEEDED
    added_outbox = [
        obj for obj in session_mock.add.call_args_list if isinstance(obj[0][0], OutboxEventModel)
    ]
    assert len(added_outbox) == 1
    outbox_event = added_outbox[0][0][0]
    assert outbox_event.kind == "EXECUTION_RESUME"

    # Subsequent event while RUNNING must preserve RUNNING and NOT emit EXECUTION_SUCCEEDED
    session_mock.add.reset_mock()
    task_2_id = uuid.uuid4()
    task_2 = TaskModel(
        id=task_2_id,
        execution_id=exec_id,
        worker="DISCOVERY",
        action="search",
        status="RUNNING",
        attempt=1,
        context_version=1,
    )
    event_2 = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_2_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        output={"places": []},
    )

    def mock_execute_2(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        res = MagicMock()
        if "FROM executions" in str(stmt):
            res.scalar_one_or_none.return_value = execution
        elif "FROM tasks" in str(stmt) and "status IN" in str(stmt):
            res.scalar.return_value = 0
        elif "FROM tasks" in str(stmt):
            res.scalar_one_or_none.return_value = task_2
        else:
            res.scalar.return_value = 1
            res.scalar_one_or_none.return_value = None
        return res

    session_mock.execute.side_effect = mock_execute_2

    result_2 = await reconciler.reconcile_task_event(event=event_2)
    assert execution.status == "RUNNING"
    assert result_2.graph_resumed is False
    assert result_2.status == "APPLIED"
    assert len([obj for obj in session_mock.add.call_args_list if isinstance(obj[0][0], OutboxEventModel)]) == 0


@pytest.mark.asyncio
async def test_reconciliation_terminalizes_optimizer_workflow_to_succeeded() -> None:
    """Standalone optimizer workflow (ROUTE_OPTIMIZATION) is terminalized to SUCCEEDED upon task completion."""
    session_mock = AsyncMock(spec=AsyncSession)
    session_mock.__aenter__.return_value = session_mock
    session_mock.__aexit__.return_value = None
    session_mock.begin.return_value.__aenter__.return_value = session_mock
    session_mock.begin.return_value.__aexit__.return_value = None

    session_factory = MagicMock(return_value=session_mock)
    reconciler = CompletionReconciliationService(session_factory=session_factory)

    exec_id = uuid.uuid4()
    task_id = uuid.uuid4()
    event_id = uuid.uuid4()

    execution = ExecutionModel(
        id=exec_id,
        workflow="ROUTE_OPTIMIZATION",
        status="CREATED",
        owner_id=uuid.uuid4(),
        context_version=1,
    )

    task = TaskModel(
        id=task_id,
        execution_id=exec_id,
        worker="OPTIMIZER",
        action="solve",
        status="RUNNING",
        attempt=1,
        context_version=1,
    )

    def mock_execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        res = MagicMock()
        if "FROM executions" in str(stmt):
            res.scalar_one_or_none.return_value = execution
        elif "FROM tasks" in str(stmt) and "status IN" in str(stmt):
            res.scalar.return_value = 0
        elif "FROM tasks" in str(stmt):
            res.scalar_one_or_none.return_value = task
        else:
            res.scalar.return_value = 1
            res.scalar_one_or_none.return_value = None
        return res

    session_mock.execute.side_effect = mock_execute

    event = TaskCompletionEvent(
        event_id=event_id,
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        output={"route_id": str(uuid.uuid4())},
    )

    result = await reconciler.reconcile_task_event(event=event)

    # Verification: execution status must become SUCCEEDED
    assert execution.status == "SUCCEEDED"
    assert result.graph_resumed is False
    assert result.status == "APPLIED"

    # Outbox must contain EXECUTION_SUCCEEDED
    added_outbox = [
        obj for obj in session_mock.add.call_args_list if isinstance(obj[0][0], OutboxEventModel)
    ]
    assert len(added_outbox) == 1
    outbox_event = added_outbox[0][0][0]
    assert outbox_event.kind == "EXECUTION_SUCCEEDED"


@pytest.mark.asyncio
async def test_reconciliation_terminal_state_idempotency() -> None:
    """Subsequent task completion events on already terminal executions return PARENT_TERMINAL."""
    session_mock = AsyncMock(spec=AsyncSession)
    session_mock.__aenter__.return_value = session_mock
    session_mock.__aexit__.return_value = None
    session_mock.begin.return_value.__aenter__.return_value = session_mock
    session_mock.begin.return_value.__aexit__.return_value = None

    session_factory = MagicMock(return_value=session_mock)
    reconciler = CompletionReconciliationService(session_factory=session_factory)

    exec_id = uuid.uuid4()
    task_id = uuid.uuid4()
    event_id = uuid.uuid4()

    execution = ExecutionModel(
        id=exec_id,
        workflow="ROUTE_OPTIMIZATION",
        status="SUCCEEDED",  # Already terminal
        owner_id=uuid.uuid4(),
        context_version=1,
    )

    task = TaskModel(
        id=task_id,
        execution_id=exec_id,
        worker="OPTIMIZER",
        action="solve",
        status="COMPLETED",
        attempt=1,
        context_version=1,
    )

    def mock_execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        res = MagicMock()
        if "FROM executions" in str(stmt):
            res.scalar_one_or_none.return_value = execution
        elif "FROM tasks" in str(stmt):
            res.scalar_one_or_none.return_value = task
        else:
            res.scalar.return_value = 1
            res.scalar_one_or_none.return_value = None
        return res

    session_mock.execute.side_effect = mock_execute

    event = TaskCompletionEvent(
        event_id=event_id,
        task_id=task_id,
        execution_id=exec_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        output={"route_id": str(uuid.uuid4())},
    )

    result = await reconciler.reconcile_task_event(event=event)

    assert result.status == "PARENT_TERMINAL"
    assert result.checkpoint_updated is False
    assert result.graph_resumed is False


@pytest.mark.asyncio
async def test_solve_provider_detour_finishes_after_closing_returns_infeasible() -> None:
    """Verifies that when matrix durations allow a mandatory stop to fit before closing,
    but provider detour pushes dwell completion past closing, the solver returns INFEASIBLE.
    """
    t0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    stop1_id = uuid.uuid4()
    stop2_id = uuid.uuid4()

    request = RouteRequest(
        departure_at=t0,
        stops=[
            RouteStopInput(
                id=stop1_id,
                name="Origin",
                location=Point(latitude=23.6168, longitude=87.1305),
                stop_type=StopType.ORIGIN,
                dwell_seconds=0,
            ),
            RouteStopInput(
                id=stop2_id,
                name="Morning Museum",
                location=Point(latitude=23.6200, longitude=87.1350),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                window_start=t0,
                window_end=t0 + timedelta(minutes=30),  # 09:30
                dwell_seconds=900,  # 15 minutes
            ),
        ],
        is_round_trip=False,
    )

    closure_poly = [
        [87.1320, 23.6180],
        [87.1330, 23.6180],
        [87.1330, 23.6190],
        [87.1320, 23.6190],
        [87.1320, 23.6180],
    ]
    restr = RestrictionClaim(
        id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry={"type": "Polygon", "coordinates": [closure_poly]},
        effective_start=t0 - timedelta(hours=1),
        effective_end=t0 + timedelta(hours=24),
        is_active=True,
        is_verified=True,
    )

    mock_valhalla = MockValhallaClient(detour_extra_seconds=2000)
    session_mock = AsyncMock(spec=AsyncSession)
    worker_service = RouteOptimizerWorkerService(
        session=session_mock,
        valhalla_client=mock_valhalla,
        route_repo=MagicMock(),
    )

    solved_route = await worker_service._solve_route_with_provider(
        request, restrictions=[restr]
    )

    assert solved_route.status == RouteStatus.INFEASIBLE
    assert solved_route.solver_status == SolverStatus.INFEASIBLE
    assert any("SCHEDULE_INFEASIBLE" in d for d in solved_route.diagnostics)


@pytest.mark.asyncio
async def test_solve_optional_stop_dropped_yields_partial_with_provider_geometries() -> None:
    """Verifies that when an optional stop is dropped by the solver, the resulting route
    status is PARTIAL and all visited legs are hydrated with actual provider geometries.
    """
    t0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    origin_id = uuid.uuid4()
    mandatory_id = uuid.uuid4()
    optional_id = uuid.uuid4()

    request = RouteRequest(
        departure_at=t0,
        return_deadline=t0 + timedelta(hours=1),
        stops=[
            RouteStopInput(
                id=origin_id,
                name="Origin",
                location=Point(latitude=23.6168, longitude=87.1305),
                stop_type=StopType.ORIGIN,
                dwell_seconds=0,
            ),
            RouteStopInput(
                id=mandatory_id,
                name="Mandatory Temple",
                location=Point(latitude=23.6200, longitude=87.1350),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                window_start=t0,
                window_end=t0 + timedelta(hours=1),
                dwell_seconds=600,
            ),
            RouteStopInput(
                id=optional_id,
                name="Optional Distant Fort",
                location=Point(latitude=24.5000, longitude=88.0000),
                stop_type=StopType.DESTINATION,
                is_must_visit=False,
                window_start=t0,
                window_end=t0 + timedelta(minutes=10),
                dwell_seconds=1800,
            ),
        ],
        is_round_trip=False,
    )

    mock_valhalla = MockValhallaClient()
    session_mock = AsyncMock(spec=AsyncSession)
    worker_service = RouteOptimizerWorkerService(
        session=session_mock,
        valhalla_client=mock_valhalla,
        route_repo=MagicMock(),
    )

    solved_route = await worker_service._solve_route_with_provider(request)

    assert solved_route.status == RouteStatus.PARTIAL
    assert solved_route.solver_status in (SolverStatus.FEASIBLE, SolverStatus.OPTIMAL)
    assert len(solved_route.legs) == 1
    leg = solved_route.legs[0]
    assert leg.from_stop_id == origin_id
    assert leg.to_stop_id == mandatory_id
    assert len(leg.geometry["coordinates"]) >= 3
    assert leg.departure_at == t0
    assert any(s.id == optional_id and s.is_skipped for s in solved_route.dropped_stops)
    assert not any(s.id == optional_id for s in solved_route.stops)


@pytest.mark.asyncio
async def test_replan_provider_detour_finishes_after_closing_returns_infeasible() -> None:
    """Verifies that when a mid-trip replan has a mandatory remaining stop where matrix fits
    but provider detour finishes after venue closing, the replan returns INFEASIBLE.
    """
    t0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    origin_id = uuid.uuid4()
    completed_stop_id = uuid.uuid4()
    remaining_stop_id = uuid.uuid4()

    previous_route = OptimizedRoute(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        trip_id=uuid.uuid4(),
        route_version=1,
        status=RouteStatus.FEASIBLE,
        solver_status=SolverStatus.OPTIMAL,
        objective=RouteObjective.FASTEST,
        departure_at=t0,
        stops=[
            ScheduledStopOutput(
                id=origin_id,
                stop_order=0,
                name="Start",
                location=Point(latitude=23.6168, longitude=87.1305),
                stop_type=StopType.ORIGIN,
                planned_arrival_at=t0,
                planned_departure_at=t0,
                dwell_seconds=0,
                is_completed=True,
            ),
            ScheduledStopOutput(
                id=completed_stop_id,
                stop_order=1,
                name="First Stop",
                location=Point(latitude=23.6180, longitude=87.1320),
                stop_type=StopType.DESTINATION,
                planned_arrival_at=t0 + timedelta(minutes=10),
                planned_departure_at=t0 + timedelta(minutes=30),
                dwell_seconds=1200,
                is_completed=True,
            ),
            ScheduledStopOutput(
                id=remaining_stop_id,
                stop_order=2,
                name="Remaining Heritage Site",
                location=Point(latitude=23.6220, longitude=87.1370),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                window_start=t0,
                window_end=t0 + timedelta(minutes=50),
                dwell_seconds=900,
                planned_arrival_at=t0 + timedelta(minutes=35),
                planned_departure_at=t0 + timedelta(minutes=50),
                is_completed=False,
            ),
        ],
        legs=[],
    )

    replan_req = RouteReplanRequest(
        current_location=Point(latitude=23.6180, longitude=87.1320),
        replan_at=t0 + timedelta(minutes=30),
        completed_stop_ids=[completed_stop_id],
    )

    closure_poly = [
        [87.1330, 23.6190],
        [87.1350, 23.6190],
        [87.1350, 23.6210],
        [87.1330, 23.6210],
        [87.1330, 23.6190],
    ]
    restr = RestrictionClaim(
        id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry={"type": "Polygon", "coordinates": [closure_poly]},
        effective_start=t0 - timedelta(hours=1),
        effective_end=t0 + timedelta(hours=24),
        is_active=True,
        is_verified=True,
    )

    mock_valhalla = MockValhallaClient(detour_extra_seconds=1800)
    replan_service = RouteReplanService(valhalla_client=mock_valhalla)

    route_v2 = await replan_service.replan_route(
        previous_route, replan_req, restrictions=[restr]
    )

    assert route_v2.status == RouteStatus.INFEASIBLE
    assert route_v2.solver_status == SolverStatus.INFEASIBLE
    assert any("SCHEDULE_INFEASIBLE" in d for d in route_v2.diagnostics)


@pytest.mark.asyncio
async def test_replan_optional_stop_dropped_yields_partial_with_provider_geometries() -> None:
    """Verifies that during mid-trip replanning, when an optional stop is dropped,
    the replanned route returns PARTIAL with actual provider geometries for all newly scheduled legs.
    """
    t0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    origin_id = uuid.uuid4()
    completed_stop_id = uuid.uuid4()
    mandatory_stop_id = uuid.uuid4()
    optional_stop_id = uuid.uuid4()

    previous_route = OptimizedRoute(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        trip_id=uuid.uuid4(),
        route_version=1,
        status=RouteStatus.FEASIBLE,
        solver_status=SolverStatus.OPTIMAL,
        objective=RouteObjective.FASTEST,
        departure_at=t0,
        stops=[
            ScheduledStopOutput(
                id=origin_id,
                stop_order=0,
                name="Start",
                location=Point(latitude=23.6168, longitude=87.1305),
                stop_type=StopType.ORIGIN,
                planned_arrival_at=t0,
                planned_departure_at=t0,
                dwell_seconds=0,
                is_completed=True,
            ),
            ScheduledStopOutput(
                id=completed_stop_id,
                stop_order=1,
                name="Completed Stop",
                location=Point(latitude=23.6180, longitude=87.1320),
                stop_type=StopType.DESTINATION,
                planned_arrival_at=t0 + timedelta(minutes=10),
                planned_departure_at=t0 + timedelta(minutes=30),
                dwell_seconds=1200,
                is_completed=True,
            ),
            ScheduledStopOutput(
                id=mandatory_stop_id,
                stop_order=2,
                name="Mandatory Stop",
                location=Point(latitude=23.6200, longitude=87.1350),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                window_start=t0,
                window_end=t0 + timedelta(hours=3),
                dwell_seconds=600,
                is_completed=False,
            ),
            ScheduledStopOutput(
                id=optional_stop_id,
                stop_order=3,
                name="Optional Distant Venue",
                location=Point(latitude=24.5000, longitude=88.0000),
                stop_type=StopType.DESTINATION,
                is_must_visit=False,
                window_start=t0,
                window_end=t0 + timedelta(minutes=40),
                dwell_seconds=1800,
                is_completed=False,
            ),
        ],
        legs=[],
    )

    replan_req = RouteReplanRequest(
        current_location=Point(latitude=23.6180, longitude=87.1320),
        replan_at=t0 + timedelta(minutes=30),
        completed_stop_ids=[completed_stop_id],
        return_deadline=t0 + timedelta(hours=2),
    )

    mock_valhalla = MockValhallaClient()
    replan_service = RouteReplanService(valhalla_client=mock_valhalla)

    route_v2 = await replan_service.replan_route(previous_route, replan_req)

    assert route_v2.status == RouteStatus.PARTIAL
    assert route_v2.solver_status in (SolverStatus.FEASIBLE, SolverStatus.OPTIMAL)
    assert len(route_v2.legs) >= 1
    new_leg = route_v2.legs[-1]
    assert new_leg.to_stop_id == mandatory_stop_id
    assert len(new_leg.geometry["coordinates"]) >= 3
    assert any(s.id == optional_stop_id and s.is_skipped for s in route_v2.dropped_stops)
    assert not any(s.id == optional_stop_id for s in route_v2.stops)


@pytest.mark.asyncio
async def test_solve_optional_stop_detour_exceeds_closing_drops_stop_yields_partial() -> None:
    """Verifies that when a matrix schedule fits an optional stop before closing,
    but provider detour finishes service after venue closing, the optional stop is dropped,
    yielding PARTIAL with provider geometries on remaining scheduled legs.
    """
    t0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    origin_id = uuid.uuid4()
    optional_id = uuid.uuid4()
    mandatory_id = uuid.uuid4()

    request = RouteRequest(
        departure_at=t0,
        stops=[
            RouteStopInput(
                id=origin_id,
                name="Origin",
                location=Point(latitude=23.6168, longitude=87.1305),
                stop_type=StopType.ORIGIN,
                dwell_seconds=0,
            ),
            RouteStopInput(
                id=optional_id,
                name="Optional Historic Pavilion",
                location=Point(latitude=23.6200, longitude=87.1350),
                stop_type=StopType.DESTINATION,
                is_must_visit=False,
                window_start=t0,
                window_end=t0 + timedelta(minutes=30),
                dwell_seconds=900,
            ),
            RouteStopInput(
                id=mandatory_id,
                name="Mandatory Grand Temple",
                location=Point(latitude=23.6250, longitude=87.1400),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                window_start=t0,
                window_end=t0 + timedelta(hours=3),
                dwell_seconds=600,
            ),
        ],
        is_round_trip=False,
    )

    closure_poly = [
        [87.1320, 23.6180],
        [87.1330, 23.6180],
        [87.1330, 23.6190],
        [87.1320, 23.6190],
        [87.1320, 23.6180],
    ]
    restr = RestrictionClaim(
        id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry={"type": "Polygon", "coordinates": [closure_poly]},
        effective_start=t0 - timedelta(hours=1),
        effective_end=t0 + timedelta(hours=24),
        is_active=True,
        is_verified=True,
    )

    mock_valhalla = MockValhallaClient(detour_extra_seconds=2000)
    session_mock = AsyncMock(spec=AsyncSession)
    worker_service = RouteOptimizerWorkerService(
        session=session_mock,
        valhalla_client=mock_valhalla,
        route_repo=MagicMock(),
    )

    solved_route = await worker_service._solve_route_with_provider(
        request, restrictions=[restr]
    )

    assert solved_route.status == RouteStatus.PARTIAL
    assert solved_route.solver_status in (SolverStatus.FEASIBLE, SolverStatus.OPTIMAL)
    assert len(solved_route.legs) == 1
    leg = solved_route.legs[0]
    assert leg.from_stop_id == origin_id
    assert leg.to_stop_id == mandatory_id
    assert len(leg.geometry["coordinates"]) >= 3
    assert any(s.id == optional_id and s.is_skipped for s in solved_route.dropped_stops)


@pytest.mark.asyncio
async def test_replan_optional_stop_detour_exceeds_closing_drops_stop_yields_partial() -> None:
    """Verifies that during mid-trip replanning, when provider detour forces dwell completion
    past an optional venue's closing time, the optional stop is dropped and returns PARTIAL.
    """
    t0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    origin_id = uuid.uuid4()
    completed_stop_id = uuid.uuid4()
    optional_stop_id = uuid.uuid4()
    mandatory_stop_id = uuid.uuid4()

    previous_route = OptimizedRoute(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        trip_id=uuid.uuid4(),
        route_version=1,
        status=RouteStatus.FEASIBLE,
        solver_status=SolverStatus.OPTIMAL,
        objective=RouteObjective.FASTEST,
        departure_at=t0,
        stops=[
            ScheduledStopOutput(
                id=origin_id,
                stop_order=0,
                name="Start",
                location=Point(latitude=23.6168, longitude=87.1305),
                stop_type=StopType.ORIGIN,
                planned_arrival_at=t0,
                planned_departure_at=t0,
                dwell_seconds=0,
                is_completed=True,
            ),
            ScheduledStopOutput(
                id=completed_stop_id,
                stop_order=1,
                name="Completed Venue",
                location=Point(latitude=23.6180, longitude=87.1320),
                stop_type=StopType.DESTINATION,
                planned_arrival_at=t0 + timedelta(minutes=10),
                planned_departure_at=t0 + timedelta(minutes=30),
                dwell_seconds=1200,
                is_completed=True,
            ),
            ScheduledStopOutput(
                id=optional_stop_id,
                stop_order=2,
                name="Optional Museum",
                location=Point(latitude=23.6200, longitude=87.1350),
                stop_type=StopType.DESTINATION,
                is_must_visit=False,
                window_start=t0,
                window_end=t0 + timedelta(minutes=55),
                dwell_seconds=900,
                is_completed=False,
            ),
            ScheduledStopOutput(
                id=mandatory_stop_id,
                stop_order=3,
                name="Mandatory Shrine",
                location=Point(latitude=23.6250, longitude=87.1400),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                window_start=t0,
                window_end=t0 + timedelta(hours=3),
                dwell_seconds=600,
                is_completed=False,
            ),
        ],
        legs=[],
    )

    replan_req = RouteReplanRequest(
        current_location=Point(latitude=23.6180, longitude=87.1320),
        replan_at=t0 + timedelta(minutes=30),
        completed_stop_ids=[completed_stop_id],
    )

    closure_poly = [
        [87.1330, 23.6190],
        [87.1350, 23.6190],
        [87.1350, 23.6210],
        [87.1330, 23.6210],
        [87.1330, 23.6190],
    ]
    restr = RestrictionClaim(
        id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        restriction_type=RestrictionType.ROAD_CLOSURE,
        geometry={"type": "Polygon", "coordinates": [closure_poly]},
        effective_start=t0 - timedelta(hours=1),
        effective_end=t0 + timedelta(hours=24),
        is_active=True,
        is_verified=True,
    )

    mock_valhalla = MockValhallaClient(detour_extra_seconds=2000)
    replan_service = RouteReplanService(valhalla_client=mock_valhalla)

    route_v2 = await replan_service.replan_route(
        previous_route, replan_req, restrictions=[restr]
    )

    assert route_v2.status == RouteStatus.PARTIAL
    assert route_v2.solver_status in (SolverStatus.FEASIBLE, SolverStatus.OPTIMAL)
    assert len(route_v2.legs) >= 1
    new_leg = route_v2.legs[-1]
    assert new_leg.to_stop_id == mandatory_stop_id
    assert len(new_leg.geometry["coordinates"]) >= 3
    assert any(s.id == optional_stop_id and s.is_skipped for s in route_v2.dropped_stops)


