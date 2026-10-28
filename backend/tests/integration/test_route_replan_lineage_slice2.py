"""Comprehensive vertical integration test suite for Slice 2: Mid-trip replanning, parent lineage, and version history.

Conforms strictly to specification 0006 AC-6, AC-7, AC-9:
- API acceptance: POST /api/v1/optimization/routes/{route_id}/replans validates tenant ownership,
  enforces optimistic concurrency (409 Conflict on version mismatch), atomically reserves
  idempotency, creates ExecutionModel(workflow="ROUTE_OPTIMIZER_GRAPH"), and stages EXECUTION_START.
- Graph Runner: executes RouteOptimizerGraph with action="replan", emits task specification,
  transitions to WAITING_TASKS, and commits checkpoint state.
- Worker Execution: claims replan task, injects traveler coordinates as new departure origin,
  freezes completed stops, calculates updated itinerary with MockValhallaClient, and persists RouteModel
  with parent_route_id and incremented route_version.
- Reconciliation & Resumption: reconciles TASK_COMPLETED, transitions to RUNNING, stages EXECUTION_RESUME,
  resumes graph to load persisted replanned route by ID, and sets execution SUCCEEDED.
- Discovery: GET /api/v1/jobs/{replan_job_id} returns populated result_ref;
  GET /api/v1/optimization/routes/by-job/{replan_job_id} returns updated route.
- Versions Lineage: GET /api/v1/optimization/routes/{route_id}/versions traverses parent_route_id
  lineage from root to child, calculating distance/duration deltas, dropped stop IDs, and replan_origin.
- Tenant isolation: Cross-tenant access to replan and versions endpoints returns HTTP 403 Forbidden.
"""

from __future__ import annotations

import re
import socket
import uuid
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
from functions.orchestrator.execution import (
    CompletionReconciliationService,
    ExecutionResumeEvent,
    ExecutionStartEvent,
    GraphRunnerService,
    OutboxEventModel,
    TaskCompletionEvent,
    TaskModel,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
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
            ("139.59.42.218", 20266),
        )
    ],
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", "20266"): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("139.59.42.218", 20266),
        )
    ],
}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
    try:
        res = _orig_getaddrinfo(host, port, *args, **kwargs)
        _dns_cache[key] = res
        return res
    except socket.gaierror:
        for cached_key, cached_val in _dns_cache.items():
            if cached_key[0] == host:
                return cached_val
        raise


@pytest.fixture(autouse=True)
def patch_dns() -> Any:
    socket.getaddrinfo = _resilient_getaddrinfo
    yield
    socket.getaddrinfo = _orig_getaddrinfo


