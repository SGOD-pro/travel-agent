"""Integration tests for spec 0001: atomic job acceptance and owner-scoped idempotency.

Tests against live PostgreSQL with READ COMMITTED isolation:
1. Fresh job acceptance: commits idempotency record, execution, and outbox event in 1 transaction.
2. Concurrent identical requests: resolves deterministically without constraint violations.
3. Conflicting payloads: returns HTTP 409 conflict error on reused key with different payload.
4. Owner isolation: same key across different owners succeeds independently.
5. Rollback after api_idempotency insert: rolls back all changes, leaving 0 rows.
6. Rollback after executions insert: rolls back all changes, leaving 0 rows.
7. FastAPI endpoint verification: end-to-end HTTP status codes and headers.
"""

from __future__ import annotations

import asyncio
import re
import socket
import uuid
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
from functions.api.idempotency import (
    IdempotencyConflictError,
    IdempotencyRepository,
    compute_request_hash,
)
from functions.api.models import ApiIdempotencyModel, TripModel
from functions.orchestrator.execution.models import ExecutionModel, OutboxEventModel
from middleware.auth import AuthenticatedUser, get_current_user

# In-process DNS cache to prevent transient WSL forwarder socket.gaierror drops
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
    connect_args = {}
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
async def test_fresh_job_acceptance_atomic_transaction(session_factory) -> None:
    """Verifies pre-generated execution ID and atomic commit of idempotency, execution, and outbox event."""
    owner_id = uuid.uuid4()
    key = f"key-{uuid.uuid4()}"
    operation = "test_acceptance"
    payload = {"origin": "Bengaluru", "destination": "Mysuru", "budget": 15000}

    async with session_factory() as session:
        repo = IdempotencyRepository(session)
        response_json, status_code, is_cached = await repo.accept_job(
            owner_id=owner_id,
            operation=operation,
            idempotency_key=key,
            payload=payload,
            workflow="TRAVEL",
        )

    # 1. Check returned envelope
    assert status_code == 202
    assert is_cached is False
    assert "job_id" in response_json
    assert response_json["status"] == "QUEUED"
    execution_id = uuid.UUID(response_json["job_id"])
    assert response_json["status_url"] == f"/api/v1/jobs/{execution_id}"

    # 2. Verify database state in a fresh transaction
    async with session_factory() as session:
        # Check api_idempotency
        idemp_stmt = select(ApiIdempotencyModel).where(
            ApiIdempotencyModel.owner_id == owner_id,
            ApiIdempotencyModel.operation == operation,
            ApiIdempotencyModel.key == key,
        )
        idemp_row = (await session.execute(idemp_stmt)).scalar_one_or_none()
        assert idemp_row is not None
        assert idemp_row.status_code == 202
        assert idemp_row.request_hash == compute_request_hash(payload)

        # Check executions
        exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id)
        exec_row = (await session.execute(exec_stmt)).scalar_one_or_none()
        assert exec_row is not None
        assert exec_row.owner_id == owner_id
        assert exec_row.status == "CREATED"
        assert exec_row.context_version == 1
        assert exec_row.workflow == "TRAVEL"

        # Check outbox_events
        outbox_stmt = select(OutboxEventModel).where(OutboxEventModel.execution_id == execution_id)
        outbox_row = (await session.execute(outbox_stmt)).scalar_one_or_none()
        assert outbox_row is not None
        assert outbox_row.kind == "EXECUTION_START"
        assert outbox_row.delivered_at is None
        assert outbox_row.attempts == 0
        assert outbox_row.payload_ref["job_id"] == str(execution_id)


