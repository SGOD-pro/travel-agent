"""Graph runner execution service conforming to spec 0001.

Handles EXECUTION_START and EXECUTION_RESUME outbox events.
Controls LangGraph start, execution continuation, checkpoint persistence,
atomic task dispatching (Transaction 2), and barrier enforcement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from functions.orchestrator.execution.fencing import (
    ExecutionEngineError,
    ExecutionNotFoundError,
    StaleContextError,
)
from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import (
    ExecutionModel,
    OutboxEventModel,
    TaskModel,
)

logger = logging.getLogger(__name__)

DEFAULT_RUNNER_LEASE_SECONDS: float = 25.0
DEFAULT_MAX_STEPS: int = 25


class GraphRunnerError(ExecutionEngineError):
    """Base exception for graph runner errors."""


class DuplicateExecutionError(GraphRunnerError):
    """Raised when duplicate execution attempt is detected."""


class GraphExecutionTimeoutError(GraphRunnerError):
    """Raised when graph execution exceeds runtime limit."""


@dataclass
class TaskSpec:
    """Specification for a task produced by a LangGraph node."""

    worker: str
    action: str
    payload: dict[str, Any]
    idempotency_key: str | None = None
    max_attempts: int = 3
    deadline_seconds: int = 3600


@dataclass
class ExecutionStartEvent:
    """Incoming event to begin workflow execution."""

    execution_id: uuid.UUID
    event_id: uuid.UUID | None = None
    workflow: str = "TRAVEL"
    context_version: int = 1
    payload: dict[str, Any] | None = None


@dataclass
class ExecutionResumeEvent:
    """Incoming event to resume workflow execution after task reconciliation."""

    execution_id: uuid.UUID
    event_id: uuid.UUID | None = None
    triggered_by_task_id: uuid.UUID | None = None
    action: str = "resume_graph"
    context_version: int = 1


@dataclass
class GraphRunnerResult:
    """Outcome of processing an execution start or resume event."""

    execution_id: uuid.UUID
    status: str
    tasks_dispatched: int = 0
    task_ids: list[uuid.UUID] = field(default_factory=list)
    graph_advanced: bool = False
    checkpoint_id: str | None = None
    error: str | None = None


class GraphRunnerService:
    """Service executing LangGraph state transitions and managing task dispatches.

    Enforces:
    1. Parent-first row locking and status fencing.
    2. Cancellation, deadline, and context-version barriers.
    3. Replay-safe checkpoint start and continuation using LangGraph checkpointer.
    4. Atomic Transaction 2: task insertion, matching TASK_DISPATCH outbox insertion,
       and WAITING_TASKS execution state transition.
    5. Terminal status transitions (SUCCEEDED, PARTIAL, FAILED).
    6. Bounded execution runtime suitable for AWS Lambda invocation budgets.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        default_graph: Any | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_runtime_seconds: float = DEFAULT_RUNNER_LEASE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._default_graph = default_graph
        if (
            default_graph is not None
            and hasattr(default_graph, "checkpointer")
            and getattr(default_graph.checkpointer, "session_factory", None) is None
        ):
            default_graph.checkpointer.session_factory = session_factory
        self._max_steps = max_steps
        self._max_runtime_seconds = max_runtime_seconds

    async def handle_execution_start(
        self,
        *,
        event: ExecutionStartEvent | dict[str, Any],
        graph: Any | None = None,
    ) -> GraphRunnerResult:
        """Handles an EXECUTION_START event.

        Initializes LangGraph thread, executes initial graph step,
        and atomically dispatches generated tasks or sets terminal status.
        """
        if isinstance(event, dict):
            event_obj = ExecutionStartEvent(
                execution_id=uuid.UUID(str(event["execution_id"])),
                event_id=uuid.UUID(str(event["event_id"])) if event.get("event_id") else None,
                workflow=str(event.get("workflow", "TRAVEL")),
                context_version=int(event.get("context_version", 1)),
                payload=event.get("payload") or event.get("payload_ref"),
            )
        else:
            event_obj = event

        active_graph = graph or self._default_graph
        if active_graph is None:
            raise GraphRunnerError("No graph provided and no default graph configured")

        execution_id = event_obj.execution_id
        now = datetime.now(UTC)
        runner_lease = uuid.uuid4()

        # Phase 1: Lock execution and evaluate barriers
        async with self._session_factory() as session:
            async with session.begin():
                exec_stmt = (
                    select(ExecutionModel)
                    .where(ExecutionModel.id == execution_id)
                    .with_for_update()
                )
                execution = (await session.execute(exec_stmt)).scalar_one_or_none()
                if execution is None:
                    raise ExecutionNotFoundError(f"Execution {execution_id} not found")

                # Cancellation barrier
                if execution.status == "CANCELLED" or execution.cancelled_at is not None:
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status="CANCELLED",
                        graph_advanced=False,
                    )

                # Deadline barrier
                if execution.deadline_at and execution.deadline_at <= now:
                    if execution.status != "FAILED":
                        execution.status = "FAILED"
                        execution.updated_at = now
                        await self._record_terminal_failure(
                            session, execution_id, "Deadline exceeded before start"
                        )
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status="FAILED",
                        graph_advanced=False,
                        error="Execution deadline exceeded",
                    )

                # Context version barrier
                if event_obj.context_version < execution.context_version:
                    raise StaleContextError(
                        f"Event context version {event_obj.context_version} is older than execution version {execution.context_version}"
                    )

                # Duplicate / Replay check
                if execution.status in ("WAITING_TASKS", "SUCCEEDED", "PARTIAL", "FAILED"):
                    # Check if tasks already exist for this execution
                    task_stmt = select(TaskModel).where(TaskModel.execution_id == execution_id)
                    existing_tasks = (await session.execute(task_stmt)).scalars().all()
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status=execution.status,
                        tasks_dispatched=0,
                        task_ids=[t.id for t in existing_tasks],
                        graph_advanced=False,
                        checkpoint_id=execution.checkpoint_id,
                    )

                # Active runner lease fencing
                if (
                    execution.lease_token is not None
                    and execution.lease_expiry is not None
                    and execution.lease_expiry > now
                ):
                    # Another runner actively processing this execution
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status=execution.status,
                        tasks_dispatched=0,
                        graph_advanced=False,
                    )

                # Acquire lease and mark RUNNING
                execution.status = "RUNNING"
                execution.lease_token = runner_lease
                execution.lease_expiry = now + timedelta(seconds=self._max_runtime_seconds)
                execution.updated_at = now
                await record_job_event(
                    session,
                    execution_id=execution_id,
                    event_type="EXECUTION_STARTED",
                    safe_payload={"status": "RUNNING"},
                )

        # Phase 2: Execute LangGraph start outside database transaction
        config = {"configurable": {"thread_id": str(execution_id)}}
        initial_state = {
            "status": "RUNNING",
            "current_step": "start",
            "accepted_result_ids": [],
            "task_outputs": {},
            **(event_obj.payload or {}),
        }

        checkpointer = getattr(active_graph, "checkpointer", None)
        if checkpointer and hasattr(checkpointer, "session_factory") and checkpointer.session_factory is None:
            checkpointer.session_factory = self._session_factory
        if checkpointer and hasattr(checkpointer, "discard"):
            checkpointer.discard(str(execution_id))

        try:
            # Check if thread state already exists in checkpointer
            existing_state = await active_graph.aget_state(config=config)
            if existing_state and existing_state.values:
                # Thread already initialized, continue or step
                await active_graph.ainvoke(None, config=config)
            else:
                await active_graph.ainvoke(initial_state, config=config)

            post_state = await active_graph.aget_state(config=config)
        except Exception as e:
            logger.exception("LangGraph execution error during start: %s", e)
            if checkpointer and hasattr(checkpointer, "discard"):
                checkpointer.discard(str(execution_id))
            await self._release_lease_and_fail(execution_id, runner_lease, str(e))
            raise

        # Phase 3: Process resulting state and commit Transaction 2
        return await self._apply_graph_outcome(
            execution_id=execution_id,
            runner_lease=runner_lease,
            post_state=post_state,
            checkpointer=getattr(active_graph, "checkpointer", None),
        )

    async def handle_execution_resume(
        self,
        *,
        event: ExecutionResumeEvent | dict[str, Any],
        graph: Any | None = None,
    ) -> GraphRunnerResult:
        """Handles an EXECUTION_RESUME event.

        Resumes LangGraph execution from persisted checkpoint,
        dispatches subsequent tasks or transitions to terminal state.
        """
        if isinstance(event, dict):
            event_obj = ExecutionResumeEvent(
                execution_id=uuid.UUID(str(event["execution_id"])),
                event_id=uuid.UUID(str(event["event_id"])) if event.get("event_id") else None,
                triggered_by_task_id=(
                    uuid.UUID(str(event["triggered_by_task_id"]))
                    if event.get("triggered_by_task_id")
                    else None
                ),
                action=str(event.get("action", "resume_graph")),
                context_version=int(event.get("context_version", 1)),
            )
        else:
            event_obj = event

        active_graph = graph or self._default_graph
        if active_graph is None:
            raise GraphRunnerError("No graph provided and no default graph configured")

        execution_id = event_obj.execution_id
        now = datetime.now(UTC)
        runner_lease = uuid.uuid4()

        # Phase 1: Lock execution and evaluate barriers
        async with self._session_factory() as session:
            async with session.begin():
                exec_stmt = (
                    select(ExecutionModel)
                    .where(ExecutionModel.id == execution_id)
                    .with_for_update()
                )
                execution = (await session.execute(exec_stmt)).scalar_one_or_none()
                if execution is None:
                    raise ExecutionNotFoundError(f"Execution {execution_id} not found")

                # Cancellation barrier
                if execution.status == "CANCELLED" or execution.cancelled_at is not None:
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status="CANCELLED",
                        graph_advanced=False,
                    )

                # Deadline barrier
                if execution.deadline_at and execution.deadline_at <= now:
                    if execution.status != "FAILED":
                        execution.status = "FAILED"
                        execution.updated_at = now
                        await self._record_terminal_failure(
                            session, execution_id, "Deadline exceeded before resume"
                        )
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status="FAILED",
                        graph_advanced=False,
                        error="Execution deadline exceeded",
                    )

                # Terminal barrier: already finished
                if execution.status in ("SUCCEEDED", "PARTIAL", "FAILED"):
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status=execution.status,
                        tasks_dispatched=0,
                        graph_advanced=False,
                        checkpoint_id=execution.checkpoint_id,
                    )

                # Check if still waiting for tasks
                if execution.status == "WAITING_TASKS":
                    active_tasks_res = await session.execute(
                        select(sa.func.count(TaskModel.id)).where(
                            TaskModel.execution_id == execution_id,
                            TaskModel.status.in_(["PENDING", "CLAIMED", "RUNNING"]),
                        )
                    )
                    active_count = active_tasks_res.scalar() or 0
                    if active_count > 0:
                        # Cannot resume yet, tasks still active
                        return GraphRunnerResult(
                            execution_id=execution_id,
                            status="WAITING_TASKS",
                            tasks_dispatched=0,
                            graph_advanced=False,
                        )

                # Active runner lease fencing
                if (
                    execution.lease_token is not None
                    and execution.lease_expiry is not None
                    and execution.lease_expiry > now
                ):
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status=execution.status,
                        tasks_dispatched=0,
                        graph_advanced=False,
                    )

                # Acquire lease and set RUNNING
                execution.status = "RUNNING"
                execution.lease_token = runner_lease
                execution.lease_expiry = now + timedelta(seconds=self._max_runtime_seconds)
                execution.updated_at = now
                await record_job_event(
                    session,
                    execution_id=execution_id,
                    event_type="EXECUTION_RESUMING",
                    safe_payload={"status": "RUNNING", "action": "resume_graph"},
                )

        # Phase 2: Execute LangGraph continuation outside database transaction
        config = {"configurable": {"thread_id": str(execution_id)}}
        checkpointer = getattr(active_graph, "checkpointer", None)
        if checkpointer and hasattr(checkpointer, "session_factory") and checkpointer.session_factory is None:
            checkpointer.session_factory = self._session_factory
        if checkpointer and hasattr(checkpointer, "discard"):
            checkpointer.discard(str(execution_id))

        try:
            post_state = await active_graph.aget_state(config=config)
            # Only invoke continuation if there are pending nodes to execute
            if post_state and post_state.next:
                await active_graph.ainvoke(None, config=config)
                post_state = await active_graph.aget_state(config=config)
        except Exception as e:
            logger.exception("LangGraph execution error during resume: %s", e)
            if checkpointer and hasattr(checkpointer, "discard"):
                checkpointer.discard(str(execution_id))
            await self._release_lease_and_fail(execution_id, runner_lease, str(e))
            raise

        # Phase 3: Process resulting state and commit outcome
        return await self._apply_graph_outcome(
            execution_id=execution_id,
            runner_lease=runner_lease,
            post_state=post_state,
            checkpointer=getattr(active_graph, "checkpointer", None),
        )

    async def _apply_graph_outcome(
        self,
        *,
        execution_id: uuid.UUID,
        runner_lease: uuid.UUID,
        post_state: Any,
        checkpointer: Any | None = None,
    ) -> GraphRunnerResult:
        """Applies graph outcome: dispatches tasks or sets terminal status under lock."""
        now = datetime.now(UTC)
        state_values = dict(post_state.values) if post_state and post_state.values else {}
        checkpoint_id = (
            post_state.config.get("configurable", {}).get("checkpoint_id")
            if post_state and post_state.config
            else None
        )
        has_next_nodes = bool(post_state and post_state.next)
        pending_tasks = state_values.get("pending_tasks", [])

        async with self._session_factory() as session:
            async with session.begin():
                exec_stmt = (
                    select(ExecutionModel)
                    .where(ExecutionModel.id == execution_id)
                    .with_for_update()
                )
                execution = (await session.execute(exec_stmt)).scalar_one_or_none()
                if execution is None:
                    raise ExecutionNotFoundError(f"Execution {execution_id} not found")

                # Verify lease ownership
                if execution.lease_token != runner_lease:
                    # Lease was lost or overwritten by another worker
                    if checkpointer and hasattr(checkpointer, "discard"):
                        checkpointer.discard(str(execution_id))
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status=execution.status,
                        tasks_dispatched=0,
                        graph_advanced=False,
                    )

                # Cancellation barrier check
                if execution.status == "CANCELLED" or execution.cancelled_at is not None:
                    execution.lease_token = None
                    execution.lease_expiry = None
                    if checkpointer and hasattr(checkpointer, "discard"):
                        checkpointer.discard(str(execution_id))
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status="CANCELLED",
                        graph_advanced=False,
                    )

                # Deadline barrier check
                if execution.deadline_at and execution.deadline_at <= now:
                    execution.status = "FAILED"
                    execution.lease_token = None
                    execution.lease_expiry = None
                    execution.updated_at = now
                    if checkpointer and hasattr(checkpointer, "discard"):
                        checkpointer.discard(str(execution_id))
                    await self._record_terminal_failure(
                        session, execution_id, "Deadline exceeded during execution"
                    )
                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status="FAILED",
                        graph_advanced=False,
                        error="Deadline exceeded during execution",
                    )

                if checkpoint_id:
                    execution.checkpoint_id = checkpoint_id

                # Case A: Graph produced tasks to dispatch (Transaction 2)
                if pending_tasks:
                    existing_keys_stmt = select(TaskModel.idempotency_key).where(
                        TaskModel.execution_id == execution_id
                    )
                    existing_keys = set((await session.execute(existing_keys_stmt)).scalars().all())

                    dispatched_ids: list[uuid.UUID] = []
                    for task_spec in pending_tasks:
                        task_payload = dict(task_spec.get("payload", {}))
                        input_hash = hashlib.sha256(
                            json.dumps(task_payload, sort_keys=True).encode("utf-8")
                        ).hexdigest()
                        idemp_key = (
                            task_spec.get("idempotency_key")
                            or f"{execution_id}:{task_spec.get('action')}:{input_hash[:16]}"
                        )

                        if idemp_key in existing_keys:
                            # Deduplicate tasks already created
                            continue

                        task_id = uuid.uuid4()
                        task_deadline = execution.deadline_at or (
                            now + timedelta(seconds=int(task_spec.get("deadline_seconds", 3600)))
                        )
                        task_row = TaskModel(
                            id=task_id,
                            execution_id=execution_id,
                            worker=str(task_spec.get("worker", "EVIDENCE")),
                            action=str(task_spec.get("action", "collect")),
                            payload_ref=task_payload,
                            input_hash=input_hash,
                            idempotency_key=idemp_key,
                            attempt=0,
                            max_attempts=int(task_spec.get("max_attempts", 3)),
                            context_version=execution.context_version,
                            status="PENDING",
                            lease_token=None,
                            lease_expiry=None,
                            deadline_at=task_deadline,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(task_row)

                        dispatch_payload = {
                            "schema_version": "1.0",
                            "execution_id": str(execution_id),
                            "task_id": str(task_id),
                            "worker": task_row.worker,
                            "action": task_row.action,
                            "attempt": 0,
                            "context_version": execution.context_version,
                            "payload": task_payload,
                        }
                        dispatch_event = OutboxEventModel(
                            id=uuid.uuid4(),
                            execution_id=execution_id,
                            aggregate_id=execution_id,
                            task_id=task_id,
                            kind="TASK_DISPATCH",
                            event_type="TASK_DISPATCH",
                            payload_ref=dispatch_payload,
                            payload=dispatch_payload,
                            due_at=now,
                            attempts=0,
                            created_at=now,
                        )
                        session.add(dispatch_event)
                        dispatched_ids.append(task_id)
                        existing_keys.add(idemp_key)

                    execution.status = "WAITING_TASKS"
                    execution.lease_token = None
                    execution.lease_expiry = None
                    execution.updated_at = now

                    await record_job_event(
                        session,
                        execution_id=execution_id,
                        event_type="TASKS_DISPATCHED",
                        safe_payload={
                            "count": len(dispatched_ids),
                            "status": "WAITING_TASKS",
                        },
                    )

                    if checkpointer and hasattr(checkpointer, "flush"):
                        await checkpointer.flush(session, str(execution_id))

                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status="WAITING_TASKS",
                        tasks_dispatched=len(dispatched_ids),
                        task_ids=dispatched_ids,
                        graph_advanced=True,
                        checkpoint_id=checkpoint_id,
                    )

                # Case B: Graph reached completion with no remaining nodes
                if not has_next_nodes:
                    terminal_status = state_values.get("status", "SUCCEEDED")
                    if terminal_status not in ("SUCCEEDED", "PARTIAL", "FAILED"):
                        terminal_status = "SUCCEEDED"

                    execution.status = terminal_status
                    execution.lease_token = None
                    execution.lease_expiry = None
                    execution.updated_at = now

                    session.add(
                        OutboxEventModel(
                            id=uuid.uuid4(),
                            execution_id=execution_id,
                            aggregate_id=execution_id,
                            task_id=None,
                            kind=f"EXECUTION_{terminal_status}",
                            event_type=f"EXECUTION_{terminal_status}",
                            payload_ref={
                                "execution_id": str(execution_id),
                                "status": terminal_status,
                            },
                            payload={"execution_id": str(execution_id), "status": terminal_status},
                            due_at=now,
                            attempts=0,
                            created_at=now,
                        )
                    )

                    await record_job_event(
                        session,
                        execution_id=execution_id,
                        event_type=f"EXECUTION_{terminal_status}",
                        safe_payload={"status": terminal_status},
                    )

                    if checkpointer and hasattr(checkpointer, "flush"):
                        await checkpointer.flush(session, str(execution_id))

                    return GraphRunnerResult(
                        execution_id=execution_id,
                        status=terminal_status,
                        tasks_dispatched=0,
                        graph_advanced=True,
                        checkpoint_id=checkpoint_id,
                    )

                # Case C: Graph paused without pending tasks
                execution.status = "WAITING_TASKS"
                execution.lease_token = None
                execution.lease_expiry = None
                execution.updated_at = now

                if checkpointer and hasattr(checkpointer, "flush"):
                    await checkpointer.flush(session, str(execution_id))

                return GraphRunnerResult(
                    execution_id=execution_id,
                    status="WAITING_TASKS",
                    tasks_dispatched=0,
                    graph_advanced=True,
                    checkpoint_id=checkpoint_id,
                )

    async def _release_lease_and_fail(
        self,
        execution_id: uuid.UUID,
        runner_lease: uuid.UUID,
        error_msg: str,
    ) -> None:
        """Fails the execution and releases active runner lease on unhandled error."""
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    exec_stmt = (
                        select(ExecutionModel)
                        .where(ExecutionModel.id == execution_id)
                        .with_for_update()
                    )
                    execution = (await session.execute(exec_stmt)).scalar_one_or_none()
                    if execution and execution.lease_token == runner_lease:
                        execution.status = "FAILED"
                        execution.lease_token = None
                        execution.lease_expiry = None
                        execution.updated_at = now
                        await self._record_terminal_failure(session, execution_id, error_msg)
        except Exception as e:
            logger.error("Failed to release lease on failure for execution %s: %s", execution_id, e)

    async def _record_terminal_failure(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
        reason: str,
    ) -> None:
        """Records terminal failure outbox event and job timeline event."""
        now = datetime.now(UTC)
        session.add(
            OutboxEventModel(
                id=uuid.uuid4(),
                execution_id=execution_id,
                aggregate_id=execution_id,
                task_id=None,
                kind="EXECUTION_FAILED",
                event_type="EXECUTION_FAILED",
                payload_ref={"execution_id": str(execution_id), "reason": reason},
                payload={"execution_id": str(execution_id), "reason": reason},
                due_at=now,
                attempts=0,
                created_at=now,
            )
        )
        await record_job_event(
            session,
            execution_id=execution_id,
            event_type="EXECUTION_FAILED",
            safe_payload={"status": "FAILED", "reason": reason},
        )
