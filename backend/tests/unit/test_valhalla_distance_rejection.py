"""Unit tests for Valhalla matrix distance rejection classification and recovery.

Conforms to spec 0007 /develop recovery slice:
1. Classifies HTTP 400 with error_code 154 from /sources_to_targets.
2. Fast fails without wasteful retries (nonretryable).
3. Distinguishes from other 4xx errors, other endpoints, and unrelated provider errors.
4. Completes TRAVEL optimizer tasks under lease with degraded ROUTING_UNAVAILABLE output.
5. Re-raises for unrelated errors and non-TRAVEL (standalone route) workflows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from contracts.routing import (
    Point,
    RouteRequest,
    RouteStopInput,
    StopType,
)
from contracts.trips import TransportMode
from functions.orchestrator.execution.fencing import (
    TaskClaimResult,
    WorkerFencingService,
)
from functions.orchestrator.execution.models import ExecutionModel
from functions.route_optimizer.service import (
    RouteOptimizerDispatchEnvelope,
    RouteOptimizerWorkerService,
)
from functions.route_optimizer.valhalla import (
    ValhallaClient,
    ValhallaRoutingError,
    is_valhalla_matrix_distance_limit_error,
)


@pytest.mark.asyncio
async def test_valhalla_error_code_154_classified_correctly() -> None:
    """Verifies that Valhalla HTTP 400 with error_code 154 on /sources_to_targets is correctly classified."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert "/sources_to_targets" in str(request.url)
        return httpx.Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            json={
                "error_code": 154,
                "error": "Path distance exceeds the max distance limit: 150000 meters",
                "status_code": 400,
                "status": "Bad Request",
            },
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ValhallaClient("http://valhalla.test", http_client=http_client)
        points = [
            Point(latitude=12.9716, longitude=77.5946),  # Bengaluru
            Point(latitude=12.4244, longitude=75.7382),  # Madikeri (~250km)
        ]

        with pytest.raises(ValhallaRoutingError) as exc_info:
            await client.sources_to_targets(points, points, mode=TransportMode.CAR_PETROL)

        exc = exc_info.value
        assert exc.status_code == 400
        assert exc.error_code == 154
        assert exc.endpoint == "/sources_to_targets"
        assert exc.is_matrix_distance_limit_exceeded is True
        assert is_valhalla_matrix_distance_limit_error(exc) is True
        assert client.call_count == 1
        assert client.retry_count == 0


@pytest.mark.asyncio
async def test_valhalla_other_400_not_classified_as_distance_limit() -> None:
    """Verifies that other 400 errors (e.g. unknown location) are NOT classified as distance limit."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            json={
                "error_code": 171,
                "error": "No suitable edges near location",
                "status_code": 400,
                "status": "Bad Request",
            },
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ValhallaClient("http://valhalla.test", http_client=http_client)
        points = [
            Point(latitude=12.9716, longitude=77.5946),
            Point(latitude=12.2958, longitude=76.6394),
        ]

        with pytest.raises(ValhallaRoutingError) as exc_info:
            await client.sources_to_targets(points, points, mode=TransportMode.CAR_PETROL)

        exc = exc_info.value
        assert exc.status_code == 400
        assert exc.error_code == 171
        assert exc.is_matrix_distance_limit_exceeded is False
        assert is_valhalla_matrix_distance_limit_error(exc) is False
        assert client.call_count == 1


@pytest.mark.asyncio
async def test_valhalla_route_endpoint_not_classified_as_matrix_distance_limit() -> None:
    """Verifies that an error on /route is NOT classified as a matrix distance limit error."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert "/route" in str(request.url)
        return httpx.Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            json={
                "error_code": 154,
                "error": "Some other error",
                "status_code": 400,
                "status": "Bad Request",
            },
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ValhallaClient("http://valhalla.test", http_client=http_client)
        locations = [
            Point(latitude=12.9716, longitude=77.5946),
            Point(latitude=12.4244, longitude=75.7382),
        ]

        with pytest.raises(ValhallaRoutingError) as exc_info:
            await client.route(locations, mode=TransportMode.CAR_PETROL)

        exc = exc_info.value
        assert exc.status_code == 400
        assert exc.endpoint == "/route"
        assert exc.is_matrix_distance_limit_exceeded is False
        assert is_valhalla_matrix_distance_limit_error(exc) is False


