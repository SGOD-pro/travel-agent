"""Integration test suite for Slice 1 of 0006-route-graph-and-map-e2e:
backend submit → graph → worker → poll → map vertical slice.

Verifies on live PostgreSQL / PostGIS:
1. Atomically reserves owner-scoped idempotency, creates execution (workflow=ROUTE_OPTIMIZER_GRAPH),
   and stages EXECUTION_START in outbox.
2. Selects RouteOptimizerGraph by execution workflow for start and resume.
3. Graph nodes emit pending task specs; GraphRunnerService owns task/outbox insertion,
   WAITING_TASKS transition, and checkpoint commit.
4. Worker processes task with explicitly injected MockValhallaClient and commits route.
5. CompletionReconciliationService reconciles task event, updates checkpoint, and stages EXECUTION_RESUME.
6. GraphRunnerService resumes graph without duplicate dispatch, parses RouteOptimizerWorkerOutput.route_id,
   loads owner-scoped persisted route, and transitions execution to SUCCEEDED.
7. Job polling returns result_ref with route_id and map_url.
8. Durable job-to-route discovery via GET /api/v1/optimization/routes/by-job/{job_id}.
9. Authenticated map retrieval via GET /api/v1/optimization/routes/{route_id}/map returns
   RFC 7946 GeoJSON FeatureCollection with stop Points and provider leg LineStrings.
10. Strict tenant isolation on map and by-job endpoints (403 Forbidden across owners).
11. Idempotency reservation and replay protection.
"""

from __future__ import annotations

import re
import socket
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.config import settings
from config.db import get_session
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
from functions.orchestrator.execution.transport import OutboxMessage, SqsTransportAdapter
from functions.orchestrator.graphs.route_optimizer import (
    compile_route_optimizer_workflow,
)
from functions.orchestrator.routing.repository import RouteRepository
from functions.route_optimizer.closures import ClosureAvoidanceService
from functions.route_optimizer.consumer import process_record
from functions.route_optimizer.fixtures import MockValhallaClient
from middleware.auth import AuthenticatedUser, get_current_user

# Resilient DNS caching for PostgreSQL connectivity
_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", 20266): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("165.232.185.214", 20266),
        )
    ],
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", "20266"): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("165.232.185.214", 20266),
        )
    ],
}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
    last_err: Exception | None = None
    for _ in range(5):
        try:
            res = _orig_getaddrinfo(host, port, *args, **kwargs)
            _dns_cache[key] = res
            return res
        except socket.gaierror as e:
            last_err = e
            time.sleep(0.3)
    if key in _dns_cache:
        return _dns_cache[key]
    if last_err:
        raise last_err
    raise socket.gaierror(-5, "No address associated with hostname")


