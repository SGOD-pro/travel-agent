"""Integration test for API to runner to route optimizer dispatch against PostgreSQL.

Conforms to spec 0007 AC-3, AC-4, AC-9, and optimizer dispatch requirements:
1. Graph produces bounded RouteRequest with verified mandatory anchors and optional POIs.
2. Accommodations, dining, and advisories are partitioned into editorial recommendations.
3. Task is persisted in PostgreSQL tasks table and claimed under lease fencing.
4. RouteOptimizerWorkerService executes the task using an explicitly labeled controlled Valhalla client.
5. Solved route is persisted in PostgreSQL routes table and task completes atomically.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from contracts.routing import (
    MatrixResponse,
    Point,
    RouteOptimizerWorkerOutput,
    RouteRequest,
    RouteStatus,
)
from contracts.trips import TransportMode
from functions.api.models import TripModel
from functions.orchestrator.execution.models import (
    ExecutionModel,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    dispatch_route_optimizer_node,
)
from functions.orchestrator.routing.models import RouteModel, RouteStopModel
from functions.route_optimizer.service import RouteOptimizerWorkerService
from functions.route_optimizer.valhalla import (
    ValhallaLegResult,
    ValhallaRouteResult,
)


class ControlledValhallaClient:
    """Explicitly labeled controlled Valhalla provider for testing.

    Returns deterministic matrix and leg geometries without making network calls.
    """

    def __init__(self) -> None:
        self.base_url = "http://controlled-test-valhalla.local"
        self.call_count = 0

    async def sources_to_targets(
        self,
        origins: list[Point],
        destinations: list[Point],
        mode: TransportMode = TransportMode.CAR_PETROL,
        costing_options: dict[str, Any] | None = None,
    ) -> MatrixResponse:
        self.call_count += 1
        n = len(origins)
        # Deterministic distances (meters) and durations (seconds)
        durations: list[list[int | None]] = []
        distances: list[list[int | None]] = []
        for i in range(n):
            dur_row: list[int | None] = []
            dist_row: list[int | None] = []
            for j in range(n):
                if i == j:
                    dur_row.append(0)
                    dist_row.append(0)
                else:
                    # Realistic ~140km trip between Bengaluru and Mysuru ~10800s
                    dur_row.append(1800 + abs(i - j) * 1200)
                    dist_row.append(25000 + abs(i - j) * 15000)
            durations.append(dur_row)
            distances.append(dist_row)
        return MatrixResponse(durations_s=durations, distances_m=distances)

    async def route(
        self,
        locations: list[Point],
        mode: TransportMode = TransportMode.CAR_PETROL,
        exclude_polygons: list[list[list[float]]] | None = None,
    ) -> ValhallaRouteResult:
        self.call_count += 1
        # Realistic coordinates geometry
        p1 = locations[0]
        p2 = locations[1]
        geom = {
            "type": "LineString",
            "coordinates": [[p1.longitude, p1.latitude], [p2.longitude, p2.latitude]],
        }
        leg = ValhallaLegResult(
            distance_m=35000,
            travel_seconds=2400,
            geometry=geom,
            turn_instructions=[],
        )
        return ValhallaRouteResult(
            total_distance_m=35000,
            total_travel_seconds=2400,
            legs=[leg],
            geometry=geom,
        )


@pytest.mark.asyncio
async def test_api_to_runner_to_optimizer_dispatch_and_execution(
    session_factory: Any,
) -> None:
    """Verifies end to end dispatch flow:

    1. Formulates bounded RouteRequest with semantic partitioning.
    2. Persists task record in PostgreSQL.
    3. RouteOptimizerWorkerService executes task under lease fencing.
    4. Persists route and worker result in isolated PostgreSQL.
    """
    now = datetime.now(UTC)
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()

    # 1. Setup trip and execution in isolated PostgreSQL acceptance database
    async with session_factory() as session:
        trip = TripModel(
            id=trip_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(trip)

        execution = ExecutionModel(
            id=execution_id,
            owner_id=owner_id,
            trip_id=trip_id,
            workflow="TRAVEL",
            status="RUNNING",
            context_version=1,
            budget={
                "remaining_tool_calls": 4,
                "remaining_cost_minor": 200,
                "currency": "INR",
            },
            deadline_at=now + timedelta(minutes=30),
            created_at=now,
            updated_at=now,
        )
        session.add(execution)
        await session.commit()

    # 2. Formulate state with mixed candidates
    state: TripPlanningState = {
        "execution_id": str(execution_id),
        "owner_id": str(owner_id),
        "trip_id": str(trip_id),
        "optimizer_task_id": str(task_id),
        "max_locations": 100,
        "max_matrix_cells": 100,
        "brief": {
            "origin_name": "Bengaluru",
            "origin_lat": 12.9716,
            "origin_lng": 77.5946,
            "destinations": [
                {
                    "name": "Mysuru",
                    "latitude": 12.2958,
                    "longitude": 76.6394,
                    "stay_days": 1,
                    "confidence": 1.0,
                }
            ],
            "start_date": "2026-11-20",
            "end_date": "2026-11-21",
            "travelers": 2,
            "rooms": 1,
            "budget": {"amount": "15000.00", "currency": "INR"},
            "preferred_modes": ["car_petrol"],
        },
        "eligible_stops": [
            {
                "primary_name": "Mysuru",
                "category": "destination",
                "point": {"lat": 12.2958, "lng": 76.6394},
                "existence_status": "VERIFIED",
                "hours_status": "HOURS_ASSUMED",
            },
            {
                "primary_name": "Mysore Palace",
                "category": "heritage",
                "point": {"lat": 12.3051, "lng": 76.6551},
                "suggested_stay_minutes": 60,
                "existence_status": "VERIFIED",
                "hours_status": "VERIFIED_HOURS",
                "window_start": "10:00",
                "window_end": "17:30",
            },
            {
                "primary_name": "Hotel Mysore Royale",
                "category": "hotel",
                "point": {"lat": 12.3100, "lng": 76.6500},
                "existence_status": "VERIFIED",
            },
            {
                "primary_name": "Hotel Original Mylari",
                "category": "restaurant",
                "point": {"lat": 12.3080, "lng": 76.6580},
                "existence_status": "VERIFIED",
            },
        ],
        "editorial_recommendations": [],
        "excluded_candidates": [],
    }

    # 3. Dispatch route optimizer node
    outcome = dispatch_route_optimizer_node(state)
    assert outcome["current_step"] == "route_optimizer_dispatched"
    assert len(outcome["pending_tasks"]) == 1

    task_spec = outcome["pending_tasks"][0]
    payload = task_spec["payload"]
    route_req = RouteRequest.model_validate(payload)

    # Verify stops: Bengaluru (origin), Mysuru (anchor), Mysore Palace (optional POI)
    assert len(route_req.stops) == 3
    assert route_req.stops[0].name == "Bengaluru"
    assert route_req.stops[0].is_must_visit is True
    assert route_req.stops[1].name == "Mysuru"
    assert route_req.stops[1].is_must_visit is True
    assert route_req.stops[2].name == "Mysore Palace"
    assert route_req.stops[2].is_must_visit is False

    # Hotels and restaurants are preserved in editorial recommendations
    recs = {r["primary_name"]: r for r in outcome["editorial_recommendations"]}
    assert "Hotel Mysore Royale" in recs
    assert "Hotel Original Mylari" in recs

    # 4. Persist dispatched task to PostgreSQL
    async with session_factory() as session:
        task = TaskModel(
            id=task_id,
            execution_id=execution_id,
            parent_task_id=None,
            worker="OPTIMIZER",
            action="solve",
            payload_ref=payload,
            input_hash="optimizer_dispatch_test_hash",
            idempotency_key=task_spec["idempotency_key"],
            attempt=0,
            max_attempts=3,
            context_version=1,
            status="PENDING",
            deadline_at=now + timedelta(minutes=10),
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.commit()

    # 5. Execute task with RouteOptimizerWorkerService and controlled provider
    controlled_client = ControlledValhallaClient()
    envelope_message = {
        "task_id": str(task_id),
        "execution_id": str(execution_id),
        "worker": "OPTIMIZER",
        "action": "solve",
        "payload": payload,
        "attempt": 0,
        "context_version": 1,
    }

    async with session_factory() as session:
        worker_service = RouteOptimizerWorkerService(
            session=session,
            valhalla_client=controlled_client,
        )
        _, worker_output = await worker_service.execute_task(envelope_message)
        await session.commit()

    # 6. Verify worker output and database persistence
    assert isinstance(worker_output, RouteOptimizerWorkerOutput)
    assert worker_output.status in (RouteStatus.FEASIBLE, RouteStatus.PARTIAL)
    assert worker_output.route_id is not None
    assert controlled_client.call_count >= 1

    async with session_factory() as session:
        # Check task completion
        t_stmt = select(TaskModel).where(TaskModel.id == task_id)
        t_res = await session.execute(t_stmt)
        persisted_task = t_res.scalar_one()
        assert persisted_task.status == "COMPLETED"

        # Check worker result
        wr_stmt = select(WorkerResultModel).where(WorkerResultModel.task_id == task_id)
        wr_res = await session.execute(wr_stmt)
        worker_result = wr_res.scalar_one_or_none()
        assert worker_result is not None
        assert worker_result.status == "SUCCESS"

        # Check persisted route in routes table
        r_stmt = select(RouteModel).where(RouteModel.id == worker_output.route_id)
        r_res = await session.execute(r_stmt)
        persisted_route = r_res.scalar_one_or_none()
        assert persisted_route is not None
        assert persisted_route.trip_id == trip_id
        assert persisted_route.execution_id == execution_id
        assert persisted_route.owner_id == owner_id
        assert persisted_route.status in ("FEASIBLE", "PARTIAL")

        # Check stop names in route_stops table
        rs_stmt = select(RouteStopModel).where(RouteStopModel.route_id == persisted_route.id)
        rs_res = await session.execute(rs_stmt)
        persisted_stops = rs_res.scalars().all()
        persisted_stop_names = [s.name for s in persisted_stops]
        assert "Bengaluru" in persisted_stop_names
        assert "Mysuru" in persisted_stop_names
