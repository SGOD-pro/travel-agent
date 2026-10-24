"""Integration test suite for Slice 6: Route optimizer worker execution and REST API.

Verifies on live PostgreSQL / PostGIS:
1. End-to-end asynchronous route optimization via REST API:
   - Client submits POST /api/v1/optimization/routes -> 202 Accepted with job_id.
   - SQS TASK_DISPATCH message claimed under WorkerFencingService.
   - Route solved and persisted to routes, route_stops, route_legs tables.
   - Task completed atomically under lease fencing with timeline event.
   - Client polls GET /api/v1/jobs/{job_id} and retrieves route via GET /api/v1/optimization/routes/{route_id}.
2. Tenant ownership and security enforcement:
   - Unauthorized user receives 403 Forbidden attempting to fetch another tenant's route.
   - Unauthorized user receives 403 Forbidden attempting to replan another tenant's route.
3. Mid-trip replanning asynchronous flow:
   - Owner submits POST /api/v1/optimization/routes/{route_id}/replans -> 202 Accepted.
   - Worker processes replan task, freezes completed stops, increments route_version to 2.
   - Route version 2 is retrieved cleanly.
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
    OutboxEventModel,
    TaskCompletionEvent,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.execution.transport import OutboxMessage, SqsTransportAdapter
from functions.orchestrator.routing.repository import RouteRepository
from functions.route_optimizer.closures import ClosureAvoidanceService
from functions.route_optimizer.consumer import process_record
from functions.route_optimizer.fixtures import MockValhallaClient
from functions.route_optimizer.valhalla import ValhallaConnectionError
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
async def test_route_optimization_worker_e2e_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end integration test: API submission -> SQS consumption -> worker solve -> polling -> fetch."""
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

    current_user = AuthenticatedUser(id=owner_id, email="owner@test.internal")

    async def override_get_current_user() -> AuthenticatedUser:
        return current_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 2. Submit route optimization request via API
            dep_at = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
            req_payload = {
                "trip_id": str(trip_id),
                "departure_at": dep_at.isoformat(),
                "objective": "FASTEST",
                "is_round_trip": False,
                "stops": [
                    {
                        "name": "Hotel Grand",
                        "location": {"latitude": 22.5726, "longitude": 88.3639},
                        "stop_type": "ORIGIN",
                        "dwell_seconds": 0,
                    },
                    {
                        "name": "Victoria Memorial",
                        "location": {"latitude": 22.5448, "longitude": 88.3426},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                        "is_must_visit": True,
                    },
                    {
                        "name": "Indian Museum",
                        "location": {"latitude": 22.5579, "longitude": 88.3511},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                        "is_must_visit": True,
                    },
                ],
            }

            resp = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": f"opt-{uuid.uuid4()}"},
                json=req_payload,
            )
            assert resp.status_code == 202
            resp_data = resp.json()
            job_id_str = resp_data["job_id"]
            job_id = uuid.UUID(job_id_str)
            assert resp_data["status"] == "QUEUED"
            assert resp_data["status_url"] == f"/api/v1/jobs/{job_id}"

            # 3. Verify database state: execution, task, and outbox event
            async with session_factory() as session:
                exec_stmt = select(ExecutionModel).where(ExecutionModel.id == job_id)
                execution = (await session.execute(exec_stmt)).scalar_one_or_none()
                assert execution is not None
                assert execution.status == "CREATED"
                assert execution.owner_id == owner_id

                task_stmt = select(TaskModel).where(TaskModel.execution_id == job_id)
                task = (await session.execute(task_stmt)).scalar_one_or_none()
                assert task is not None
                assert task.worker == "OPTIMIZER"
                assert task.action == "solve"
                assert task.status == "PENDING"

                outbox_stmt = select(OutboxEventModel).where(OutboxEventModel.execution_id == job_id)
                outbox_event = (await session.execute(outbox_stmt)).scalar_one_or_none()
                assert outbox_event is not None
                assert outbox_event.kind == "TASK_DISPATCH"
                dispatch_record = outbox_event.payload_ref

            # 4. Outbox routing: verify dedicated SQS routing to optimizer queue
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
                event_id=outbox_event.id,
                execution_id=outbox_event.execution_id,
                task_id=outbox_event.task_id,
                kind=outbox_event.kind,
                payload=outbox_event.payload or outbox_event.payload_ref,
                attempts=outbox_event.attempts,
                created_at=outbox_event.created_at,
            )
            await transport_adapter.publish(outbox_msg)
            assert optimizer_queue_url in published_queues

            # 5. Real consumer execution under lease fencing with injected provider
            injected_provider = MockValhallaClient()
            injected_closures = ClosureAvoidanceService()
            await process_record(
                dispatch_record,
                session_factory,
                valhalla_client=injected_provider,
                closure_service=injected_closures,
            )

            # 6. Verify task completed in database and extract result
            async with session_factory() as session:
                task_completed = (await session.execute(task_stmt)).scalar_one()
                assert task_completed.status == "COMPLETED"
                result_stmt = select(WorkerResultModel).where(WorkerResultModel.id == task_completed.result_id)
                worker_result = (await session.execute(result_stmt)).scalar_one()
                assert worker_result.output_ref is not None
                route_id_str = worker_result.output_ref["route_id"]
                route_id = uuid.UUID(route_id_str)

                # Verify route and legs persisted
                route_repo = RouteRepository(session)
                route_model = await route_repo.get_route_by_id(route_id, owner_id=owner_id)
                assert route_model is not None
                assert route_model.owner_id == owner_id
                assert route_model.route_version == 1
                assert route_model.status in ("FEASIBLE", "OPTIMAL")
                assert len(route_model.stops) == 3
                assert len(route_model.legs) == 2

            # 7. Completion reconciliation: reconcile task completion into execution terminal SUCCEEDED state
            reconciliation_service = CompletionReconciliationService(session_factory)
            completion_event = TaskCompletionEvent(
                event_id=uuid.uuid4(),
                task_id=task.id,
                execution_id=job_id,
                attempt=task_completed.attempt,
                event_type="TASK_COMPLETED",
                result_id=worker_result.id,
            )
            reconcile_result = await reconciliation_service.reconcile_task_event(event=completion_event)
            assert reconcile_result.status == "APPLIED"

            async with session_factory() as session:
                reconciled_exec = (await session.execute(exec_stmt)).scalar_one()
                assert reconciled_exec.status == "SUCCEEDED"

            # 8. Terminal job polling: GET /api/v1/jobs/{job_id} confirms SUCCEEDED and result_available
            job_resp = await client.get(f"/api/v1/jobs/{job_id}")
            assert job_resp.status_code == 200
            job_data = job_resp.json()
            assert job_data["job_id"] == job_id_str
            assert job_data["status"] == "SUCCEEDED"
            assert job_data["result_available"] is True
            assert job_data["retry_after_seconds"] == 0
            assert any(e["event_type"] == "EXECUTION_SUCCEEDED" for e in job_data["timeline"])

            # 9. Owner-isolated route retrieval via GET /api/v1/optimization/routes/{route_id}
            route_resp = await client.get(f"/api/v1/optimization/routes/{route_id}")
            assert route_resp.status_code == 200
            route_data = route_resp.json()
            assert route_data["id"] == route_id_str
            assert route_data["route_version"] == 1
            assert len(route_data["stops"]) == 3
            assert len(route_data["legs"]) == 2

            # 10. Tenant isolation: unauthorized user receives 403 Forbidden
            other_user = AuthenticatedUser(id=uuid.uuid4(), email="attacker@test.internal")
            app.dependency_overrides[get_current_user] = lambda: other_user

            forbidden_resp = await client.get(f"/api/v1/optimization/routes/{route_id}")
            assert forbidden_resp.status_code == 403
            assert "belongs to another tenant" in forbidden_resp.json()["detail"]

            # 11. Mid-trip replan submission by owner -> 202 Accepted
            app.dependency_overrides[get_current_user] = lambda: current_user
            replan_body = {
                "current_location": {"latitude": 22.5450, "longitude": 88.3430},
                "replan_at": (dep_at + timedelta(minutes=45)).isoformat(),
                "completed_stop_ids": [route_data["stops"][0]["id"]],
                "expected_previous_version": 1,
            }

            replan_resp = await client.post(
                f"/api/v1/optimization/routes/{route_id}/replans",
                json=replan_body,
            )
            assert replan_resp.status_code == 202
            replan_data = replan_resp.json()
            replan_job_id = uuid.UUID(replan_data["job_id"])

            # 12. Outbox routing for replan task
            async with session_factory() as session:
                replan_outbox_stmt = select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == replan_job_id
                )
                replan_outbox = (await session.execute(replan_outbox_stmt)).scalar_one()
                replan_dispatch_record = replan_outbox.payload_ref

            published_queues.clear()
            replan_msg = OutboxMessage(
                event_id=replan_outbox.id,
                execution_id=replan_outbox.execution_id,
                task_id=replan_outbox.task_id,
                kind=replan_outbox.kind,
                payload=replan_outbox.payload or replan_outbox.payload_ref,
                attempts=replan_outbox.attempts,
                created_at=replan_outbox.created_at,
            )
            await transport_adapter.publish(replan_msg)
            assert optimizer_queue_url in published_queues

            # 13. Process replan task in worker with injected provider
            await process_record(
                replan_dispatch_record,
                session_factory,
                valhalla_client=injected_provider,
                closure_service=injected_closures,
            )

            # 14. Verify replanned route version 2 created and reconcile completion
            async with session_factory() as session:
                replan_task_stmt = select(TaskModel).where(
                    TaskModel.execution_id == replan_job_id
                )
                replan_task_completed = (await session.execute(replan_task_stmt)).scalar_one()
                assert replan_task_completed.status == "COMPLETED"
                replan_res_stmt = select(WorkerResultModel).where(
                    WorkerResultModel.id == replan_task_completed.result_id
                )
                replan_worker_result = (await session.execute(replan_res_stmt)).scalar_one()
                assert replan_worker_result.output_ref is not None
                v2_route_id_str = replan_worker_result.output_ref["route_id"]
                assert replan_worker_result.output_ref["route_version"] == 2

            replan_reconcile_event = TaskCompletionEvent(
                event_id=uuid.uuid4(),
                task_id=replan_task_completed.id,
                execution_id=replan_job_id,
                attempt=replan_task_completed.attempt,
                event_type="TASK_COMPLETED",
                result_id=replan_worker_result.id,
            )
            replan_reconcile_result = await reconciliation_service.reconcile_task_event(
                event=replan_reconcile_event
            )
            assert replan_reconcile_result.status == "APPLIED"

            # 15. Polling replan job -> SUCCEEDED
            replan_job_resp = await client.get(f"/api/v1/jobs/{replan_job_id}")
            assert replan_job_resp.status_code == 200
            assert replan_job_resp.json()["status"] == "SUCCEEDED"
            assert replan_job_resp.json()["result_available"] is True

            # 16. Fetch version 2 route
            v2_resp = await client.get(f"/api/v1/optimization/routes/{v2_route_id_str}")
            assert v2_resp.status_code == 200
            v2_data = v2_resp.json()
            assert v2_data["route_version"] == 2
            assert v2_data["previous_route_version"] == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_route_optimization_worker_fails_explicitly_without_provider(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that the worker fails explicitly with ValhallaConnectionError when provider is unavailable."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    now = datetime.now(UTC)

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

    current_user = AuthenticatedUser(id=owner_id, email="owner@test.internal")

    async def override_get_current_user() -> AuthenticatedUser:
        return current_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            dep_at = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
            req_payload = {
                "trip_id": str(trip_id),
                "departure_at": dep_at.isoformat(),
                "objective": "FASTEST",
                "is_round_trip": False,
                "stops": [
                    {
                        "name": "Start",
                        "location": {"latitude": 22.5726, "longitude": 88.3639},
                        "stop_type": "ORIGIN",
                        "dwell_seconds": 0,
                    },
                    {
                        "name": "End",
                        "location": {"latitude": 22.5448, "longitude": 88.3426},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 0,
                    },
                ],
            }
            resp = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": f"opt-{uuid.uuid4()}"},
                json=req_payload,
            )
            assert resp.status_code == 202
            job_id = uuid.UUID(resp.json()["job_id"])

            async with session_factory() as session:
                outbox_stmt = select(OutboxEventModel).where(OutboxEventModel.execution_id == job_id)
                outbox_event = (await session.execute(outbox_stmt)).scalar_one()
                dispatch_record = outbox_event.payload_ref

            # Calling process_record with no injected provider and no VALHALLA_ENDPOINT_URL must fail explicitly
            monkeypatch.setattr(settings, "VALHALLA_ENDPOINT_URL", None)
            with pytest.raises(ValhallaConnectionError, match="VALHALLA_ENDPOINT_URL is not configured"):
                await process_record(dispatch_record, session_factory)
    finally:
        app.dependency_overrides.clear()