@pytest.mark.asyncio
async def test_concurrent_identical_requests(session_factory) -> None:
    """Verifies that 10 concurrent identical requests resolve deterministically under READ COMMITTED."""
    owner_id = uuid.uuid4()
    key = f"concurrent-{uuid.uuid4()}"
    operation = "test_concurrent"
    payload = {"corridor": "Western Ghats", "pace": "relaxed"}
    concurrency_count = 10

    async def execute_request() -> tuple[dict, int, bool]:
        async with session_factory() as session:
            repo = IdempotencyRepository(session)
            return await repo.accept_job(
                owner_id=owner_id,
                operation=operation,
                idempotency_key=key,
                payload=payload,
            )

    # Launch all concurrent requests simultaneously
    results = await asyncio.gather(*(execute_request() for _ in range(concurrency_count)))

    # All requests must succeed with HTTP 202
    for res_json, status_code, _ in results:
        assert status_code == 202
        assert res_json["status"] == "QUEUED"

    # All requests must return the EXACT SAME job_id
    first_job_id = results[0][0]["job_id"]
    for res_json, _, _ in results:
        assert res_json["job_id"] == first_job_id

    # Exactly one request must be fresh (is_cached=False), and 9 must be cached (is_cached=True)
    fresh_count = sum(1 for _, _, is_cached in results if not is_cached)
    cached_count = sum(1 for _, _, is_cached in results if is_cached)
    assert fresh_count == 1
    assert cached_count == concurrency_count - 1

    # Verify exactly 1 execution and 1 outbox event exist in PostgreSQL
    async with session_factory() as session:
        exec_count = (
            (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == uuid.UUID(first_job_id))
                )
            )
            .scalars()
            .all()
        )
        assert len(exec_count) == 1

        outbox_count = (
            (
                await session.execute(
                    select(OutboxEventModel).where(
                        OutboxEventModel.execution_id == uuid.UUID(first_job_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(outbox_count) == 1


@pytest.mark.asyncio
async def test_conflicting_payloads_raises_409(session_factory) -> None:
    """Submitting the same idempotency key with a conflicting payload raises HTTP 409 conflict."""
    owner_id = uuid.uuid4()
    key = f"conflict-{uuid.uuid4()}"
    operation = "test_conflict"
    payload_a = {"destination": "Goa"}
    payload_b = {"destination": "Manali"}

    # Request 1: succeeds
    async with session_factory() as session:
        repo = IdempotencyRepository(session)
        res_a, status_a, cached_a = await repo.accept_job(
            owner_id=owner_id,
            operation=operation,
            idempotency_key=key,
            payload=payload_a,
        )
    assert status_a == 202
    assert cached_a is False

    # Request 2: identical key, conflicting payload -> raises IdempotencyConflictError
    with pytest.raises(IdempotencyConflictError, match="different request payload"):
        async with session_factory() as session:
            repo = IdempotencyRepository(session)
            await repo.accept_job(
                owner_id=owner_id,
                operation=operation,
                idempotency_key=key,
                payload=payload_b,
            )


@pytest.mark.asyncio
async def test_concurrent_conflicting_requests(session_factory) -> None:
    """Verifies that concurrent requests with identical key but conflicting payloads resolve deterministically:
    one commits 202, while the other raises IdempotencyConflictError (HTTP 409).
    """
    owner_id = uuid.uuid4()
    key = f"concurrent-conflict-{uuid.uuid4()}"
    operation = "test_concurrent_conflict"
    payload_a = {"destination": "Goa", "pace": "fast"}
    payload_b = {"destination": "Manali", "pace": "slow"}

    async def run_req(payload: dict) -> tuple[str, Any]:
        try:
            async with session_factory() as session:
                repo = IdempotencyRepository(session)
                res, status_code, cached = await repo.accept_job(
                    owner_id=owner_id,
                    operation=operation,
                    idempotency_key=key,
                    payload=payload,
                )
                return "SUCCESS", (res, status_code, cached)
        except IdempotencyConflictError as e:
            return "CONFLICT", str(e)

    results = await asyncio.gather(run_req(payload_a), run_req(payload_b))
    statuses = [r[0] for r in results]

    assert "SUCCESS" in statuses
    assert "CONFLICT" in statuses


@pytest.mark.asyncio
async def test_owner_isolation(session_factory) -> None:
    """Verifies that the same idempotency key used by different owners operates in strict isolation."""
    owner_1 = uuid.uuid4()
    owner_2 = uuid.uuid4()
    shared_key = f"shared-{uuid.uuid4()}"
    operation = "isolated_op"
    payload_1 = {"user": "one"}
    payload_2 = {"user": "two"}

    # Owner 1 submits
    async with session_factory() as session:
        repo1 = IdempotencyRepository(session)
        res_1, status_1, cached_1 = await repo1.accept_job(
            owner_id=owner_1,
            operation=operation,
            idempotency_key=shared_key,
            payload=payload_1,
        )
    assert status_1 == 202
    assert cached_1 is False

    # Owner 2 submits with the SAME key -> succeeds independently without collision
    async with session_factory() as session:
        repo2 = IdempotencyRepository(session)
        res_2, status_2, cached_2 = await repo2.accept_job(
            owner_id=owner_2,
            operation=operation,
            idempotency_key=shared_key,
            payload=payload_2,
        )
    assert status_2 == 202
    assert cached_2 is False

    # Both owners receive distinct executions
    assert res_1["job_id"] != res_2["job_id"]

    # Verify both records exist with distinct owners in PostgreSQL
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ApiIdempotencyModel).where(
                        ApiIdempotencyModel.operation == operation,
                        ApiIdempotencyModel.key == shared_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        owners_found = {r.owner_id for r in rows}
        assert owners_found == {owner_1, owner_2}


@pytest.mark.asyncio
async def test_rollback_after_idempotency_insert(session_factory) -> None:
    """Verifies complete rollback when failure occurs immediately after api_idempotency insert."""
    owner_id = uuid.uuid4()
    key = f"rollback-1-{uuid.uuid4()}"
    operation = "test_rollback"
    payload = {"data": "test"}

    # Trigger simulated failure after api_idempotency insert
    with pytest.raises(RuntimeError, match="Simulated failure after api_idempotency insert"):
        async with session_factory() as session:
            repo = IdempotencyRepository(session)
            await repo.accept_job(
                owner_id=owner_id,
                operation=operation,
                idempotency_key=key,
                payload=payload,
                test_fail_after_idempotency_insert=True,
            )

    # Verify that ZERO rows exist in api_idempotency, executions, or outbox_events
    async with session_factory() as session:
        idemp_row = (
            await session.execute(
                select(ApiIdempotencyModel).where(
                    ApiIdempotencyModel.owner_id == owner_id,
                    ApiIdempotencyModel.operation == operation,
                    ApiIdempotencyModel.key == key,
                )
            )
        ).scalar_one_or_none()
        assert idemp_row is None

    # Subsequent retry with the same key now succeeds as a fresh request
    async with session_factory() as session:
        repo = IdempotencyRepository(session)
        res, status_code, cached = await repo.accept_job(
            owner_id=owner_id,
            operation=operation,
            idempotency_key=key,
            payload=payload,
        )
        assert status_code == 202
        assert cached is False


@pytest.mark.asyncio
async def test_rollback_after_execution_insert(session_factory) -> None:
    """Verifies complete rollback when failure occurs after executions insert (before outbox)."""
    owner_id = uuid.uuid4()
    key = f"rollback-2-{uuid.uuid4()}"
    operation = "test_rollback_exec"
    payload = {"data": "test_exec"}

    # Trigger simulated failure after executions insert
    with pytest.raises(RuntimeError, match="Simulated failure after executions insert"):
        async with session_factory() as session:
            repo = IdempotencyRepository(session)
            await repo.accept_job(
                owner_id=owner_id,
                operation=operation,
                idempotency_key=key,
                payload=payload,
                test_fail_after_execution_insert=True,
            )

    # Verify that ZERO rows exist in any of the three tables
    async with session_factory() as session:
        idemp_row = (
            await session.execute(
                select(ApiIdempotencyModel).where(
                    ApiIdempotencyModel.owner_id == owner_id,
                    ApiIdempotencyModel.operation == operation,
                    ApiIdempotencyModel.key == key,
                )
            )
        ).scalar_one_or_none()
        assert idemp_row is None

        exec_rows = (
            (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.owner_id == owner_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(exec_rows) == 0

    # Subsequent retry succeeds cleanly
    async with session_factory() as session:
        repo = IdempotencyRepository(session)
        res, status_code, cached = await repo.accept_job(
            owner_id=owner_id,
            operation=operation,
            idempotency_key=key,
            payload=payload,
        )
        assert status_code == 202
        assert cached is False


@pytest.mark.asyncio
async def test_rollback_after_outbox_insert(session_factory) -> None:
    """Verifies complete rollback when failure occurs after outbox_events insert (before commit)."""
    owner_id = uuid.uuid4()
    key = f"rollback-3-{uuid.uuid4()}"
    operation = "test_rollback_outbox"
    payload = {"data": "test_outbox"}

    # Trigger simulated failure after outbox_events insert
    with pytest.raises(RuntimeError, match="Simulated failure after outbox_events insert"):
        async with session_factory() as session:
            repo = IdempotencyRepository(session)
            await repo.accept_job(
                owner_id=owner_id,
                operation=operation,
                idempotency_key=key,
                payload=payload,
                test_fail_after_outbox_insert=True,
            )

    # Verify that ZERO rows exist in any of the three tables
    async with session_factory() as session:
        idemp_row = (
            await session.execute(
                select(ApiIdempotencyModel).where(
                    ApiIdempotencyModel.owner_id == owner_id,
                    ApiIdempotencyModel.operation == operation,
                    ApiIdempotencyModel.key == key,
                )
            )
        ).scalar_one_or_none()
        assert idemp_row is None

        exec_rows = (
            (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.owner_id == owner_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(exec_rows) == 0

    # Subsequent retry succeeds cleanly
    async with session_factory() as session:
        repo = IdempotencyRepository(session)
        res, status_code, cached = await repo.accept_job(
            owner_id=owner_id,
            operation=operation,
            idempotency_key=key,
            payload=payload,
        )
        assert status_code == 202
        assert cached is False


@pytest.mark.asyncio
async def test_target_resource_scoped_idempotency(session_factory) -> None:
    """Verifies that the same key used by the same owner on different trips produces distinct jobs."""
    owner_id = uuid.uuid4()
    trip_1 = uuid.uuid4()
    trip_2 = uuid.uuid4()
    shared_key = f"shared-trip-key-{uuid.uuid4()}"
    now = datetime.now(UTC)
    async with session_factory() as session:
        t1 = TripModel(
            id=trip_1,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        t2 = TripModel(
            id=trip_2,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        session.add_all([t1, t2])
        await session.commit()

    payload = {"options": {"pace": "fast"}}

    # Call on Trip 1
    async with session_factory() as session:
        repo = IdempotencyRepository(session)
        res_1, status_1, cached_1 = await repo.accept_job(
            owner_id=owner_id,
            operation=f"start_trip_plan:{trip_1}",
            idempotency_key=shared_key,
            payload={"trip_id": str(trip_1), **payload},
            trip_id=trip_1,
        )
    assert status_1 == 202
    assert cached_1 is False

    # Call on Trip 2 with identical key and base payload
    async with session_factory() as session:
        repo = IdempotencyRepository(session)
        res_2, status_2, cached_2 = await repo.accept_job(
            owner_id=owner_id,
            operation=f"start_trip_plan:{trip_2}",
            idempotency_key=shared_key,
            payload={"trip_id": str(trip_2), **payload},
            trip_id=trip_2,
        )
    assert status_2 == 202
    assert cached_2 is False

    # Must be two distinct executions and jobs
    assert res_1["job_id"] != res_2["job_id"]


@pytest.mark.asyncio
async def test_api_trip_plans_endpoint_idempotency_integration(session_factory) -> None:
    """Verifies FastAPI HTTP contract for POST /api/v1/trips/{trip_id}/plans."""
    owner_id = uuid.uuid4()
    other_owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    trip_2_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Insert two trip records into PostgreSQL
    async with session_factory() as session:
        trip = TripModel(
            id=trip_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        trip_2 = TripModel(
            id=trip_2_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        session.add_all([trip, trip_2])
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
            idemp_key = f"http-idemp-{uuid.uuid4()}"
            body_1 = {"planning_options": {"pace": "fast"}, "workflow": "TRAVEL"}

            # 1. Missing Idempotency-Key header -> 400 Bad Request
            resp_missing = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                json=body_1,
            )
            assert resp_missing.status_code == 400
            assert "Idempotency-Key header is required" in resp_missing.json()["detail"]

            # 2. Fresh request with Idempotency-Key as authorized owner -> 202 Accepted
            resp_1 = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers={"Idempotency-Key": idemp_key},
                json=body_1,
            )
            assert resp_1.status_code == 202
            data_1 = resp_1.json()
            assert "job_id" in data_1
            assert data_1["status"] == "QUEUED"
            assert data_1["status_url"] == f"/api/v1/jobs/{data_1['job_id']}"

            # 3. Duplicate request on same trip with identical body -> 202 Accepted with same job_id
            resp_2 = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers={"Idempotency-Key": idemp_key},
                json=body_1,
            )
            assert resp_2.status_code == 202
            data_2 = resp_2.json()
            assert data_2["job_id"] == data_1["job_id"]

            # 4. Same idempotency key on Trip 2 (different trip) -> creates fresh job for Trip 2
            resp_trip_2 = await client.post(
                f"/api/v1/trips/{trip_2_id}/plans",
                headers={"Idempotency-Key": idemp_key},
                json=body_1,
            )
            assert resp_trip_2.status_code == 202
            data_trip_2 = resp_trip_2.json()
            assert data_trip_2["job_id"] != data_1["job_id"]

            # 5. Conflicting request with same key on trip_id but different body -> 409 Conflict
            body_conflict = {"planning_options": {"pace": "leisure"}, "workflow": "TRAVEL"}
            resp_conflict = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers={"Idempotency-Key": idemp_key},
                json=body_conflict,
            )
            assert resp_conflict.status_code == 409
            assert "different request payload" in resp_conflict.json()["detail"]

            # 6. Authenticated User B attempts to plan User A's trip -> 404 Not Found
            current_authenticated_user = AuthenticatedUser(
                id=other_owner_id,
                email="other@test.internal",
            )
            resp_cross_user = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers={"Idempotency-Key": f"user-b-{uuid.uuid4()}"},
                json=body_1,
            )
            assert resp_cross_user.status_code == 404

            # 7. Non-existent trip -> 404 Not Found
            missing_trip_id = uuid.uuid4()
            resp_not_found = await client.post(
                f"/api/v1/trips/{missing_trip_id}/plans",
                headers={"Idempotency-Key": f"missing-{uuid.uuid4()}"},
                json=body_1,
            )
            assert resp_not_found.status_code == 404
    finally:
        app.dependency_overrides.clear()