@pytest.fixture
async def live_db_session_factory() -> Any:
    """Provides an isolated async_sessionmaker connecting to live PostgreSQL."""
    assert settings.DATABASE_URL, "DATABASE_URL must be configured"
    url = settings.DATABASE_URL
    connect_args: dict[str, Any] = {
        "command_timeout": 60,
        "timeout": 30,
        "statement_cache_size": 0,
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

    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_route_replan_and_lineage_lifecycle(
    live_db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies complete end-to-end Slice 2 replanning and version lineage lifecycle on live PostgreSQL."""
    session_factory = live_db_session_factory
    owner_id = uuid.uuid4()
    other_owner_id = uuid.uuid4()

    auth_user = AuthenticatedUser(id=owner_id, email="replan-traveler@swena.local")
    other_user = AuthenticatedUser(id=other_owner_id, email="attacker@swena.local")

    active_user = auth_user

    async def _mock_get_current_user() -> AuthenticatedUser:
        return active_user

    async def _mock_get_session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_current_user] = _mock_get_current_user
    app.dependency_overrides[get_session] = _mock_get_session

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            now = datetime.now(UTC)

            # -------------------------------------------------------------
            # STEP 1: Submit Initial Route (v1) via POST /api/v1/optimization/routes
            # -------------------------------------------------------------
            dep_time = (now + timedelta(hours=1)).isoformat()
            stop1_id = uuid.uuid4()
            stop2_id = uuid.uuid4()
            stop3_id = uuid.uuid4()

            v1_payload = {
                "departure_at": dep_time,
                "stops": [
                    {
                        "id": str(stop1_id),
                        "name": "Victoria Memorial (Origin)",
                        "location": {"latitude": 22.5448, "longitude": 88.3426},
                        "stop_type": "ORIGIN",
                        "dwell_seconds": 0,
                    },
                    {
                        "id": str(stop2_id),
                        "name": "Indian Museum (Venue)",
                        "location": {"latitude": 22.5579, "longitude": 88.3511},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                    },
                    {
                        "id": str(stop3_id),
                        "name": "Howrah Station (Destination)",
                        "location": {"latitude": 22.5850, "longitude": 88.3420},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 900,
                    },
                ],
                "transport_mode": "car_petrol",
                "time_limit_seconds": 10,
            }

            resp_v1 = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": f"v1-key-{uuid.uuid4()}"},
                json=v1_payload,
            )
            assert resp_v1.status_code == 202, (
                f"Expected 202, got {resp_v1.status_code}: {resp_v1.text}"
            )
            v1_job_id = uuid.UUID(resp_v1.json()["job_id"])

            # Drive v1 through GraphRunner -> Worker -> Reconciliation -> Resume
            async with session_factory() as session:
                outbox_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == v1_job_id,
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
                execution_id=v1_job_id,
                event_id=start_outbox.id,
                workflow="ROUTE_OPTIMIZER_GRAPH",
                context_version=1,
                payload=start_payload,
            )
            start_res = await runner.handle_execution_start(event=start_event)
            assert start_res.status == "WAITING_TASKS"

            async with session_factory() as session:
                dispatch_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == v1_job_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
                dispatch_outbox = (await session.execute(dispatch_stmt)).scalar_one()
                dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

            injected_valhalla = MockValhallaClient()
            injected_closures = ClosureAvoidanceService()
            await process_record(
                dispatch_payload,
                session_factory,
                valhalla_client=injected_valhalla,
                closure_service=injected_closures,
            )

            async with session_factory() as session:
                receipt_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == v1_job_id,
                    OutboxEventModel.kind == "TASK_COMPLETED",
                )
                receipt_outbox = (await session.execute(receipt_stmt)).scalar_one()
                receipt_payload = receipt_outbox.payload_ref or receipt_outbox.payload

            reconciler = CompletionReconciliationService(
                session_factory,
                workflow_graphs={"ROUTE_OPTIMIZER_GRAPH": route_graph},
            )
            comp_event = TaskCompletionEvent(
                event_id=receipt_outbox.id,
                task_id=receipt_outbox.task_id,
                execution_id=v1_job_id,
                attempt=1,
                event_type="TASK_COMPLETED",
                result_id=receipt_payload.get("result_id"),
                output=receipt_payload.get("output"),
            )
            reconcile_res = await reconciler.reconcile_task_event(event=comp_event)
            assert reconcile_res.status == "APPLIED"
            assert reconcile_res.graph_resumed is True

            async with session_factory() as session:
                resume_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == v1_job_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
                resume_outbox = (await session.execute(resume_stmt)).scalar_one()

            resume_event = ExecutionResumeEvent(
                execution_id=v1_job_id,
                event_id=resume_outbox.id,
                triggered_by_task_id=resume_outbox.task_id,
                context_version=1,
            )
            resume_res = await runner.handle_execution_resume(event=resume_event)
            assert resume_res.status == "SUCCEEDED"

            # Check job status for v1
            job_v1_resp = await client.get(f"/api/v1/jobs/{v1_job_id}")
            assert job_v1_resp.status_code == 200
            assert job_v1_resp.json()["status"] == "SUCCEEDED"
            v1_route_id = uuid.UUID(job_v1_resp.json()["result_ref"]["route_id"])

            # Verify initial route (v1) in database
            async with session_factory() as session:
                repo = RouteRepository(session)
                v1_route = await repo.get_route_by_id(v1_route_id, owner_id=owner_id)
                assert v1_route is not None
                assert v1_route.route_version == 1
                assert v1_route.parent_route_id is None

            # -------------------------------------------------------------
            # STEP 2: Optimistic Concurrency Rejection (expected_previous_version mismatch)
            # -------------------------------------------------------------
            bad_replan_payload = {
                "current_location": {"latitude": 22.5500, "longitude": 88.3450},
                "replan_at": (now + timedelta(hours=1, minutes=15)).isoformat(),
                "completed_stop_ids": [str(stop1_id)],
                "expected_previous_version": 999,  # Intentional conflict
                "time_limit_seconds": 10,
            }
            conflict_resp = await client.post(
                f"/api/v1/optimization/routes/{v1_route_id}/replans",
                json=bad_replan_payload,
            )
            assert conflict_resp.status_code == 409
            assert "Route version mismatch" in conflict_resp.json()["detail"]

            # -------------------------------------------------------------
            # STEP 3: Submit Valid Mid-Trip Replan (v2) via POST /{route_id}/replans
            # -------------------------------------------------------------
            replan_origin_coords = {"latitude": 22.5510, "longitude": 88.3460}
            replan_at_iso = (now + timedelta(hours=1, minutes=15)).isoformat()
            replan_payload = {
                "current_location": replan_origin_coords,
                "replan_at": replan_at_iso,
                "completed_stop_ids": [str(stop1_id)],
                "expected_previous_version": 1,  # Matches v1 version
                "time_limit_seconds": 10,
            }

            resp_replan = await client.post(
                f"/api/v1/optimization/routes/{v1_route_id}/replans",
                headers={"Idempotency-Key": f"replan-key-{uuid.uuid4()}"},
                json=replan_payload,
            )
            assert resp_replan.status_code == 202
            replan_job_id = uuid.UUID(resp_replan.json()["job_id"])

            # -------------------------------------------------------------
            # STEP 4: Drive Replan Execution (GraphRunner -> Worker -> Reconcile -> Resume)
            # -------------------------------------------------------------
            async with session_factory() as session:
                replan_outbox_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == replan_job_id,
                    OutboxEventModel.kind == "EXECUTION_START",
                )
                replan_start_outbox = (await session.execute(replan_outbox_stmt)).scalar_one()
                replan_start_payload = (
                    replan_start_outbox.payload_ref or replan_start_outbox.payload
                )

            replan_start_event = ExecutionStartEvent(
                execution_id=replan_job_id,
                event_id=replan_start_outbox.id,
                workflow="ROUTE_OPTIMIZER_GRAPH",
                context_version=1,
                payload=replan_start_payload,
            )
            replan_start_res = await runner.handle_execution_start(event=replan_start_event)
            assert replan_start_res.status == "WAITING_TASKS"

            async with session_factory() as session:
                # Confirm replan task was created with action="replan"
                task_stmt = select(TaskModel).where(TaskModel.execution_id == replan_job_id)
                replan_task = (await session.execute(task_stmt)).scalar_one()
                assert replan_task.worker == "OPTIMIZER"
                assert replan_task.action == "replan"

                replan_dispatch_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == replan_job_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
                replan_dispatch_outbox = (await session.execute(replan_dispatch_stmt)).scalar_one()
                replan_dispatch_payload = (
                    replan_dispatch_outbox.payload_ref or replan_dispatch_outbox.payload
                )

            # Worker executes replan with injected mock
            await process_record(
                replan_dispatch_payload,
                session_factory,
                valhalla_client=injected_valhalla,
                closure_service=injected_closures,
            )

            # Reconcile receipt and resume replan graph
            async with session_factory() as session:
                replan_receipt_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == replan_job_id,
                    OutboxEventModel.kind == "TASK_COMPLETED",
                )
                replan_receipt_outbox = (await session.execute(replan_receipt_stmt)).scalar_one()
                replan_receipt_payload = (
                    replan_receipt_outbox.payload_ref or replan_receipt_outbox.payload
                )

            comp_replan_event = TaskCompletionEvent(
                event_id=replan_receipt_outbox.id,
                task_id=replan_receipt_outbox.task_id,
                execution_id=replan_job_id,
                attempt=1,
                event_type="TASK_COMPLETED",
                result_id=replan_receipt_payload.get("result_id"),
                output=replan_receipt_payload.get("output"),
            )
            reconcile_replan_res = await reconciler.reconcile_task_event(event=comp_replan_event)
            assert reconcile_replan_res.status == "APPLIED"
            assert reconcile_replan_res.graph_resumed is True

            async with session_factory() as session:
                replan_resume_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == replan_job_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
                replan_resume_outbox = (await session.execute(replan_resume_stmt)).scalar_one()

            replan_resume_event = ExecutionResumeEvent(
                execution_id=replan_job_id,
                event_id=replan_resume_outbox.id,
                triggered_by_task_id=replan_resume_outbox.task_id,
                context_version=1,
            )
            replan_resume_res = await runner.handle_execution_resume(event=replan_resume_event)
            assert replan_resume_res.status == "SUCCEEDED"

            # -------------------------------------------------------------
            # STEP 5: Discovery of Replan Route via Job Status and by-job Endpoint
            # -------------------------------------------------------------
            job_replan_resp = await client.get(f"/api/v1/jobs/{replan_job_id}")
            assert job_replan_resp.status_code == 200
            assert job_replan_resp.json()["status"] == "SUCCEEDED"
            v2_route_id = uuid.UUID(job_replan_resp.json()["result_ref"]["route_id"])

            by_job_resp = await client.get(f"/api/v1/optimization/routes/by-job/{replan_job_id}")
            assert by_job_resp.status_code == 200
            v2_route_data = by_job_resp.json()
            assert v2_route_data["id"] == str(v2_route_id)
            assert v2_route_data["parent_route_id"] == str(v1_route_id)
            assert v2_route_data["route_version"] == 2

            # -------------------------------------------------------------
            # STEP 6: Verify Database Lineage State
            # -------------------------------------------------------------
            async with session_factory() as session:
                repo = RouteRepository(session)
                v2_route = await repo.get_route_by_id(v2_route_id, owner_id=owner_id)
                assert v2_route is not None
                assert v2_route.parent_route_id == v1_route_id
                assert v2_route.route_version == 2
                assert v2_route.execution_id == replan_job_id

            # -------------------------------------------------------------
            # STEP 7: Lineage & Version History Endpoint GET /{route_id}/versions
            # -------------------------------------------------------------
            versions_resp = await client.get(f"/api/v1/optimization/routes/{v2_route_id}/versions")
            assert versions_resp.status_code == 200
            lineage_data = versions_resp.json()

            assert lineage_data["root_route_id"] == str(v1_route_id)
            assert lineage_data["current_route_id"] == str(v2_route_id)
            versions = lineage_data["versions"]
            assert len(versions) == 2

            v1_summary = versions[0]
            assert v1_summary["route_id"] == str(v1_route_id)
            assert v1_summary["route_version"] == 1
            assert v1_summary["parent_route_id"] is None
            assert v1_summary["distance_delta_m"] is None
            assert v1_summary["duration_delta_seconds"] is None
            assert v1_summary["replan_origin"] is None

            v2_summary = versions[1]
            assert v2_summary["route_id"] == str(v2_route_id)
            assert v2_summary["route_version"] == 2
            assert v2_summary["parent_route_id"] == str(v1_route_id)
            assert v2_summary["distance_delta_m"] is not None
            assert v2_summary["duration_delta_seconds"] is not None
            assert v2_summary["replan_origin"] is not None
            assert (
                abs(v2_summary["replan_origin"]["latitude"] - replan_origin_coords["latitude"])
                < 1e-4
            )
            assert (
                abs(v2_summary["replan_origin"]["longitude"] - replan_origin_coords["longitude"])
                < 1e-4
            )

            # Querying versions from v1 also returns the complete lineage
            versions_v1_resp = await client.get(
                f"/api/v1/optimization/routes/{v1_route_id}/versions"
            )
            assert versions_v1_resp.status_code == 200
            assert len(versions_v1_resp.json()["versions"]) == 2

            # -------------------------------------------------------------
            # STEP 8: Strict Tenant Isolation Fencing (AC-9)
            # -------------------------------------------------------------
            active_user = other_user  # Switch to unauthorized tenant

            # User B querying versions of User A's route -> 403 Forbidden
            other_versions_resp = await client.get(
                f"/api/v1/optimization/routes/{v2_route_id}/versions"
            )
            assert other_versions_resp.status_code == 403
            assert "belongs to another tenant" in other_versions_resp.json()["detail"]

            # User B submitting replan against User A's route -> 403 Forbidden
            other_replan_resp = await client.post(
                f"/api/v1/optimization/routes/{v2_route_id}/replans",
                json=replan_payload,
            )
            assert other_replan_resp.status_code == 403
            assert "belongs to another tenant" in other_replan_resp.json()["detail"]

            # Non-existent route queries -> 404 Not Found
            random_id = uuid.uuid4()
            not_found_versions = await client.get(
                f"/api/v1/optimization/routes/{random_id}/versions"
            )
            assert not_found_versions.status_code == 404

            not_found_replan = await client.post(
                f"/api/v1/optimization/routes/{random_id}/replans",
                json=replan_payload,
            )
            assert not_found_replan.status_code == 404

    finally:
        app.dependency_overrides.clear()
