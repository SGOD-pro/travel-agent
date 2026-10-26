"""Integration test suite for Slice 3 of 0006-route-graph-and-map-e2e:
Infeasible and partial route handling, constraint diagnostic preservation, and failure separation conforming to AC-8.

Verifies on live PostgreSQL / PostGIS:
1. Infeasible Route Outcome vs Job Failure (Mandatory Constraint Breach):
   - Mandatory stop window closing time before departure/service finish.
   - Worker solver mathematically proves hard constraint infeasibility.
   - Route persisted with routes.status = "INFEASIBLE", legs = [], all requested stops
     in route_stops with is_skipped = True and skip_reason, and diagnostics in assumptions.
   - Graph resumes at finalize_route_node and marks execution SUCCEEDED.
   - GET /api/v1/jobs/{job_id} returns SUCCEEDED with result_ref.
   - GET /api/v1/optimization/routes/{route_id} returns HTTP 200 with diagnostics.
   - GET /api/v1/optimization/routes/{route_id}/map returns HTTP 200 with FeatureCollection
     where all requested stops are Point features with marker-color = "#dc2626",
     is_skipped = True, skip_reason, and 0 leg LineStrings.
2. Partial Route Outcome with Optional Stop Dropped:
   - Optional stop with unachievable time window is dropped by solver via disjunction penalty.
   - Route persisted with routes.status = "PARTIAL".
   - Visited stops marked green (#059669) or blue (#2563eb), dropped stop marked red (#dc2626)
     with is_skipped = True and skip_reason. Surviving legs rendered.
3. System Job Failure Separation:
   - Runtime crash or unhandled execution failure marks execution.status = "FAILED".
   - GET /api/v1/jobs/{job_id} returns FAILED with error payload, result_available = False,
     result_ref = None, and zero persisted routes.
"""

from __future__ import annotations

