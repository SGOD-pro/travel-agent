"""Connected provider acceptance test for RouteOptimizerGraph GeoJSON map generation (AC-11).

Conforms strictly to specification 0006 Slice 5:
1. Skips cleanly when unconfigured or disabled (RUN_CONNECTED_VALHALLA_TESTS != '1' or VALHALLA_ENDPOINT_URL not set).
2. Fails with actionable diagnostics when enabled but unreachable.
3. Executes RouteOptimizerGraph with live Valhalla provider client against live PostgreSQL.
4. Verifies resulting GeoJSON FeatureCollection contains real polyline LineString geometries.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config.config import settings
from config.db import get_session
from contracts.routing import Point
from functions.api.app import app
from functions.api.models import TripModel
from functions.orchestrator.execution import (
    CompletionReconciliationService,
    ExecutionModel,
    ExecutionResumeEvent,
    ExecutionStartEvent,
    GraphRunnerService,
    OutboxEventModel,
    TaskCompletionEvent,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.graphs.route_optimizer import compile_route_optimizer_workflow
from functions.route_optimizer.closures import ClosureAvoidanceService
from functions.route_optimizer.consumer import process_record
from functions.route_optimizer.valhalla import ValhallaClient
from middleware.auth import AuthenticatedUser, get_current_user

# Reference coordinates in Bengaluru metropolitan corridor
MG_ROAD_BENGALURU = Point(latitude=12.9716, longitude=77.5946)
INDIRANAGAR_BENGALURU = Point(latitude=12.9784, longitude=77.6408)


def _is_connected_valhalla_enabled() -> tuple[bool, str]:
    """Checks whether live Valhalla provider testing is enabled and configured."""
    flag = os.getenv("RUN_CONNECTED_VALHALLA_TESTS", "0").strip().lower()
    if flag not in ("1", "true", "yes"):
        return False, "RUN_CONNECTED_VALHALLA_TESTS is not enabled (set RUN_CONNECTED_VALHALLA_TESTS=1)"

    endpoint = settings.VALHALLA_ENDPOINT_URL
    if not endpoint or not endpoint.strip():
        return False, "VALHALLA_ENDPOINT_URL is unconfigured in application settings"

    return True, ""


@pytest.mark.asyncio
async def test_route_graph_valhalla_connected_geojson_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies that RouteOptimizerGraph with live Valhalla produces verified decoded GeoJSON legs (AC-11)."""
    enabled, reason = _is_connected_valhalla_enabled()
    if not enabled:
        pytest.skip(reason)

    endpoint = settings.VALHALLA_ENDPOINT_URL
    client = ValhallaClient(endpoint)

    # Health check probe
    try:
        is_healthy = await client.check_health()
        assert is_healthy is True
    except Exception as exc:
        pytest.fail(f"Valhalla service at {endpoint} is unreachable: {exc}")

    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    now = datetime.now(UTC)

    # 1. Seed TripModel
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
        await session.commit()

    async def override_get_session():
        async with session_factory() as session:
            yield session

    current_user = AuthenticatedUser(id=owner_id, email="owner-valhalla@test.internal")

    async def override_get_current_user():
        return current_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            dep_at = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
            req_payload = {
                "trip_id": str(trip_id),
                "departure_at": dep_at.isoformat(),
                "objective": "FASTEST",
                "is_round_trip": False,
                "stops": [
                    {
                        "id": str(uuid.uuid4()),
                        "name": "Bengaluru Origin",
                        "location": {"latitude": MG_ROAD_BENGALURU.latitude, "longitude": MG_ROAD_BENGALURU.longitude},
                        "stop_type": "ORIGIN",
                        "dwell_seconds": 0,
                    },
                    {
                        "id": str(uuid.uuid4()),
                        "name": "Indiranagar Destination",
                        "location": {"latitude": INDIRANAGAR_BENGALURU.latitude, "longitude": INDIRANAGAR_BENGALURU.longitude},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 3600,
                        "is_must_visit": True,
                    },
                ],
            }

            # Submit route optimization request
            resp = await http_client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": f"valhalla-conn-{uuid.uuid4()}"},
                json=req_payload,
            )
            assert resp.status_code == 202
            job_id_str = resp.json()["job_id"]
            job_id = uuid.UUID(job_id_str)

            # Start LangGraph execution
            async with session_factory() as session:
                start_outbox = (
                    await session.execute(
                        select(OutboxEventModel).where(
                            OutboxEventModel.execution_id == job_id,
                            OutboxEventModel.kind == "EXECUTION_START",
                        )
                    )
                ).scalar_one()
                start_payload = start_outbox.payload_ref or start_outbox.payload

            checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
            route_graph = compile_route_optimizer_workflow(checkpointer=checkpointer)
            runner = GraphRunnerService(
                session_factory,
                workflow_graphs={"ROUTE_OPTIMIZER_GRAPH": route_graph},
            )

            await runner.handle_execution_start(
                event=ExecutionStartEvent(
                    execution_id=job_id,
                    event_id=start_outbox.id,
                    workflow="ROUTE_OPTIMIZER_GRAPH",
                    context_version=1,
                    payload=start_payload,
                )
            )

            # Route Optimizer Worker solves with real live Valhalla client
            async with session_factory() as session:
                dispatch_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == job_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
                dispatch_outbox = (await session.execute(dispatch_stmt)).scalar_one()
                dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

            closure_service = ClosureAvoidanceService()
            await process_record(
                dispatch_payload,
                session_factory,
                valhalla_client=client,
                closure_service=closure_service,
            )

            # Reconcile worker task completion
            task_stmt = select(TaskModel).where(TaskModel.execution_id == job_id)
            async with session_factory() as session:
                task_done = (await session.execute(task_stmt)).scalar_one()
                assert task_done.status == "COMPLETED"
                result_stmt = select(WorkerResultModel).where(
                    WorkerResultModel.id == task_done.result_id
                )
                worker_res = (await session.execute(result_stmt)).scalar_one()

            reconciler = CompletionReconciliationService(
                session_factory,
                workflow_graphs={"ROUTE_OPTIMIZER_GRAPH": route_graph},
            )
            completion_event = TaskCompletionEvent(
                event_id=uuid.uuid4(),
                task_id=task_done.id,
                execution_id=job_id,
                attempt=task_done.attempt,
                event_type="TASK_COMPLETED",
                result_id=worker_res.id,
            )
            reconcile_res = await reconciler.reconcile_task_event(event=completion_event)
            assert reconcile_res.status == "APPLIED"

            # Resume execution
            resume_stmt = select(OutboxEventModel).where(
                OutboxEventModel.execution_id == job_id,
                OutboxEventModel.kind == "EXECUTION_RESUME",
            )
            async with session_factory() as session:
                resume_outbox = (await session.execute(resume_stmt)).scalar_one()

            resume_event = ExecutionResumeEvent(
                execution_id=job_id,
                event_id=resume_outbox.id,
                triggered_by_task_id=task_done.id,
                context_version=1,
            )
            resume_res = await runner.handle_execution_resume(event=resume_event)
            assert resume_res.status == "SUCCEEDED"

            # Verify map GeoJSON retrieved via API
            by_job_resp = await http_client.get(f"/api/v1/optimization/routes/by-job/{job_id}")
            assert by_job_resp.status_code == 200
            route_id_str = by_job_resp.json()["id"]

            map_resp = await http_client.get(f"/api/v1/optimization/routes/{route_id_str}/map")
            assert map_resp.status_code == 200
            map_data = map_resp.json()

            assert map_data["type"] == "FeatureCollection"
            assert map_data["status"] in ("OPTIMAL", "FEASIBLE")

            leg_features = [f for f in map_data["features"] if f["properties"].get("feature_type") == "leg"]
            assert len(leg_features) >= 1

            driving_leg = leg_features[0]
            assert driving_leg["geometry"]["type"] == "LineString"
            # Real Valhalla geometry contains rich turn-by-turn shape points (> 50)
            coords = driving_leg["geometry"]["coordinates"]
            assert len(coords) >= 50
            # Distance from MG Road to Indiranagar is ~5.7 km
            dist_km = driving_leg["properties"]["distance_m"] / 1000
            assert 4.0 <= dist_km <= 8.0
    finally:
        app.dependency_overrides.clear()
        await client.aclose()