socket.getaddrinfo = _resilient_getaddrinfo


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Initializes live PostgreSQL connection pool."""
    url = settings.DATABASE_URL
    connect_args: dict[str, Any] = {
        "command_timeout": 60,
        "timeout": 30,
    }
    if (
        "sslmode=require" in url
        or "ssl=require" in url
        or "aivencloud.com" in url
        or settings.DB_SSL
    ):
        connect_args["ssl"] = "require"
    if "sslmode=" in url:
        url = re.sub(r"[?&]sslmode=[^&]+", "", url)
        if "?" not in url and "&" in url:
            url = url.replace("&", "?", 1)

    test_engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_route_graph_map_slice1_e2e_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies complete Slice 1 vertical flow on live PostgreSQL."""
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

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    current_user = AuthenticatedUser(id=owner_id, email="owner-slice1@test.internal")

    async def override_get_current_user() -> AuthenticatedUser:
        return current_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            dep_at = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
            stop1_id = str(uuid.uuid4())
            stop2_id = str(uuid.uuid4())
            stop3_id = str(uuid.uuid4())
            req_payload = {
                "trip_id": str(trip_id),
                "departure_at": dep_at.isoformat(),
                "objective": "FASTEST",
                "is_round_trip": False,
                "stops": [
                    {
                        "id": stop1_id,
                        "name": "Hotel Grand",
                        "location": {"latitude": 22.5726, "longitude": 88.3639},
                        "stop_type": "ORIGIN",
                        "dwell_seconds": 0,
                    },
                    {
                        "id": stop2_id,
                        "name": "Victoria Memorial",
                        "location": {"latitude": 22.5448, "longitude": 88.3426},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                        "is_must_visit": True,
                    },
                    {
                        "id": stop3_id,
                        "name": "Indian Museum",
                        "location": {"latitude": 22.5579, "longitude": 88.3511},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                        "is_must_visit": True,
                    },
                ],
            }
            idemp_key = f"slice1-{uuid.uuid4()}"

            # 2. Submit route optimization request via API
            resp = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": idemp_key},
                json=req_payload,
            )
            assert resp.status_code == 202
            resp_data = resp.json()
            job_id_str = resp_data["job_id"]
            job_id = uuid.UUID(job_id_str)
            assert resp_data["status"] == "QUEUED"
            assert resp_data["status_url"] == f"/api/v1/jobs/{job_id}"

            # 3. Replay / Idempotency verification: exact duplicate request returns 202 with identical job_id
            replay_resp = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": idemp_key},
                json=req_payload,
            )
            assert replay_resp.status_code == 202
            assert replay_resp.json()["job_id"] == job_id_str

            # Differing payload under same idempotency key returns 409 Conflict
            conflict_payload = {**req_payload, "objective": "SHORTEST"}
            conflict_resp = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": idemp_key},
                json=conflict_payload,
            )
            assert conflict_resp.status_code == 409

            # 4. Verify database state: execution created with ROUTE_OPTIMIZER_GRAPH, EXECUTION_START staged
            async with session_factory() as session:
                exec_stmt = select(ExecutionModel).where(ExecutionModel.id == job_id)
                execution = (await session.execute(exec_stmt)).scalar_one_or_none()
                assert execution is not None
                assert execution.status == "CREATED"
                assert execution.workflow == "ROUTE_OPTIMIZER_GRAPH"
                assert execution.owner_id == owner_id

                # Confirm NO tasks created yet by API
                task_stmt = select(TaskModel).where(TaskModel.execution_id == job_id)
                tasks_initial = (await session.execute(task_stmt)).scalars().all()
                assert len(tasks_initial) == 0

                # Confirm EXECUTION_START outbox event staged
                outbox_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == job_id,
                    OutboxEventModel.kind == "EXECUTION_START",
                )
                start_outbox = (await session.execute(outbox_stmt)).scalar_one_or_none()
                assert start_outbox is not None
                assert start_outbox.aggregate_id == job_id
                start_payload = start_outbox.payload_ref or start_outbox.payload

            # 5. Graph Runner: handle_execution_start
            checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
            route_graph = compile_route_optimizer_workflow(checkpointer=checkpointer)
            runner = GraphRunnerService(
                session_factory,
                workflow_graphs={"ROUTE_OPTIMIZER_GRAPH": route_graph},
            )

            start_event = ExecutionStartEvent(
                execution_id=job_id,
                event_id=start_outbox.id,
                workflow="ROUTE_OPTIMIZER_GRAPH",
                context_version=1,
                payload=start_payload,
            )
            start_result = await runner.handle_execution_start(event=start_event)
            assert start_result.status == "WAITING_TASKS"
            assert start_result.tasks_dispatched == 1
            assert start_result.graph_advanced is True

            # 6. Verify database after graph runner start: task created, TASK_DISPATCH staged, execution in WAITING_TASKS
            async with session_factory() as session:
                exec_waiting = (await session.execute(exec_stmt)).scalar_one()
                assert exec_waiting.status == "WAITING_TASKS"

                tasks_created = (await session.execute(task_stmt)).scalars().all()
                assert len(tasks_created) == 1
                task = tasks_created[0]
                assert task.worker == "OPTIMIZER"
                assert task.action == "solve"
                assert task.status == "PENDING"

                dispatch_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == job_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
                dispatch_outbox = (await session.execute(dispatch_stmt)).scalar_one()
                dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

            # 7. Transport / Relay: verify queue routing
            published_queues: list[str] = []

            class MockSqsClient:
                def send_message(self, QueueUrl: str, MessageBody: str, **kwargs: Any) -> dict[str, str]:
                    published_queues.append(QueueUrl)
                    return {"MessageId": f"mock-msg-{uuid.uuid4()}"}

            optimizer_queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/route-optimizer-queue"
            transport_adapter = SqsTransportAdapter(
                queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/default-queue",
                sqs_client=MockSqsClient(),
                worker_queues={"OPTIMIZER": optimizer_queue_url},
            )
            outbox_msg = OutboxMessage(
                event_id=dispatch_outbox.id,
                execution_id=dispatch_outbox.execution_id,
                task_id=dispatch_outbox.task_id,
                kind=dispatch_outbox.kind,
                payload=dispatch_payload,
                attempts=dispatch_outbox.attempts,
                created_at=dispatch_outbox.created_at,
            )
            await transport_adapter.publish(outbox_msg)
            assert optimizer_queue_url in published_queues

            # 8. Route Optimizer Worker execution with injected MockValhallaClient
            injected_valhalla = MockValhallaClient()
            injected_closures = ClosureAvoidanceService()
            await process_record(
                dispatch_payload,
                session_factory,
                valhalla_client=injected_valhalla,
                closure_service=injected_closures,
            )

            # 9. Verify worker output and persisted route in database
            async with session_factory() as session:
                task_done = (await session.execute(task_stmt)).scalar_one()
                assert task_done.status == "COMPLETED"
                result_stmt = select(WorkerResultModel).where(
                    WorkerResultModel.id == task_done.result_id
                )
                worker_res = (await session.execute(result_stmt)).scalar_one()
                assert worker_res.output_ref is not None
                route_id_str = worker_res.output_ref["route_id"]
                route_id = uuid.UUID(route_id_str)

                route_repo = RouteRepository(session)
                persisted_route = await route_repo.get_route_by_id(route_id, owner_id=owner_id)
                assert persisted_route is not None
                assert persisted_route.owner_id == owner_id
                # Canonical status check: FEASIBLE belongs to RouteStatus, OPTIMAL/FEASIBLE belongs to SolverStatus
                assert persisted_route.status == "FEASIBLE"
                assert persisted_route.solver_status in ("OPTIMAL", "FEASIBLE")
                assert len(persisted_route.stops) == 3
                assert len(persisted_route.legs) == 2

            # 10. Completion Reconciliation: reconcile_task_event
            reconciler = CompletionReconciliationService(
                session_factory,
                workflow_graphs={"ROUTE_OPTIMIZER_GRAPH": route_graph},
            )
            completion_event = TaskCompletionEvent(
                event_id=uuid.uuid4(),
                task_id=task.id,
                execution_id=job_id,
                attempt=task_done.attempt,
                event_type="TASK_COMPLETED",
                result_id=worker_res.id,
            )
            reconcile_res = await reconciler.reconcile_task_event(event=completion_event)
            assert reconcile_res.status == "APPLIED"
            assert reconcile_res.checkpoint_updated is True
            assert reconcile_res.graph_resumed is True

            # Verify execution status transitioned to RUNNING and EXECUTION_RESUME staged
            async with session_factory() as session:
                exec_running = (await session.execute(exec_stmt)).scalar_one()
                assert exec_running.status == "RUNNING"

                resume_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == job_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
                resume_outbox = (await session.execute(resume_stmt)).scalar_one()
                assert resume_outbox is not None

            # 11. Graph Runner: handle_execution_resume
            resume_event = ExecutionResumeEvent(
                execution_id=job_id,
                event_id=resume_outbox.id,
                triggered_by_task_id=task.id,
                context_version=1,
            )
            resume_res = await runner.handle_execution_resume(event=resume_event)
            assert resume_res.status == "SUCCEEDED"
            # Crucial: verify resumption cleared pending tasks without duplicate dispatch
            assert resume_res.tasks_dispatched == 0
            assert resume_res.graph_advanced is True

            # 12. Verify database state is terminal SUCCEEDED
            async with session_factory() as session:
                exec_succeeded = (await session.execute(exec_stmt)).scalar_one()
                assert exec_succeeded.status == "SUCCEEDED"

            # 13. Poll GET /api/v1/jobs/{job_id} confirms SUCCEEDED and provides result_ref
            job_resp = await client.get(f"/api/v1/jobs/{job_id}")
            assert job_resp.status_code == 200
            job_data = job_resp.json()
            assert job_data["status"] == "SUCCEEDED"
            assert job_data["result_available"] is True
            assert job_data["result_ref"] is not None
            assert job_data["result_ref"]["route_id"] == route_id_str
            assert job_data["result_ref"]["map_url"] == f"/api/v1/optimization/routes/{route_id}/map"

            # 14. Durable job-to-route discovery via GET /api/v1/optimization/routes/by-job/{job_id}
            by_job_resp = await client.get(f"/api/v1/optimization/routes/by-job/{job_id}")
            assert by_job_resp.status_code == 200
            by_job_data = by_job_resp.json()
            assert by_job_data["id"] == route_id_str
            assert by_job_data["status"] == "FEASIBLE"
            assert by_job_data["solver_status"] in ("OPTIMAL", "FEASIBLE")
            assert len(by_job_data["stops"]) == 3
            assert len(by_job_data["legs"]) == 2

            # 15. Authenticated map retrieval via GET /api/v1/optimization/routes/{route_id}/map
            map_resp = await client.get(f"/api/v1/optimization/routes/{route_id}/map")
            assert map_resp.status_code == 200
            map_data = map_resp.json()
            assert map_data["type"] == "FeatureCollection"
            assert map_data["route_id"] == route_id_str
            assert "bbox" in map_data and len(map_data["bbox"]) == 4

            features = map_data["features"]
            # 3 stop Points + 2 leg LineStrings = 5 features minimum
            point_features = [f for f in features if f["geometry"]["type"] == "Point"]
            linestring_features = [f for f in features if f["geometry"]["type"] == "LineString"]
            assert len(point_features) == 3
            assert len(linestring_features) == 2

            # Check simplestyle properties on stops
            for pt in point_features:
                props = pt["properties"]
                assert "marker-color" in props
                assert "marker-symbol" in props
                assert "stop_name" in props
                # Coordinates must be [longitude, latitude]
                coords = pt["geometry"]["coordinates"]
                assert len(coords) == 2
                assert -180 <= coords[0] <= 180
                assert -90 <= coords[1] <= 90

            # Check stroke styling on legs
            for line in linestring_features:
                props = line["properties"]
                assert "stroke" in props
                assert "stroke-width" in props
                assert "mode" in props
                coords = line["geometry"]["coordinates"]
                assert len(coords) >= 2

            # 16. Tenant isolation fencing checks
            other_user = AuthenticatedUser(id=uuid.uuid4(), email="attacker@test.internal")
            app.dependency_overrides[get_current_user] = lambda: other_user

            # Other user cannot access route by-job (403 Forbidden)
            forbidden_by_job = await client.get(f"/api/v1/optimization/routes/by-job/{job_id}")
            assert forbidden_by_job.status_code == 403
            assert "belongs to another tenant" in forbidden_by_job.json()["detail"]

            # Other user cannot access route map (403 Forbidden)
            forbidden_map = await client.get(f"/api/v1/optimization/routes/{route_id}/map")
            assert forbidden_map.status_code == 403
            assert "belongs to another tenant" in forbidden_map.json()["detail"]

            # Other user cannot access route by ID (403 Forbidden)
            forbidden_route = await client.get(f"/api/v1/optimization/routes/{route_id}")
            assert forbidden_route.status_code == 403

            # Non-existent job returns 404
            random_job = uuid.uuid4()
            not_found_job = await client.get(f"/api/v1/optimization/routes/by-job/{random_job}")
            assert not_found_job.status_code == 404

            # Non-existent route map returns 404
            random_route = uuid.uuid4()
            not_found_map = await client.get(f"/api/v1/optimization/routes/{random_route}/map")
            assert not_found_map.status_code == 404

    finally:
        app.dependency_overrides.clear()