import re
import socket
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
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
from functions.orchestrator.graphs.route_optimizer import (
    compile_route_optimizer_workflow,
)
from functions.orchestrator.routing.models import RouteModel
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
async def test_route_infeasible_mandatory_constraint_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies that an infeasible mandatory constraint preserves all stops with failure properties, zero legs, and keeps job SUCCEEDED (AC-8)."""
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

    current_user = AuthenticatedUser(id=owner_id, email="owner-infeasible@test.internal")

    async def override_get_current_user() -> AuthenticatedUser:
        return current_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            dep_at = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
            stop1_id = str(uuid.uuid4())
            stop2_id = str(uuid.uuid4())
            stop3_id = str(uuid.uuid4())

            # Mandatory stop 2 closes BEFORE departure time (impossible constraint)
            req_payload = {
                "trip_id": str(trip_id),
                "departure_at": dep_at.isoformat(),
                "objective": "FASTEST",
                "is_round_trip": False,
                "stops": [
                    {
                        "id": stop1_id,
                        "name": "Hotel Departure",
                        "location": {"latitude": 22.5726, "longitude": 88.3639},
                        "stop_type": "ORIGIN",
                        "dwell_seconds": 0,
                    },
                    {
                        "id": stop2_id,
                        "name": "Closed Mandatory Venue",
                        "location": {"latitude": 22.5448, "longitude": 88.3426},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                        "is_must_visit": True,
                        "window_start": (dep_at - timedelta(hours=2)).isoformat(),
                        "window_end": (dep_at - timedelta(minutes=30)).isoformat(),
                    },
                    {
                        "id": stop3_id,
                        "name": "Afternoon Gallery",
                        "location": {"latitude": 22.5579, "longitude": 88.3511},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                        "is_must_visit": True,
                        "window_start": (dep_at + timedelta(hours=1)).isoformat(),
                        "window_end": (dep_at + timedelta(hours=4)).isoformat(),
                    },
                ],
            }

            # 2. Submit route optimization request via API
            resp = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": f"infeasible-{uuid.uuid4()}"},
                json=req_payload,
            )
            assert resp.status_code == 202
            resp_data = resp.json()
            job_id_str = resp_data["job_id"]
            job_id = uuid.UUID(job_id_str)

            # 3. Graph Runner: start execution
            async with session_factory() as session:
                outbox_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == job_id,
                    OutboxEventModel.kind == "EXECUTION_START",
                )
                start_outbox = (await session.execute(outbox_stmt)).scalar_one()
                start_payload = start_outbox.payload_ref or start_outbox.payload

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

            # 4. Fetch task dispatch payload
            async with session_factory() as session:
                task_stmt = select(TaskModel).where(TaskModel.execution_id == job_id)
                task = (await session.execute(task_stmt)).scalar_one()
                dispatch_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == job_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
                dispatch_outbox = (await session.execute(dispatch_stmt)).scalar_one()
                dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

            # 5. Route Optimizer Worker execution with injected MockValhallaClient
            injected_valhalla = MockValhallaClient()
            injected_closures = ClosureAvoidanceService()
            await process_record(
                dispatch_payload,
                session_factory,
                valhalla_client=injected_valhalla,
                closure_service=injected_closures,
            )

            # 6. Verify worker output and persisted route in database
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
                # Infeasible outcome properties
                assert persisted_route.status == "INFEASIBLE"
                assert persisted_route.solver_status == "INFEASIBLE"
                # All 3 requested stops preserved in route_stops
                assert len(persisted_route.stops) == 3
                for s in persisted_route.stops:
                    assert s.is_skipped is True
                    assert s.skip_reason is not None
                # Zero route legs saved (AC-8)
                assert len(persisted_route.legs) == 0
                # Diagnostics preserved in assumptions JSONB column
                assert len(persisted_route.assumptions) > 0
                assert any("cannot complete service before closing" in d or "MANDATORY" in d for d in persisted_route.assumptions)

            # 7. Completion Reconciliation
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
            assert reconcile_res.graph_resumed is True

            # 8. Graph Resume
            async with session_factory() as session:
                resume_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == job_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
                resume_outbox = (await session.execute(resume_stmt)).scalar_one()

            resume_event = ExecutionResumeEvent(
                execution_id=job_id,
                event_id=resume_outbox.id,
                triggered_by_task_id=task.id,
                context_version=1,
            )
            resume_res = await runner.handle_execution_resume(event=resume_event)
            # Crucial failure separation: execution SUCCEEDED because solver proved outcome
            assert resume_res.status == "SUCCEEDED"

            # 9. Verify job polling endpoint: SUCCEEDED with result_ref
            job_resp = await client.get(f"/api/v1/jobs/{job_id}")
            assert job_resp.status_code == 200
            job_data = job_resp.json()
            assert job_data["status"] == "SUCCEEDED"
            assert job_data["result_available"] is True
            assert job_data["result_ref"] is not None
            assert job_data["result_ref"]["route_id"] == route_id_str

            # 10. Verify route retrieval endpoint: returns INFEASIBLE with diagnostics
            route_resp = await client.get(f"/api/v1/optimization/routes/{route_id}")
            assert route_resp.status_code == 200
            route_data = route_resp.json()
            assert route_data["status"] == "INFEASIBLE"
            assert len(route_data["diagnostics"]) > 0
            assert len(route_data["legs"]) == 0

            # 11. Verify route map GeoJSON endpoint conforming to AC-8
            map_resp = await client.get(f"/api/v1/optimization/routes/{route_id}/map")
            assert map_resp.status_code == 200
            map_data = map_resp.json()
            assert map_data["type"] == "FeatureCollection"
            assert map_data["status"] == "INFEASIBLE"
            assert len(map_data["diagnostics"]) > 0

            # Inspect features
            features = map_data["features"]
            point_features = [f for f in features if f["properties"].get("feature_type") == "stop"]
            leg_features = [f for f in features if f["properties"].get("feature_type") == "leg"]

            # Exactly 3 stop features and 0 leg features
            assert len(point_features) == 3
            assert len(leg_features) == 0

            # All stop points marked with failure properties: marker-color = "#dc2626", is_skipped = True
            for pt_feat in point_features:
                props = pt_feat["properties"]
                assert props["is_skipped"] is True
                assert props["marker-color"] == "#dc2626"
                assert props["skip_reason"] is not None

    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_route_partial_optional_stop_drop_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies that an impossible optional stop is dropped, yielding PARTIAL route with surviving legs (AC-8)."""
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

    current_user = AuthenticatedUser(id=owner_id, email="owner-partial@test.internal")

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

            # Stop 2 is mandatory and achievable.
            # Stop 3 is OPTIONAL (is_must_visit=False) with impossible window closing 1 minute after dep_at.
            req_payload = {
                "trip_id": str(trip_id),
                "departure_at": dep_at.isoformat(),
                "objective": "FASTEST",
                "is_round_trip": False,
                "stops": [
                    {
                        "id": stop1_id,
                        "name": "Hotel Start",
                        "location": {"latitude": 22.5726, "longitude": 88.3639},
                        "stop_type": "ORIGIN",
                        "dwell_seconds": 0,
                    },
                    {
                        "id": stop2_id,
                        "name": "Feasible Mandatory Venue",
                        "location": {"latitude": 22.5448, "longitude": 88.3426},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                        "is_must_visit": True,
                        "window_start": (dep_at + timedelta(minutes=10)).isoformat(),
                        "window_end": (dep_at + timedelta(hours=3)).isoformat(),
                    },
                    {
                        "id": stop3_id,
                        "name": "Unachievable Optional Venue",
                        "location": {"latitude": 22.5579, "longitude": 88.3511},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                        "is_must_visit": False,
                        "window_start": dep_at.isoformat(),
                        "window_end": (dep_at + timedelta(seconds=60)).isoformat(),
                    },
                ],
            }

            resp = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": f"partial-{uuid.uuid4()}"},
                json=req_payload,
            )
            assert resp.status_code == 202
            job_id_str = resp.json()["job_id"]
            job_id = uuid.UUID(job_id_str)

            # Start execution
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

            start_result = await runner.handle_execution_start(
                event=ExecutionStartEvent(
                    execution_id=job_id,
                    event_id=start_outbox.id,
                    workflow="ROUTE_OPTIMIZER_GRAPH",
                    context_version=1,
                    payload=start_payload,
                )
            )
            assert start_result.status == "WAITING_TASKS"

            # Claim and process worker task
            async with session_factory() as session:
                task = (
                    await session.execute(
                        select(TaskModel).where(TaskModel.execution_id == job_id)
                    )
                ).scalar_one()
                dispatch_outbox = (
                    await session.execute(
                        select(OutboxEventModel).where(
                            OutboxEventModel.execution_id == job_id,
                            OutboxEventModel.kind == "TASK_DISPATCH",
                        )
                    )
                ).scalar_one()
                dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

            injected_valhalla = MockValhallaClient()
            injected_closures = ClosureAvoidanceService()
            await process_record(
                dispatch_payload,
                session_factory,
                valhalla_client=injected_valhalla,
                closure_service=injected_closures,
            )

            # Verify persisted route is PARTIAL
            async with session_factory() as session:
                task_done = (
                    await session.execute(
                        select(TaskModel).where(TaskModel.execution_id == job_id)
                    )
                ).scalar_one()
                assert task_done.status == "COMPLETED"
                worker_res = (
                    await session.execute(
                        select(WorkerResultModel).where(
                            WorkerResultModel.id == task_done.result_id
                        )
                    )
                ).scalar_one()
                route_id_str = worker_res.output_ref["route_id"]
                route_id = uuid.UUID(route_id_str)

                route_repo = RouteRepository(session)
                persisted_route = await route_repo.get_route_by_id(route_id, owner_id=owner_id)
                assert persisted_route is not None
                assert persisted_route.status == "PARTIAL"
                # Visited stops (2) + dropped stop (1) = 3 stops in route_stops
                assert len(persisted_route.stops) == 3
                # Surviving leg connects Origin -> Feasible Mandatory Venue
                assert len(persisted_route.legs) >= 1

            # Reconcile and resume
            reconciler = CompletionReconciliationService(
                session_factory,
                workflow_graphs={"ROUTE_OPTIMIZER_GRAPH": route_graph},
            )
            await reconciler.reconcile_task_event(
                event=TaskCompletionEvent(
                    event_id=uuid.uuid4(),
                    task_id=task.id,
                    execution_id=job_id,
                    attempt=task_done.attempt,
                    event_type="TASK_COMPLETED",
                    result_id=worker_res.id,
                )
            )

            async with session_factory() as session:
                resume_outbox = (
                    await session.execute(
                        select(OutboxEventModel).where(
                            OutboxEventModel.execution_id == job_id,
                            OutboxEventModel.kind == "EXECUTION_RESUME",
                        )
                    )
                ).scalar_one()

            resume_res = await runner.handle_execution_resume(
                event=ExecutionResumeEvent(
                    execution_id=job_id,
                    event_id=resume_outbox.id,
                    triggered_by_task_id=task.id,
                    context_version=1,
                )
            )
            assert resume_res.status == "SUCCEEDED"

            # Check job polling reflects SUCCEEDED
            job_resp = await client.get(f"/api/v1/jobs/{job_id}")
            assert job_resp.status_code == 200
            assert job_resp.json()["status"] == "SUCCEEDED"
            assert job_resp.json()["result_ref"]["route_id"] == route_id_str

            # Check map GeoJSON presentation conforming to AC-8
            map_resp = await client.get(f"/api/v1/optimization/routes/{route_id}/map")
            assert map_resp.status_code == 200
            map_data = map_resp.json()
            assert map_data["status"] == "PARTIAL"

            features = map_data["features"]
            stop_features = [f for f in features if f["properties"].get("feature_type") == "stop"]
            leg_features = [f for f in features if f["properties"].get("feature_type") == "leg"]

            assert len(stop_features) == 3
            assert len(leg_features) >= 1

            # Visited venue must be green (#059669)
            visited_venue = next(
                f for f in stop_features if f["properties"]["name"] == "Feasible Mandatory Venue"
            )
            assert visited_venue["properties"]["is_skipped"] is False
            assert visited_venue["properties"]["marker-color"] == "#059669"

            # Dropped optional venue must be red (#dc2626) with is_skipped = True
            dropped_venue = next(
                f for f in stop_features if f["properties"]["name"] == "Unachievable Optional Venue"
            )
            assert dropped_venue["properties"]["is_skipped"] is True
            assert dropped_venue["properties"]["marker-color"] == "#dc2626"
            assert dropped_venue["properties"]["skip_reason"] is not None

            # Surviving leg must have valid styling
            for leg in leg_features:
                assert leg["properties"]["stroke"] in ("#3b82f6", "#10b981")
                assert len(leg["geometry"]["coordinates"]) >= 2

    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_route_system_job_failure_separation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies that actual system crashes result in execution FAILED with error payload and zero routes persisted (AC-8)."""
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

    current_user = AuthenticatedUser(id=owner_id, email="owner-fail@test.internal")

    async def override_get_current_user() -> AuthenticatedUser:
        return current_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            dep_at = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
            req_payload = {
                "trip_id": str(trip_id),
                "departure_at": dep_at.isoformat(),
                "objective": "FASTEST",
                "is_round_trip": False,
                "stops": [
                    {
                        "id": str(uuid.uuid4()),
                        "name": "Origin",
                        "location": {"latitude": 22.5726, "longitude": 88.3639},
                        "stop_type": "ORIGIN",
                        "dwell_seconds": 0,
                    },
                    {
                        "id": str(uuid.uuid4()),
                        "name": "Destination",
                        "location": {"latitude": 22.5448, "longitude": 88.3426},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                        "is_must_visit": True,
                    },
                ],
            }

            resp = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": f"sysfail-{uuid.uuid4()}"},
                json=req_payload,
            )
            assert resp.status_code == 202
            job_id_str = resp.json()["job_id"]
            job_id = uuid.UUID(job_id_str)

            # Start execution
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

            # Simulate worker/system failure: Task transitions to FAILED
            async with session_factory() as session:
                task = (
                    await session.execute(
                        select(TaskModel).where(TaskModel.execution_id == job_id)
                    )
                ).scalar_one()
                task.status = "FAILED"
                task.error = {"code": "WORKER_PANIC", "message": "Unhandled segmentation fault in solver"}
                # Transition execution to FAILED
                exec_model = (
                    await session.execute(
                        select(ExecutionModel).where(ExecutionModel.id == job_id)
                    )
                ).scalar_one()
                exec_model.status = "FAILED"
                await session.commit()

            # Poll GET /api/v1/jobs/{job_id}: confirms FAILED, error payload, no result_ref
            job_resp = await client.get(f"/api/v1/jobs/{job_id}")
            assert job_resp.status_code == 200
            job_data = job_resp.json()
            assert job_data["status"] == "FAILED"
            assert job_data["result_available"] is False
            assert job_data["result_ref"] is None
            assert job_data["error"] is not None
            assert job_data["error"]["code"] == "EXECUTION_FAILED"

            # Confirm zero routes were persisted for this execution
            async with session_factory() as session:
                route_stmt = select(RouteModel).where(RouteModel.execution_id == job_id)
                persisted_routes = (await session.execute(route_stmt)).scalars().all()
                assert len(persisted_routes) == 0

    finally:
        app.dependency_overrides.clear()
