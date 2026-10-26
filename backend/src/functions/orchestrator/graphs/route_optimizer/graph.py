"""LangGraph Route Optimizer Graph conforming to spec 0006 AC-1, AC-3.

Coordinates:
1. Route initialization and solver task specification emission.
2. Interruption barrier before result consumption (GraphRunnerService owns task/outbox insertion,
   WAITING_TASKS transition, and checkpoint commit).
3. Resumption without duplicate task dispatch.
4. Parsing RouteOptimizerWorkerOutput.route_id and loading owner-scoped persisted RouteModel.
"""

from __future__ import annotations

import logging
import uuid
from enum import StrEnum
from typing import Any, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from config.db import get_session_factory
from contracts.routing import (
    RouteOptimizerWorkerOutput,
    RouteReplanRequest,
    RouteRequest,
)
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.routing.models import RouteModel
from functions.orchestrator.routing.repository import RouteRepository

logger = logging.getLogger(__name__)

_DEFAULT_RUNNABLE_CONFIG: RunnableConfig = cast(RunnableConfig, {})


class RouteOptimizerState(TypedDict, total=False):
    """Execution state contract for RouteOptimizerGraph."""

    execution_id: str
    owner_id: str
    trip_id: str | None
    workflow: str
    current_step: str
    status: str
    errors: list[str]
    request: dict[str, Any]
    pending_tasks: list[dict[str, Any]]
    task_outputs: dict[str, Any]
    accepted_result_ids: list[str]
    route_id: str | None
    route_version: int | None
    _session_factory: Any


class RouteOptimizerNode(StrEnum):
    """Canonical node identifiers for RouteOptimizerGraph."""

    INITIALIZE_ROUTE = "initialize_route"
    AWAIT_SOLVER_RESULT = "await_solver_result"
    FINALIZE_ROUTE = "finalize_route"


def initialize_route_node(
    state: RouteOptimizerState,
) -> dict[str, Any]:
    """Validates route request and emits pending task specification for GraphRunnerService.

    Graph nodes emit pending task specifications; GraphRunnerService owns task/outbox insertion,
    WAITING_TASKS transition, and checkpoint commit. Mutations are NOT duplicated here.
    """
    execution_id = state.get("execution_id")
    raw_req = state.get("request") or {
        k: v
        for k, v in state.items()
        if k
        not in (
            "execution_id",
            "owner_id",
            "trip_id",
            "workflow",
            "current_step",
            "status",
            "errors",
            "pending_tasks",
            "task_outputs",
            "accepted_result_ids",
            "route_id",
            "route_version",
            "_session_factory",
        )
    }

    is_replan = raw_req.get("action") == "replan" or "current_location" in raw_req
    action = "replan" if is_replan else "solve"

    try:
        if is_replan:
            RouteReplanRequest.model_validate(raw_req)
        elif "stops" in raw_req and "departure_at" in raw_req:
            RouteRequest.model_validate(raw_req)
    except Exception as e:
        logger.error("Route validation failed for action %s: %s", action, e)
        return {
            "status": "FAILED",
            "errors": [f"Invalid {action} request: {e}"],
            "current_step": "initialization_failed",
        }

    task_spec = {
        "worker": "OPTIMIZER",
        "action": action,
        "payload": raw_req,
        "idempotency_key": f"{execution_id}:{action}",
        "deadline_seconds": 3600,
        "max_attempts": 3,
    }

    return {
        "current_step": "route_initialized",
        "status": "RUNNING",
        "request": raw_req,
        "pending_tasks": [task_spec],
    }


def await_solver_result_node(
    state: RouteOptimizerState,
) -> dict[str, Any]:
    """Interrupt boundary node.

    Execution halts before this node on start. When resumed after worker task completion,
    this node clears pending_tasks to ensure no duplicate tasks are dispatched.
    """
    return {
        "pending_tasks": [],
        "current_step": "awaiting_solver_result",
    }


