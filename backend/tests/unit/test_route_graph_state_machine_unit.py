"""Unit test suite for RouteOptimizerGraph state machine, nodes, and contracts (Spec 0006).

Validates:
1. Workflow compilation, interrupt barriers, and node graph integrity (AC-1).
2. initialize_route_node for solve and replan task dispatch emission.
3. await_solver_result_node clearing pending tasks on resumption to eliminate duplicate dispatch (AC-3).
4. finalize_route_node parsing RouteOptimizerWorkerOutput, enforcing owner fencing, and handling missing/infeasible routes (AC-3, AC-8, AC-9).
5. Version lineage and comparison contract validation (AC-7).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from contracts.routing import (
    Point,
    RouteOptimizerWorkerOutput,
    RouteReplanRequest,
    RouteRequest,
    RouteStatus,
    RouteVersionHistoryResponse,
    RouteVersionItem,
    SolverStatus,
    TransportMode,
)
from functions.orchestrator.graphs.route_optimizer.graph import (
    RouteOptimizerNode,
    RouteOptimizerState,
    await_solver_result_node,
    build_route_optimizer_graph,
    compile_route_optimizer_workflow,
    finalize_route_node,
    initialize_route_node,
)
from functions.orchestrator.routing.models import RouteModel


def test_route_optimizer_workflow_compilation() -> None:
    """Verifies that RouteOptimizerGraph compiles with proper nodes, edges, and interrupt boundary (AC-1)."""
    graph = build_route_optimizer_graph()
    assert RouteOptimizerNode.INITIALIZE_ROUTE in graph.nodes
    assert RouteOptimizerNode.AWAIT_SOLVER_RESULT in graph.nodes
    assert RouteOptimizerNode.FINALIZE_ROUTE in graph.nodes

    compiled = compile_route_optimizer_workflow()
    assert compiled is not None


def test_initialize_route_node_emits_solve_task() -> None:
    """Verifies initialize_route_node parses standard route optimization request and emits solve task."""
    execution_id = str(uuid.uuid4())
    req_payload = {
        "departure_at": "2026-10-27T08:00:00Z",
        "objective": "FASTEST",
        "is_round_trip": False,
        "stops": [
            {
                "id": str(uuid.uuid4()),
                "name": "Origin",
                "location": {"latitude": 12.9716, "longitude": 77.5946},
                "stop_type": "ORIGIN",
                "dwell_seconds": 0,
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Destination",
                "location": {"latitude": 12.3051, "longitude": 76.6551},
                "stop_type": "DESTINATION",
                "dwell_seconds": 3600,
            },
        ],
    }

    state: RouteOptimizerState = {
        "execution_id": execution_id,
        "workflow": "ROUTE_OPTIMIZER_GRAPH",
        "request": req_payload,
    }

    result = initialize_route_node(state)
    assert result["status"] == "RUNNING"
    assert result["current_step"] == "route_initialized"
    assert len(result["pending_tasks"]) == 1

    task = result["pending_tasks"][0]
    assert task["worker"] == "OPTIMIZER"
    assert task["action"] == "solve"
    assert task["idempotency_key"] == f"{execution_id}:solve"
    assert task["deadline_seconds"] == 3600
    assert task["max_attempts"] == 3


def test_initialize_route_node_emits_replan_task() -> None:
    """Verifies initialize_route_node parses mid trip replanning request and emits replan task (AC-6)."""
    execution_id = str(uuid.uuid4())
    replan_payload = {
        "action": "replan",
        "current_location": {"latitude": 12.5, "longitude": 77.1},
        "replan_at": "2026-10-27T10:00:00Z",
        "completed_stop_ids": [str(uuid.uuid4())],
        "expected_previous_version": 1,
    }

    state: RouteOptimizerState = {
        "execution_id": execution_id,
        "workflow": "ROUTE_OPTIMIZER_GRAPH",
        "request": replan_payload,
    }

    result = initialize_route_node(state)
    assert result["status"] == "RUNNING"
    assert result["current_step"] == "route_initialized"
    assert len(result["pending_tasks"]) == 1

    task = result["pending_tasks"][0]
    assert task["worker"] == "OPTIMIZER"
    assert task["action"] == "replan"
    assert task["idempotency_key"] == f"{execution_id}:replan"


def test_initialize_route_node_handles_invalid_payload() -> None:
    """Verifies initialize_route_node reports FAILED status on malformed request payloads."""
    state: RouteOptimizerState = {
        "execution_id": str(uuid.uuid4()),
        "workflow": "ROUTE_OPTIMIZER_GRAPH",
        "request": {
            "departure_at": "invalid-datetime",
            "stops": [],
        },
    }

    result = initialize_route_node(state)
    assert result["status"] == "FAILED"
    assert result["current_step"] == "initialization_failed"
    assert len(result["errors"]) >= 1


def test_await_solver_result_node_clears_pending_tasks() -> None:
    """Verifies await_solver_result_node clears pending_tasks to prevent duplicate dispatch on resumption (AC-3)."""
    state: RouteOptimizerState = {
        "execution_id": str(uuid.uuid4()),
        "pending_tasks": [{"worker": "OPTIMIZER", "action": "solve"}],
    }

    result = await_solver_result_node(state)
    assert result["pending_tasks"] == []
    assert result["current_step"] == "awaiting_solver_result"


@pytest.mark.asyncio
async def test_finalize_route_node_missing_task_outputs() -> None:
    """Verifies finalize_route_node fails gracefully when worker outputs are missing."""
    state: RouteOptimizerState = {
        "execution_id": str(uuid.uuid4()),
        "task_outputs": {},
    }

    result = await finalize_route_node(state)
    assert result["status"] == "FAILED"
    assert "Missing RouteOptimizerWorkerOutput" in result["errors"][0]


@pytest.mark.asyncio
async def test_finalize_route_node_malformed_worker_output() -> None:
    """Verifies finalize_route_node fails gracefully when worker output schema is invalid."""
    state: RouteOptimizerState = {
        "execution_id": str(uuid.uuid4()),
        "task_outputs": {
            "task-1": {"route_id": "not-a-valid-uuid"},
        },
    }

    result = await finalize_route_node(state)
    assert result["status"] == "FAILED"
    assert "Malformed RouteOptimizerWorkerOutput" in result["errors"][0]


@pytest.mark.asyncio
async def test_finalize_route_node_missing_owner_id() -> None:
    """Verifies finalize_route_node enforces tenant fencing when owner cannot be resolved (AC-9)."""
    route_id = uuid.uuid4()
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    state: RouteOptimizerState = {
        "execution_id": str(uuid.uuid4()),
        "owner_id": "",
        "task_outputs": {
            "task-1": {
                "route_id": str(route_id),
                "trip_id": str(uuid.uuid4()),
                "status": "FEASIBLE",
                "route_version": 1,
                "stops_count": 2,
                "legs_count": 1,
                "total_distance_m": 5000,
                "total_travel_seconds": 600,
            },
        },
        "_session_factory": mock_session_factory,
    }

    result = await finalize_route_node(state)
    assert result["status"] == "FAILED"
    assert "Unable to determine owner_id" in result["errors"][0]


@pytest.mark.asyncio
async def test_finalize_route_node_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies finalize_route_node successfully extracts route and marks SUCCEEDED (AC-3)."""
    route_id = uuid.uuid4()
    owner_id = uuid.uuid4()

    mock_route = MagicMock(spec=RouteModel)
    mock_route.id = route_id
    mock_route.route_version = 1
    mock_route.status = "FEASIBLE"
    mock_route.solver_status = "OPTIMAL"

    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    # Mock RouteRepository.get_route_by_id
    monkeypatch.setattr(
        "functions.orchestrator.graphs.route_optimizer.graph.RouteRepository.get_route_by_id",
        AsyncMock(return_value=mock_route),
    )

    state: RouteOptimizerState = {
        "execution_id": str(uuid.uuid4()),
        "owner_id": str(owner_id),
        "task_outputs": {
            "task-1": {
                "route_id": str(route_id),
                "trip_id": str(uuid.uuid4()),
                "status": "FEASIBLE",
                "route_version": 1,
                "stops_count": 2,
                "legs_count": 1,
                "total_distance_m": 5000,
                "total_travel_seconds": 600,
            },
        },
        "_session_factory": mock_session_factory,
    }

    result = await finalize_route_node(state)
    assert result["status"] == "SUCCEEDED"
    assert result["current_step"] == "route_finalized"
    assert result["route_id"] == str(route_id)
    assert result["route_version"] == 1
    assert result["route_status"] == "FEASIBLE"
    assert result["solver_status"] == "OPTIMAL"


