"""Unit test suite for Slice 6: Route optimizer worker fencing, SQS consumer, and REST API.

Conforms strictly to specification 0005 AC-9:
- Asynchronous route optimization tasks claimed under WorkerFencingService.
- Respects parent execution lease fences, deadlines, and cooperative cancellation.
- Atomic task completion under lease fencing.
- SQS consumer with partial batch failure reporting (ReportBatchItemFailures).
- Asynchronous REST endpoints (POST /api/v1/optimization/routes, GET /api/v1/jobs/{job_id},
  GET /api/v1/optimization/routes/{route_id}, POST /api/v1/optimization/routes/{route_id}/replans).
- Owner isolation: caller owner_id matching token, 403 Forbidden on tenant mismatch.
- Standalone packaging beneath 250 MB ceiling.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from contracts.routing import (
    Point,
    RouteRequest,
    RouteStopInput,
    StopType,
)
from functions.api.app import app
from functions.orchestrator.execution.fencing import (
    ParentCancelledError,
    TaskClaimConflictError,
    TaskClaimResult,
)
from functions.route_optimizer.consumer import process_batch, process_record
from functions.route_optimizer.packaging import (
    build_route_optimizer_artifact,
    verify_isolated_environment_imports,
)
from functions.route_optimizer.service import (
    ExecutionDeadlineExceededError,
    RouteOptimizerWorkerService,
    parse_optimizer_dispatch_message,
)
from middleware.auth import AuthenticatedUser, get_current_user


def _build_sample_route_request() -> RouteRequest:
    """Builds a valid RouteRequest for testing."""
    dep_time = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    return RouteRequest(
        departure_at=dep_time,
        stops=[
            RouteStopInput(
                name="Start Origin",
                location=Point(latitude=22.5726, longitude=88.3639),
                stop_type=StopType.ORIGIN,
            ),
            RouteStopInput(
                name="Destination Stop",
                location=Point(latitude=22.5448, longitude=88.3426),
                stop_type=StopType.DESTINATION,
                dwell_seconds=1800,
            ),
        ],
    )


# 1. Envelope Parsing Unit Tests
def test_parse_optimizer_dispatch_message_dict() -> None:
    """Verifies parsing of direct dictionary dispatch message."""
    task_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    msg = {
        "task_id": str(task_id),
        "execution_id": str(exec_id),
        "worker": "OPTIMIZER",
        "action": "solve",
        "attempt": 1,
        "context_version": 2,
        "payload": {"time_limit_seconds": 10},
    }
    env = parse_optimizer_dispatch_message(msg)
    assert env.task_id == task_id
    assert env.execution_id == exec_id
    assert env.worker == "OPTIMIZER"
    assert env.action == "solve"
    assert env.attempt == 1
    assert env.context_version == 2
    assert env.payload == {"time_limit_seconds": 10}


def test_parse_optimizer_dispatch_message_sqs_body_json() -> None:
    """Verifies parsing of JSON-encoded SQS body record."""
    task_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    body_dict = {
        "task_id": str(task_id),
        "execution_id": str(exec_id),
        "worker": "OPTIMIZER",
        "action": "solve",
        "payload_ref": {"time_limit_seconds": 10},
    }
    record = {"body": json.dumps(body_dict)}
    env = parse_optimizer_dispatch_message(record)
    assert env.task_id == task_id
    assert env.execution_id == exec_id


def test_parse_optimizer_dispatch_message_missing_ids() -> None:
    """Verifies ValueError is raised if required IDs are missing."""
    with pytest.raises(ValueError, match="Missing required task_id or execution_id"):
        parse_optimizer_dispatch_message({"worker": "OPTIMIZER", "action": "solve"})


# 2. Worker Service Unit Tests
@pytest.mark.asyncio
async def test_worker_service_rejects_unsupported_worker() -> None:
    """Verifies that non-optimizer workers are rejected."""
    mock_session = AsyncMock()
    service = RouteOptimizerWorkerService(mock_session)
    msg = {
        "task_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "worker": "BROWSER",
        "action": "solve",
    }
    with pytest.raises(ValueError, match="Unsupported worker: BROWSER"):
        await service.execute_task(msg)


@pytest.mark.asyncio
async def test_worker_service_rejects_unsupported_action() -> None:
    """Verifies that non-solve/replan actions are rejected."""
    mock_session = AsyncMock()
    service = RouteOptimizerWorkerService(mock_session)
    msg = {
        "task_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "worker": "OPTIMIZER",
        "action": "extract",
    }
    with pytest.raises(ValueError, match="Unsupported action: extract"):
        await service.execute_task(msg)


@pytest.mark.asyncio
async def test_worker_service_cooperative_cancellation() -> None:
    """Verifies ParentCancelledError is raised when parent execution is CANCELLED."""
    mock_session = AsyncMock()
    mock_fencing = AsyncMock()

    claim_res = TaskClaimResult(
        task_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        lease_token=uuid.uuid4(),
        lease_expiry=datetime.now(UTC) + timedelta(minutes=5),
        worker="OPTIMIZER",
        action="solve",
        context_version=1,
        attempt=0,
        payload_ref={},
    )
    mock_fencing.claim_task.return_value = claim_res

    # Mock cancelled execution
    mock_exec = MagicMock()
    mock_exec.status = "CANCELLED"
    mock_exec.cancelled_at = datetime.now(UTC)
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = mock_exec
    mock_session.execute.return_value = mock_res

    service = RouteOptimizerWorkerService(mock_session, fencing_service=mock_fencing)
    msg = {
        "task_id": str(claim_res.task_id),
        "execution_id": str(claim_res.execution_id),
        "worker": "OPTIMIZER",
        "action": "solve",
        "payload": {},
    }

    with pytest.raises(ParentCancelledError):
        await service.execute_task(msg)


@pytest.mark.asyncio
async def test_worker_service_deadline_exceeded() -> None:
    """Verifies ExecutionDeadlineExceededError is raised when deadline has expired."""
    mock_session = AsyncMock()
    mock_fencing = AsyncMock()

    claim_res = TaskClaimResult(
        task_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        lease_token=uuid.uuid4(),
        lease_expiry=datetime.now(UTC) + timedelta(minutes=5),
        worker="OPTIMIZER",
        action="solve",
        context_version=1,
        attempt=0,
        payload_ref={},
    )
    mock_fencing.claim_task.return_value = claim_res

    # Mock expired execution
    mock_exec = MagicMock()
    mock_exec.status = "RUNNING"
    mock_exec.cancelled_at = None
    mock_exec.deadline_at = datetime.now(UTC) - timedelta(seconds=10)
    mock_res_deadline = MagicMock()
    mock_res_deadline.scalar_one_or_none.return_value = mock_exec
    mock_session.execute.return_value = mock_res_deadline

    service = RouteOptimizerWorkerService(mock_session, fencing_service=mock_fencing)
    msg = {
        "task_id": str(claim_res.task_id),
        "execution_id": str(claim_res.execution_id),
        "worker": "OPTIMIZER",
        "action": "solve",
        "payload": {},
    }

    with pytest.raises(ExecutionDeadlineExceededError):
        await service.execute_task(msg)


# 3. SQS Consumer Unit Tests
@pytest.mark.asyncio
async def test_consumer_discards_fencing_conflicts_safely() -> None:
    """Verifies that conflict errors discard task dispatch without failing consumer."""
    mock_session_factory = MagicMock()

    with patch(
        "functions.route_optimizer.consumer.RouteOptimizerWorkerService.execute_task",
        side_effect=TaskClaimConflictError("Task claimed by another worker"),
    ):
        record = {
            "task_id": str(uuid.uuid4()),
            "execution_id": str(uuid.uuid4()),
            "worker": "OPTIMIZER",
            "action": "solve",
        }
        # Should not raise exception
        await process_record(record, mock_session_factory)


@pytest.mark.asyncio
async def test_consumer_process_batch_partial_failures_on_deadline() -> None:
    """Verifies process_batch reports partial failure when Lambda deadline is under 5000ms."""
    context = MagicMock()
    context.get_remaining_time_in_millis.return_value = 3000  # < 5000ms

    event = {
        "Records": [
            {
                "messageId": "msg-12345",
                "body": json.dumps({
                    "task_id": str(uuid.uuid4()),
                    "execution_id": str(uuid.uuid4()),
                    "worker": "OPTIMIZER",
                    "action": "solve",
                }),
            }
        ]
    }
    failures = await process_batch(event, context=context)
    assert len(failures) == 1
    assert failures[0]["itemIdentifier"] == "msg-12345"


# 4. Standalone Packaging Unit Test
def test_route_optimizer_packaging_within_limits(tmp_path) -> None:
    """Verifies that route optimizer worker packages standalone under 250 MB limit."""
    dist_dir = tmp_path / "build" / "route_optimizer"

    # Run packaging directly on actual backend
    from pathlib import Path
    actual_backend = Path(__file__).resolve().parent.parent.parent

    metrics = build_route_optimizer_artifact(dist_dir, actual_backend, install_dependencies=False)
    assert metrics["is_uncompressed_within_limit"] is True
    assert metrics["uncompressed_mb"] < 250.0
    assert metrics["compressed_mb"] < 50.0
    assert verify_isolated_environment_imports(dist_dir) is True


# 5. REST API Unit Tests
@pytest.mark.asyncio
async def test_api_submit_route_optimization_validation_and_acceptance() -> None:
    """Verifies POST /api/v1/optimization/routes returns 202 Accepted with status_url."""
    caller_id = uuid.uuid4()
    auth_user = AuthenticatedUser(id=caller_id, email="traveler@test.local")

    app.dependency_overrides[get_current_user] = lambda: auth_user

    mock_session = AsyncMock()
    mock_session.execute.return_value.scalar_one_or_none.return_value = caller_id

    from config.db import get_session
    app.dependency_overrides[get_session] = lambda: mock_session

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            req_payload = {
                "departure_at": "2026-10-24T09:00:00Z",
                "stops": [
                    {
                        "name": "Origin Hub",
                        "location": {"latitude": 22.5726, "longitude": 88.3639},
                        "stop_type": "ORIGIN",
                    },
                    {
                        "name": "Destination Venue",
                        "location": {"latitude": 22.5448, "longitude": 88.3426},
                        "stop_type": "DESTINATION",
                        "dwell_seconds": 1800,
                    },
                ],
            }
            resp = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": f"test-key-{uuid.uuid4()}"},
                json=req_payload,
            )
            assert resp.status_code == 202
            data = resp.json()
            assert "job_id" in data
            assert data["status"] == "QUEUED"
            assert data["status_url"] == f"/api/v1/jobs/{data['job_id']}"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_get_route_tenant_isolation_forbidden() -> None:
    """Verifies GET /api/v1/optimization/routes/{route_id} returns 403 Forbidden for cross-tenant access."""
    caller_id = uuid.uuid4()
    other_owner_id = uuid.uuid4()
    route_id = uuid.uuid4()

    auth_user = AuthenticatedUser(id=caller_id, email="caller@test.local")
    app.dependency_overrides[get_current_user] = lambda: auth_user

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = other_owner_id
    mock_session.execute.return_value = mock_result

    from config.db import get_session
    from functions.orchestrator.routing.repository import RouteRepository
    app.dependency_overrides[get_session] = lambda: mock_session

    try:
        with patch.object(RouteRepository, "get_route_by_id", new_callable=AsyncMock, return_value=None):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/v1/optimization/routes/{route_id}")
                assert resp.status_code == 403
                assert "belongs to another tenant" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()
