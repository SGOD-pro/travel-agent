"""Integration tests for public job surfaces conforming to spec 0001:

1. job_events table: migration, model, repository, and actual event recording.
2. GET /api/v1/jobs/{job_id}: owner-scoped status and timeline polling with pagination.
3. POST /api/v1/jobs/{job_id}/cancel: owner-scoped cancellation with Transaction 8 fencing.
4. Concurrency and racing between cancellation, worker completion, and reconciliation.
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
from functions.api.models import TripModel
from functions.orchestrator.execution import (
    CompletionReconciliationService,
    ExecutionModel,
    OutboxEventModel,
    ParentCancelledError,
    TaskCompletionEvent,
    TaskModel,
    WorkerFencingService,
)
from functions.orchestrator.execution.cancellation import cancel_execution
from functions.orchestrator.execution.job_events import get_job_events, record_job_event
from middleware.auth import AuthenticatedUser, get_current_user

# Resilient DNS lookup for WSL/Aiven forwarder stability
_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
    try:
        res = _orig_getaddrinfo(host, port, *args, **kwargs)
        _dns_cache[key] = res
        return res
    except socket.gaierror:
        if key in _dns_cache:
            return _dns_cache[key]
        raise


socket.getaddrinfo = _resilient_getaddrinfo


@pytest.fixture
async def session_factory():
    url = settings.DATABASE_URL
    connect_args = {
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
async def test_acceptance_status_url_resolves_successfully(session_factory) -> None:
    """Verifies that acceptance returns status_url and polling that url returns job status with timeline."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Seed TripModel for the owner
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

    current_authenticated_user = AuthenticatedUser(
        id=owner_id,
        email="owner@test.internal",
    )

    async def override_get_current_user():
        return current_authenticated_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            idemp_key = f"key-{uuid.uuid4()}"
            plan_body = {"planning_options": {"mode": "fast"}, "workflow": "TRAVEL"}

            # 1. Start trip plan -> 202 Accepted with status_url
            accept_resp = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers={"Idempotency-Key": idemp_key},
                json=plan_body,
            )
            assert accept_resp.status_code == 202
            accept_data = accept_resp.json()
            job_id_str = accept_data["job_id"]
            status_url = accept_data["status_url"]
            assert status_url == f"/api/v1/jobs/{job_id_str}"

            # 2. Poll the status_url
            status_resp = await client.get(status_url)
            assert status_resp.status_code == 200
            status_data = status_resp.json()

            assert status_data["job_id"] == job_id_str
            assert status_data["status"] == "CREATED"
            assert status_data["context_version"] == 1
            assert status_data["retry_after_seconds"] == 5
            assert status_data["result_available"] is False
            assert "progress" in status_data
            assert status_data["progress"]["workflow"] == "TRAVEL"

            # 3. Verify timeline contains the initial JOB_ACCEPTED event
            timeline = status_data["timeline"]
            assert len(timeline) >= 1
            assert timeline[0]["sequence"] == 1
            assert timeline[0]["event_type"] == "JOB_ACCEPTED"
            assert timeline[0]["safe_payload"]["status"] == "CREATED"

            # 4. Also poll dedicated /events endpoint
            events_resp = await client.get(f"{status_url}/events")
            assert events_resp.status_code == 200
            events_data = events_resp.json()
            assert len(events_data["events"]) >= 1
            assert events_data["events"][0]["event_type"] == "JOB_ACCEPTED"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_isolation_status_timeline_and_cancellation(session_factory) -> None:
    """Verifies that non-owners receive 404 for status polling, timeline, and cancellation."""
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    job_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Seed execution owned by Owner A
    async with session_factory() as session:
        execution = ExecutionModel(
            id=job_id,
            owner_id=owner_a,
            trip_id=None,
            workflow="TRAVEL",
            status="RUNNING",
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(execution)
        await session.commit()

    async def override_get_session():
        async with session_factory() as session:
            yield session

    # Current user is Owner B!
    user_b = AuthenticatedUser(id=owner_b, email="b@test.internal")

    async def override_get_current_user():
        return user_b

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Owner B polls Owner A's job -> 404
            resp_status = await client.get(f"/api/v1/jobs/{job_id}")
            assert resp_status.status_code == 404
            assert f"Job {job_id} not found" in resp_status.json()["detail"]

            # 2. Owner B requests Owner A's timeline events -> 404
            resp_events = await client.get(f"/api/v1/jobs/{job_id}/events")
            assert resp_events.status_code == 404
            assert f"Job {job_id} not found" in resp_events.json()["detail"]

            # 3. Owner B attempts to cancel Owner A's job -> 404
            resp_cancel = await client.post(f"/api/v1/jobs/{job_id}/cancel")
            assert resp_cancel.status_code == 404
            assert f"Job {job_id} not found" in resp_cancel.json()["detail"]

            # 4. Unknown job ID -> 404
            random_id = uuid.uuid4()
            resp_unknown = await client.get(f"/api/v1/jobs/{random_id}")
            assert resp_unknown.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_timeline_ordering_pagination_and_rollback_consistency(session_factory) -> None:
    """Verifies monotonic sequence ordering, cursor pagination, and rollback consistency."""
    owner_id = uuid.uuid4()
    job_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as session:
        execution = ExecutionModel(
            id=job_id,
            owner_id=owner_id,
            trip_id=None,
            workflow="TRAVEL",
            status="RUNNING",
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(execution)
        await session.flush()

        # Record multiple events
        await record_job_event(session, execution_id=job_id, event_type="JOB_ACCEPTED", safe_payload={"step": 1})
        await record_job_event(session, execution_id=job_id, event_type="TASK_DISPATCHED", safe_payload={"step": 2})
        await record_job_event(session, execution_id=job_id, event_type="TASK_COMPLETED", safe_payload={"step": 3})
        await record_job_event(session, execution_id=job_id, event_type="STEP_COMPLETED", safe_payload={"step": 4})
        await record_job_event(session, execution_id=job_id, event_type="GRAPH_RESUMED", safe_payload={"step": 5})
        await session.commit()

    # 1. Test pagination directly via repository
    async with session_factory() as session:
        # Page 1: limit 2
        p1_events, p1_cursor = await get_job_events(session, execution_id=job_id, limit=2)
        assert len(p1_events) == 2
        assert p1_events[0].sequence == 1
        assert p1_events[1].sequence == 2
        assert p1_cursor == 2

        # Page 2: cursor 2, limit 2
        p2_events, p2_cursor = await get_job_events(session, execution_id=job_id, cursor=p1_cursor, limit=2)
        assert len(p2_events) == 2
        assert p2_events[0].sequence == 3
        assert p2_events[1].sequence == 4
        assert p2_cursor == 4

        # Page 3: cursor 4, limit 2
        p3_events, p3_cursor = await get_job_events(session, execution_id=job_id, cursor=p2_cursor, limit=2)
        assert len(p3_events) == 1
        assert p3_events[0].sequence == 5
        assert p3_cursor is None

    # 2. Test rollback consistency: an aborted transaction leaves zero phantom events
    async with session_factory() as session:
        try:
            async with session.begin():
                await record_job_event(
                    session, execution_id=job_id, event_type="PHANTOM_EVENT", safe_payload={"failed": True}
                )
                raise RuntimeError("Simulated transaction rollback")
        except RuntimeError:
            pass

    async with session_factory() as session:
        all_events, _ = await get_job_events(session, execution_id=job_id, limit=100)
        assert len(all_events) == 5
        assert all(e.event_type != "PHANTOM_EVENT" for e in all_events)


@pytest.mark.asyncio
async def test_repeated_cancellation_and_terminal_job_behavior(session_factory) -> None:
    """Verifies Transaction 8 fenced cancellation, idempotency on repeated calls, and terminal job preservation."""
    owner_id = uuid.uuid4()
    job_id = uuid.uuid4()
    now = datetime.now(UTC)

    # 1. Create active execution in WAITING_TASKS
    async with session_factory() as session:
        execution = ExecutionModel(
            id=job_id,
            owner_id=owner_id,
            trip_id=None,
            workflow="TRAVEL",
            status="WAITING_TASKS",
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(execution)
        await session.commit()

    async def override_get_session():
        async with session_factory() as session:
            yield session

    current_user = AuthenticatedUser(id=owner_id, email="owner@test.internal")

    async def override_get_current_user():
        return current_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First cancellation
            resp_1 = await client.post(
                f"/api/v1/jobs/{job_id}/cancel",
                json={"reason": "User changed plans"},
            )
            assert resp_1.status_code == 200
            data_1 = resp_1.json()
            assert data_1["job_id"] == str(job_id)
            assert data_1["status"] == "CANCELLED"
            assert data_1["cancelled"] is True

            # Verify PostgreSQL state: cancelled_at set, job_events has JOB_CANCELLED, outbox has EXECUTION_CANCELLED
            async with session_factory() as session:
                ex = (
                    await session.execute(select(ExecutionModel).where(ExecutionModel.id == job_id))
                ).scalar_one()
                assert ex.status == "CANCELLED"
                assert ex.cancelled_at is not None

                outbox = (
                    await session.execute(
                        select(OutboxEventModel).where(
                            OutboxEventModel.execution_id == job_id,
                            OutboxEventModel.kind == "EXECUTION_CANCELLED",
                        )
                    )
                ).scalar_one()
                assert outbox.payload_ref["reason"] == "User changed plans"

                events, _ = await get_job_events(session, execution_id=job_id)
                cancel_event = [e for e in events if e.event_type == "JOB_CANCELLED"]
                assert len(cancel_event) == 1
                assert cancel_event[0].safe_payload["reason"] == "User changed plans"

            # Repeated cancellation on already CANCELLED job: retains terminal state, cancelled == False
            resp_2 = await client.post(f"/api/v1/jobs/{job_id}/cancel")
            assert resp_2.status_code == 200
            data_2 = resp_2.json()
            assert data_2["status"] == "CANCELLED"
            assert data_2["cancelled"] is False

            # Verify on already SUCCEEDED job: retains terminal state
            succeeded_job_id = uuid.uuid4()
            async with session_factory() as session:
                succ_exec = ExecutionModel(
                    id=succeeded_job_id,
                    owner_id=owner_id,
                    trip_id=None,
                    workflow="TRAVEL",
                    status="SUCCEEDED",
                    context_version=1,
                    deadline_at=now + timedelta(hours=1),
                    created_at=now,
                    updated_at=now,
                )
                session.add(succ_exec)
                await session.commit()

            resp_3 = await client.post(f"/api/v1/jobs/{succeeded_job_id}/cancel")
            assert resp_3.status_code == 200
            data_3 = resp_3.json()
            assert data_3["status"] == "SUCCEEDED"
            assert data_3["cancelled"] is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cancellation_racing_worker_completion_and_reconciliation(session_factory) -> None:
    """Verifies that cancellation serializes with worker completion and blocks late reconciliation."""
    owner_id = uuid.uuid4()
    job_id = uuid.uuid4()
    task_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    now = datetime.now(UTC)

    # Seed execution with running task
    async with session_factory() as session:
        execution = ExecutionModel(
            id=job_id,
            owner_id=owner_id,
            trip_id=None,
            workflow="TRAVEL",
            status="WAITING_TASKS",
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(execution)

        task = TaskModel(
            id=task_id,
            execution_id=job_id,
            parent_task_id=None,
            worker="EVIDENCE",
            action="collect",
            payload_ref={"location": "Delhi"},
            input_hash="hash_abc",
            idempotency_key=f"task-idem-{task_id}",
            attempt=1,
            max_attempts=3,
            context_version=1,
            status="RUNNING",
            lease_token=lease_token,
            lease_expiry=now + timedelta(minutes=5),
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.commit()

    # 1. Execute cancellation
    async with session_factory() as session:
        ex, was_cancelled = await cancel_execution(
            session, execution_id=job_id, owner_id=owner_id, reason="User cancelled"
        )
        assert was_cancelled is True
        await session.commit()

    # 2. Worker attempts to complete task after execution was cancelled
    async with session_factory() as session:
        worker_svc = WorkerFencingService(session)
        with pytest.raises(ParentCancelledError) as exc_info:
            await worker_svc.complete_task(
                execution_id=job_id,
                task_id=task_id,
                lease_token=lease_token,
                output={"hotels": []},
            )
        assert "cancelled" in str(exc_info.value).lower()

    # 3. Late reconciliation event arriving for cancelled execution triggers cancellation barrier
    reconciler = CompletionReconciliationService(session_factory)
    event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_id,
        execution_id=job_id,
        attempt=1,
        event_type="TASK_COMPLETED",
    )
    result = await reconciler.reconcile_task_event(event=event)
    assert result.status == "CANCELLED_BARRIER"
    assert result.checkpoint_updated is False
    assert result.graph_resumed is False