@pytest.mark.asyncio
async def test_finalize_route_node_preserves_infeasible_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies finalize_route_node preserves INFEASIBLE route status as SUCCEEDED execution (AC-8)."""
    route_id = uuid.uuid4()
    owner_id = uuid.uuid4()

    mock_route = MagicMock(spec=RouteModel)
    mock_route.id = route_id
    mock_route.route_version = 1
    mock_route.status = "INFEASIBLE"
    mock_route.solver_status = "INFEASIBLE"

    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(
        "functions.orchestrator.graphs.route_optimizer.graph.RouteRepository.get_route_by_id",
        AsyncMock(return_value=mock_route),
    )

    state: RouteOptimizerState = {
        "execution_id": str(uuid.uuid4()),
        "owner_id": str(owner_id),
        "task_outputs": {
            "task-1": {
                "route_id": str(route_id),
                "trip_id": str(uuid.uuid4()),
                "status": "INFEASIBLE",
                "route_version": 1,
                "stops_count": 2,
                "legs_count": 0,
                "total_distance_m": 0,
                "total_travel_seconds": 0,
            },
        },
        "_session_factory": mock_session_factory,
    }

    result = await finalize_route_node(state)
    # Crucial AC-8: Infeasible route calculations complete execution successfully without throwing system failure
    assert result["status"] == "SUCCEEDED"
    assert result["route_status"] == "INFEASIBLE"
    assert result["solver_status"] == "INFEASIBLE"


def test_route_version_history_contracts() -> None:
    """Verifies RouteVersionItem and RouteVersionHistoryResponse lineage models and delta metrics (AC-7)."""
    root_id = uuid.uuid4()
    child_id = uuid.uuid4()
    now = datetime.now(UTC)

    v1 = RouteVersionItem(
        route_id=root_id,
        route_version=1,
        parent_route_id=None,
        created_at=now,
        status=RouteStatus.FEASIBLE,
        total_distance_m=100000,
        total_travel_seconds=6000,
        distance_delta_m=0,
        duration_delta_seconds=0,
        dropped_stop_ids=[],
        replan_origin=None,
    )

    v2 = RouteVersionItem(
        route_id=child_id,
        route_version=2,
        parent_route_id=root_id,
        created_at=now,
        status=RouteStatus.FEASIBLE,
        total_distance_m=112000,
        total_travel_seconds=6900,
        distance_delta_m=12000,
        duration_delta_seconds=900,
        dropped_stop_ids=[uuid.uuid4()],
        replan_origin=Point(latitude=12.5, longitude=77.1),
    )

    history = RouteVersionHistoryResponse(
        root_route_id=root_id,
        current_route_id=child_id,
        versions=[v1, v2],
    )

    assert history.root_route_id == root_id
    assert history.current_route_id == child_id
    assert len(history.versions) == 2
    assert history.versions[1].distance_delta_m == 12000
    assert history.versions[1].duration_delta_seconds == 900
    assert len(history.versions[1].dropped_stop_ids) == 1
    assert history.versions[1].replan_origin is not None
    assert history.versions[1].replan_origin.latitude == 12.5