async def finalize_route_node(
    state: RouteOptimizerState,
    config: RunnableConfig = _DEFAULT_RUNNABLE_CONFIG,
) -> dict[str, Any]:
    """Parses RouteOptimizerWorkerOutput and loads the owner-scoped persisted RouteModel entity.

    Conforms to spec 0006 AC-3, AC-8:
    - Extracts output.route_id from worker output.
    - Loads persisted RouteModel with tenant isolation fencing.
    - If route is INFEASIBLE, preserves the outcome without failing the execution.
    """
    task_outputs = state.get("task_outputs", {})
    worker_output_dict: dict[str, Any] | None = None

    for _task_id, out_val in task_outputs.items():
        if isinstance(out_val, dict) and "route_id" in out_val:
            worker_output_dict = out_val
            break

    if worker_output_dict is None:
        return {
            "status": "FAILED",
            "errors": ["Missing RouteOptimizerWorkerOutput in task_outputs"],
            "current_step": "finalize_route_failed",
        }

    try:
        worker_output = RouteOptimizerWorkerOutput.model_validate(worker_output_dict)
    except Exception as exc:
        return {
            "status": "FAILED",
            "errors": [f"Malformed RouteOptimizerWorkerOutput: {exc}"],
            "current_step": "finalize_route_failed",
        }

    route_id = worker_output.route_id
    execution_id_str = state.get("execution_id")

    # Obtain session factory from config (GraphRunnerService) or state or global
    configurable = (config or {}).get("configurable", {}) if isinstance(config, dict) else {}
    session_factory = (
        configurable.get("session_factory")
        or state.get("_session_factory")
        or get_session_factory()
    )

    async with session_factory() as session:
        # Resolve owner_id with tenant fencing
        owner_id_raw = state.get("owner_id")
        owner_id: uuid.UUID | None = uuid.UUID(str(owner_id_raw)) if owner_id_raw else None

        if owner_id is None and execution_id_str:
            exec_stmt = select(ExecutionModel.owner_id).where(
                ExecutionModel.id == uuid.UUID(execution_id_str)
            )
            exec_res = await session.execute(exec_stmt)
            owner_id = exec_res.scalar_one_or_none()

        if owner_id is None:
            return {
                "status": "FAILED",
                "errors": ["Unable to determine owner_id for route retrieval fencing"],
                "current_step": "finalize_route_failed",
            }

        route_repo = RouteRepository(session)
        route_model: RouteModel | None = await route_repo.get_route_by_id(
            route_id, owner_id=owner_id
        )

        if route_model is None:
            return {
                "status": "FAILED",
                "errors": [f"Persisted route {route_id} not found under owner {owner_id}"],
                "current_step": "finalize_route_failed",
            }

        return {
            "status": "SUCCEEDED",
            "current_step": "route_finalized",
            "route_id": str(route_model.id),
            "route_version": route_model.route_version,
            "route_status": route_model.status,
            "solver_status": route_model.solver_status,
        }


def build_route_optimizer_graph() -> StateGraph[RouteOptimizerState, Any, Any, Any]:
    """Constructs the RouteOptimizerGraph StateGraph."""
    graph: StateGraph[RouteOptimizerState, Any, Any, Any] = StateGraph(RouteOptimizerState)

    graph.add_node(RouteOptimizerNode.INITIALIZE_ROUTE, initialize_route_node)
    graph.add_node(RouteOptimizerNode.AWAIT_SOLVER_RESULT, await_solver_result_node)
    graph.add_node(RouteOptimizerNode.FINALIZE_ROUTE, finalize_route_node)

    graph.add_edge(START, RouteOptimizerNode.INITIALIZE_ROUTE)
    graph.add_edge(RouteOptimizerNode.INITIALIZE_ROUTE, RouteOptimizerNode.AWAIT_SOLVER_RESULT)
    graph.add_edge(RouteOptimizerNode.AWAIT_SOLVER_RESULT, RouteOptimizerNode.FINALIZE_ROUTE)
    graph.add_edge(RouteOptimizerNode.FINALIZE_ROUTE, END)

    return graph


def compile_route_optimizer_workflow(checkpointer: Any | None = None) -> Any:
    """Compiles the RouteOptimizerGraph with checkpointing and interrupt boundaries."""
    return build_route_optimizer_graph().compile(
        checkpointer=checkpointer,
        interrupt_before=[RouteOptimizerNode.AWAIT_SOLVER_RESULT],
    )


route_optimizer_workflow = compile_route_optimizer_workflow()