@pytest.mark.asyncio
async def test_optimizer_worker_degraded_recovery_on_distance_rejection() -> None:
    """Verifies that RouteOptimizerWorkerService catches HTTP 400/154 for TRAVEL workflow and completes under lease."""
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    lease_token = uuid.uuid4()

    now = datetime.now(UTC)
    mock_session = AsyncMock()
    mock_session.add = MagicMock()

    # Mock ExecutionModel lookup and restrictions query
    exec_row = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow="TRAVEL",
        status="RUNNING",
        deadline_at=datetime.now(UTC).replace(year=2099),
    )
    mock_exec_res = MagicMock()
    mock_exec_res.scalar_one_or_none.return_value = exec_row
    mock_exec_res.mappings.return_value.all.return_value = []
    mock_session.execute.return_value = mock_exec_res

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=datetime.now(UTC),
        stops=[
            RouteStopInput(name="Bengaluru", location=Point(latitude=12.9716, longitude=77.5946), stop_type=StopType.ORIGIN),
            RouteStopInput(name="Madikeri", location=Point(latitude=12.4244, longitude=75.7382), stop_type=StopType.DESTINATION),
        ],
    )

    # Mock fencing service
    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=execution_id,
        lease_token=lease_token,
        attempt=1,
        context_version=1,
        payload_ref=req.model_dump(mode="json"),
        worker="OPTIMIZER",
        action="solve",
        lease_expiry=now.replace(year=2099),
    )
    mock_fencing.complete_task.return_value = uuid.uuid4()

    # Mock Valhalla client raising 400/154
    mock_valhalla = AsyncMock()
    mock_valhalla.base_url = "http://valhalla.test"
    mock_valhalla.sources_to_targets.side_effect = ValhallaRoutingError(
        "Valhalla client error 400 for /sources_to_targets: Path distance exceeds the max distance limit",
        status_code=400,
        error_code=154,
        endpoint="/sources_to_targets",
    )

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
        fencing_service=mock_fencing,
    )

    envelope = RouteOptimizerDispatchEnvelope(
        task_id=task_id,
        execution_id=execution_id,
        worker="OPTIMIZER",
        action="solve",
        payload=req.model_dump(mode="json"),
        attempt=1,
        context_version=1,
    )

    persisted_route_id, output = await worker_service.execute_task(envelope)

    # Must return None for persisted route ID (no phantom route)
    assert persisted_route_id is None
    assert isinstance(output, dict)
    assert output["status"] == "FAILED"
    assert output["routing_status"] == "ROUTING_UNAVAILABLE"
    assert "route_id" not in output
    assert any("distance limit exceeded" in d for d in output["diagnostics"])

    # Must have completed task under lease
    mock_fencing.complete_task.assert_awaited_once()
    complete_call_kwargs = mock_fencing.complete_task.call_args.kwargs
    assert complete_call_kwargs["execution_id"] == execution_id
    assert complete_call_kwargs["task_id"] == task_id
    assert complete_call_kwargs["lease_token"] == lease_token
    assert complete_call_kwargs["output"]["routing_status"] == "ROUTING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_optimizer_worker_reraises_for_non_travel_workflow() -> None:
    """Verifies that HTTP 400/154 is NOT caught for non-TRAVEL (standalone route) workflows."""
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    lease_token = uuid.uuid4()

    now = datetime.now(UTC)
    mock_session = AsyncMock()
    mock_session.add = MagicMock()

    # Workflow is standalone ROUTE_OPTIMIZATION with no trip_id
    exec_row = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=None,
        workflow="ROUTE_OPTIMIZATION",
        status="RUNNING",
        deadline_at=datetime.now(UTC).replace(year=2099),
    )
    mock_exec_res = MagicMock()
    mock_exec_res.scalar_one_or_none.return_value = exec_row
    mock_exec_res.mappings.return_value.all.return_value = []
    mock_session.execute.return_value = mock_exec_res

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=None,
        execution_id=execution_id,
        departure_at=datetime.now(UTC),
        stops=[
            RouteStopInput(name="A", location=Point(latitude=12.9716, longitude=77.5946), stop_type=StopType.ORIGIN),
            RouteStopInput(name="B", location=Point(latitude=12.4244, longitude=75.7382), stop_type=StopType.DESTINATION),
        ],
    )

    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=execution_id,
        lease_token=lease_token,
        attempt=1,
        context_version=1,
        payload_ref=req.model_dump(mode="json"),
        worker="OPTIMIZER",
        action="solve",
        lease_expiry=now.replace(year=2099),
    )

    mock_valhalla = AsyncMock()
    mock_valhalla.base_url = "http://valhalla.test"
    mock_valhalla.sources_to_targets.side_effect = ValhallaRoutingError(
        "Valhalla client error 400 for /sources_to_targets: Path distance exceeds the max distance limit",
        status_code=400,
        error_code=154,
        endpoint="/sources_to_targets",
    )

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
        fencing_service=mock_fencing,
    )

    envelope = RouteOptimizerDispatchEnvelope(
        task_id=task_id,
        execution_id=execution_id,
        worker="OPTIMIZER",
        action="solve",
        payload=req.model_dump(mode="json"),
        attempt=1,
        context_version=1,
    )

    with pytest.raises(ValhallaRoutingError):
        await worker_service.execute_task(envelope)

    # Must NOT have completed task
    mock_fencing.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_optimizer_worker_reraises_for_route_optimizer_graph_with_populated_trip_id() -> None:
    """Verifies that HTTP 400/154 is re-raised for ROUTE_OPTIMIZER_GRAPH even with populated trip_id."""
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    lease_token = uuid.uuid4()

    now = datetime.now(UTC)
    mock_session = AsyncMock()
    mock_session.add = MagicMock()

    # Workflow is explicitly ROUTE_OPTIMIZER_GRAPH, but trip_id is populated
    exec_row = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow="ROUTE_OPTIMIZER_GRAPH",
        status="RUNNING",
        deadline_at=datetime.now(UTC).replace(year=2099),
    )
    mock_exec_res = MagicMock()
    mock_exec_res.scalar_one_or_none.return_value = exec_row
    mock_exec_res.mappings.return_value.all.return_value = []
    mock_session.execute.return_value = mock_exec_res

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=datetime.now(UTC),
        stops=[
            RouteStopInput(name="A", location=Point(latitude=12.9716, longitude=77.5946), stop_type=StopType.ORIGIN),
            RouteStopInput(name="B", location=Point(latitude=12.4244, longitude=75.7382), stop_type=StopType.DESTINATION),
        ],
    )

    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=execution_id,
        lease_token=lease_token,
        attempt=1,
        context_version=1,
        payload_ref=req.model_dump(mode="json"),
        worker="OPTIMIZER",
        action="solve",
        lease_expiry=now.replace(year=2099),
    )

    mock_valhalla = AsyncMock()
    mock_valhalla.base_url = "http://valhalla.test"
    mock_valhalla.sources_to_targets.side_effect = ValhallaRoutingError(
        "Valhalla client error 400 for /sources_to_targets: Path distance exceeds the max distance limit",
        status_code=400,
        error_code=154,
        endpoint="/sources_to_targets",
    )

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
        fencing_service=mock_fencing,
    )

    envelope = RouteOptimizerDispatchEnvelope(
        task_id=task_id,
        execution_id=execution_id,
        worker="OPTIMIZER",
        action="solve",
        payload=req.model_dump(mode="json"),
        attempt=1,
        context_version=1,
    )

    with pytest.raises(ValhallaRoutingError) as exc_info:
        await worker_service.execute_task(envelope)

    assert exc_info.value.status_code == 400
    assert exc_info.value.error_code == 154

    # Must NOT have completed task under degraded recovery
    mock_fencing.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_optimizer_worker_reraises_unrelated_valhalla_error() -> None:
    """Verifies that unrelated Valhalla errors (e.g. 500 server error) are re-raised even for TRAVEL workflow."""
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    lease_token = uuid.uuid4()

    now = datetime.now(UTC)
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    exec_row = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow="TRAVEL",
        status="RUNNING",
        deadline_at=datetime.now(UTC).replace(year=2099),
    )
    mock_exec_res = MagicMock()
    mock_exec_res.scalar_one_or_none.return_value = exec_row
    mock_exec_res.mappings.return_value.all.return_value = []
    mock_session.execute.return_value = mock_exec_res

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=datetime.now(UTC),
        stops=[
            RouteStopInput(name="A", location=Point(latitude=12.9716, longitude=77.5946), stop_type=StopType.ORIGIN),
            RouteStopInput(name="B", location=Point(latitude=12.4244, longitude=75.7382), stop_type=StopType.DESTINATION),
        ],
    )

    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=execution_id,
        lease_token=lease_token,
        attempt=1,
        context_version=1,
        payload_ref=req.model_dump(mode="json"),
        worker="OPTIMIZER",
        action="solve",
        lease_expiry=now.replace(year=2099),
    )

    mock_valhalla = AsyncMock()
    mock_valhalla.base_url = "http://valhalla.test"
    # Unrelated 500 error
    mock_valhalla.sources_to_targets.side_effect = ValhallaRoutingError(
        "Valhalla server error 500: Internal server error",
        status_code=500,
        endpoint="/sources_to_targets",
    )

    worker_service = RouteOptimizerWorkerService(
        mock_session,
        valhalla_client=mock_valhalla,
        fencing_service=mock_fencing,
    )

    envelope = RouteOptimizerDispatchEnvelope(
        task_id=task_id,
        execution_id=execution_id,
        worker="OPTIMIZER",
        action="solve",
        payload=req.model_dump(mode="json"),
        attempt=1,
        context_version=1,
    )

    with pytest.raises(ValhallaRoutingError):
        await worker_service.execute_task(envelope)

    mock_fencing.complete_task.assert_not_awaited()
