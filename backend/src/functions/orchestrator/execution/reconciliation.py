"""LangGraph result application and checkpoint reconciliation service conforming to spec 0001."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from functions.orchestrator.execution.fencing import (
    ExecutionEngineError,
    ExecutionMismatchError,
    ExecutionNotFoundError,
    TaskNotFoundError,
)
from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import (
    CompletionReceiptModel,
    ExecutionModel,
    OutboxEventModel,
    TaskModel,
    WorkerResultModel,
)

DEFAULT_RECONCILIATION_BATCH_SIZE: int = 10
DEFAULT_MAX_RECONCILIATION_BATCH_SIZE: int = 100
DEFAULT_MAX_RECONCILIATION_RUNTIME_SECONDS: float = 25.0
DEFAULT_STALLED_RECEIPT_INTERVAL_SECONDS: int = 60


class ReconciliationError(ExecutionEngineError):
    """Base exception for reconciliation errors."""


class ReceiptDeduplicationError(ReconciliationError):
    """Raised when duplicate receipt validation fails."""


class ReceiptNotFoundError(ReconciliationError):
    """Raised when the specified receipt is not found."""


class WorkerResultNotFoundError(ReconciliationError):
    """Raised when the specified worker result is missing."""


class GraphStateError(ReconciliationError):
    """Raised when LangGraph checkpoint operations encounter errors."""


class ExecutionState(TypedDict, total=False):
    """LangGraph thread execution state contract."""

    accepted_result_ids: list[str]
    task_outputs: dict[str, Any]
    current_step: str
    status: str
    pending_tasks: list[dict[str, Any]]


@dataclass
class TaskCompletionEvent:
    """Incoming task completion or terminal outcome event."""

    event_id: uuid.UUID
    task_id: uuid.UUID
    execution_id: uuid.UUID
    attempt: int
    event_type: str = "TASK_COMPLETED"  # TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED
    result_id: uuid.UUID | None = None
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass
class ReconciliationResult:
    """Result of reconciling a single task completion receipt."""

    event_id: uuid.UUID
    execution_id: uuid.UUID
    task_id: uuid.UUID
    status: str
    checkpoint_updated: bool = False
    graph_resumed: bool = False
    resume_event_id: uuid.UUID | None = None
    error: str | None = None


@dataclass
class ReconciliationRunSummary:
    """Summary of scheduled completion reconciliation loop."""

    batches_processed: int = 0
    total_scanned: int = 0
    total_applied: int = 0
    total_skipped_already_applied: int = 0
    total_failed: int = 0
    elapsed_seconds: float = 0.0
    terminated_by_timeout: bool = False


class CompletionReconciliationService:
    """Service managing completion receipt deduplication, LangGraph checkpoint

    reconciliation, and durable execution graph-resume actions.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        default_graph: Any | None = None,
        batch_size: int = DEFAULT_RECONCILIATION_BATCH_SIZE,
        max_batch_size: int = DEFAULT_MAX_RECONCILIATION_BATCH_SIZE,
        max_runtime_seconds: float = DEFAULT_MAX_RECONCILIATION_RUNTIME_SECONDS,
        stalled_interval_seconds: int = DEFAULT_STALLED_RECEIPT_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._default_graph = default_graph
        self._max_batch_size = max_batch_size
        self._batch_size = max(1, min(batch_size, max_batch_size))
        self._max_runtime_seconds = max(1.0, max_runtime_seconds)
        self._stalled_interval_seconds = max(1, stalled_interval_seconds)

    async def reconcile_task_event(
        self,
        *,
        event: TaskCompletionEvent | dict[str, Any],
        graph: Any | None = None,
    ) -> ReconciliationResult:
        """Reconciles an incoming task event against PostgreSQL tables and LangGraph checkpoint.

        Enforces:
        1. Receipt deduplication with resumption of pending/unapplied receipts.
        2. Parent-first row locking to serialize checkpoint mutations per execution.
        3. Barrier verification (cancellation, deadlines, terminal states).
        4. Validation against persisted task, attempt, context version, and worker results.
        5. Idempotent LangGraph checkpoint update using accepted result markers.
        6. Merging outputs without overwriting results from previously completed tasks.
        7. Persisting a durable graph-resume action when all tasks for the step complete.
        """
        if isinstance(event, dict):
            event_id = uuid.UUID(str(event["event_id"]))
            task_id = uuid.UUID(str(event["task_id"]))
            execution_id = uuid.UUID(str(event["execution_id"]))
            attempt = int(event.get("attempt", 1))
            event_type = str(event.get("event_type", event.get("kind", "TASK_COMPLETED")))
            result_id = (
                uuid.UUID(str(event["result_id"])) if event.get("result_id") is not None else None
            )
            event_output = event.get("output")
            event_error = event.get("error")
        else:
            event_id = event.event_id
            task_id = event.task_id
            execution_id = event.execution_id
            attempt = event.attempt
            event_type = event.event_type
            result_id = event.result_id
            event_output = event.output
            event_error = event.error

        active_graph = graph or self._default_graph
        if (
            active_graph is not None
            and hasattr(active_graph, "checkpointer")
            and getattr(active_graph.checkpointer, "session_factory", None) is None
        ):
            active_graph.checkpointer.session_factory = self._session_factory
        now = datetime.now(UTC)

        async with self._session_factory() as session:
            async with session.begin():
                # Phase 1: Serialized Parent Execution Locking (Executions -> Tasks)
                # Locking parent execution first guarantees zero deadlocks across concurrent
                # worker results and eliminates foreign key share lock deadlocks.
                exec_stmt = (
                    select(ExecutionModel)
                    .where(ExecutionModel.id == execution_id)
                    .with_for_update()
                )
                execution = (await session.execute(exec_stmt)).scalar_one_or_none()
                if execution is None:
                    raise ExecutionNotFoundError(f"Execution {execution_id} not found")

                task_stmt = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
                task = (await session.execute(task_stmt)).scalar_one_or_none()
                if task is None:
                    raise TaskNotFoundError(f"Task {task_id} not found")

                if task.execution_id != execution_id:
                    raise ExecutionMismatchError(
                        f"Task {task_id} belongs to execution {task.execution_id}, not {execution_id}"
                    )

                # Phase 2: Receipt Insertion and Deduplication
                receipt_sql = text("""
                    INSERT INTO completion_receipts (
                        event_id, task_id, execution_id, attempt, event_type, result_id, received_at
                    ) VALUES (
                        :event_id, :task_id, :execution_id, :attempt, :event_type, :result_id, :received_at
                    )
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING event_id;
                """)
                res = await session.execute(
                    receipt_sql,
                    {
                        "event_id": event_id,
                        "task_id": task_id,
                        "execution_id": execution_id,
                        "attempt": attempt,
                        "event_type": event_type,
                        "result_id": result_id,
                        "received_at": now,
                    },
                )
                inserted = res.mappings().first()

                if inserted is None:
                    # Duplicate event received: check if already applied or pending unapplied
                    check_stmt = (
                        select(CompletionReceiptModel)
                        .where(CompletionReceiptModel.event_id == event_id)
                        .with_for_update()
                    )
                    existing_receipt = (await session.execute(check_stmt)).scalar_one_or_none()
                    if existing_receipt and existing_receipt.applied_at is not None:
                        # Already applied: exit immediately with zero secondary transitions
                        return ReconciliationResult(
                            event_id=event_id,
                            execution_id=execution_id,
                            task_id=task_id,
                            status="ALREADY_APPLIED",
                            checkpoint_updated=False,
                            graph_resumed=False,
                        )
                    # If applied_at is NULL, resume pending receipt!

                # Phase 3: Barrier Checks
                # Cancellation Barrier: do not resume or update graph for cancelled execution
                if execution.status == "CANCELLED" or execution.cancelled_at is not None:
                    if hasattr(active_graph, "checkpointer") and hasattr(active_graph.checkpointer, "discard"):
                        active_graph.checkpointer.discard(str(execution_id))
                    await session.execute(
                        text(
                            "UPDATE completion_receipts SET applied_at = :now WHERE event_id = :event_id AND applied_at IS NULL"
                        ),
                        {"now": now, "event_id": event_id},
                    )
                    return ReconciliationResult(
                        event_id=event_id,
                        execution_id=execution_id,
                        task_id=task_id,
                        status="CANCELLED_BARRIER",
                        checkpoint_updated=False,
                        graph_resumed=False,
                    )

                # Deadline Barrier
                if execution.deadline_at <= now:
                    if hasattr(active_graph, "checkpointer") and hasattr(active_graph.checkpointer, "discard"):
                        active_graph.checkpointer.discard(str(execution_id))
                    if execution.status not in ("SUCCEEDED", "FAILED", "PARTIAL", "CANCELLED"):
                        execution.status = "FAILED"
                        execution.lease_token = None
                        execution.lease_expiry = None
                        execution.updated_at = now

                        existing_outbox_stmt = select(OutboxEventModel.id).where(
                            OutboxEventModel.execution_id == execution_id,
                            OutboxEventModel.kind == "EXECUTION_FAILED",
                        )
                        if not (await session.execute(existing_outbox_stmt)).scalar():
                            session.add(
                                OutboxEventModel(
                                    id=uuid.uuid4(),
                                    execution_id=execution_id,
                                    aggregate_id=execution_id,
                                    task_id=None,
                                    kind="EXECUTION_FAILED",
                                    event_type="EXECUTION_FAILED",
                                    payload_ref={
                                        "execution_id": str(execution_id),
                                        "reason": "Deadline exceeded during reconciliation",
                                    },
                                    payload={
                                        "execution_id": str(execution_id),
                                        "reason": "Deadline exceeded during reconciliation",
                                    },
                                    due_at=now,
                                    attempts=0,
                                    created_at=now,
                                )
                            )
                            await record_job_event(
                                session,
                                execution_id=execution_id,
                                event_type="EXECUTION_FAILED",
                                safe_payload={
                                    "status": "FAILED",
                                    "reason": "Deadline exceeded during reconciliation",
                                },
                            )
                    await session.execute(
                        text(
                            "UPDATE completion_receipts SET applied_at = :now WHERE event_id = :event_id AND applied_at IS NULL"
                        ),
                        {"now": now, "event_id": event_id},
                    )
                    return ReconciliationResult(
                        event_id=event_id,
                        execution_id=execution_id,
                        task_id=task_id,
                        status="DEADLINE_EXCEEDED",
                        checkpoint_updated=False,
                        graph_resumed=False,
                    )

                # Ineligible parent execution
                if execution.status in ("SUCCEEDED", "FAILED", "PARTIAL"):
                    await session.execute(
                        text(
                            "UPDATE completion_receipts SET applied_at = :now WHERE event_id = :event_id AND applied_at IS NULL"
                        ),
                        {"now": now, "event_id": event_id},
                    )
                    return ReconciliationResult(
                        event_id=event_id,
                        execution_id=execution_id,
                        task_id=task_id,
                        status="PARENT_TERMINAL",
                        checkpoint_updated=False,
                        graph_resumed=False,
                    )

                # Context version verification
                if execution.context_version != task.context_version:
                    await session.execute(
                        text(
                            "UPDATE completion_receipts SET applied_at = :now WHERE event_id = :event_id AND applied_at IS NULL"
                        ),
                        {"now": now, "event_id": event_id},
                    )
                    return ReconciliationResult(
                        event_id=event_id,
                        execution_id=execution_id,
                        task_id=task_id,
                        status="STALE_CONTEXT",
                        checkpoint_updated=False,
                        graph_resumed=False,
                    )

                # Attempt verification
                if attempt < task.attempt:
                    await session.execute(
                        text(
                            "UPDATE completion_receipts SET applied_at = :now WHERE event_id = :event_id AND applied_at IS NULL"
                        ),
                        {"now": now, "event_id": event_id},
                    )
                    return ReconciliationResult(
                        event_id=event_id,
                        execution_id=execution_id,
                        task_id=task_id,
                        status="STALE_ATTEMPT",
                        checkpoint_updated=False,
                        graph_resumed=False,
                    )

                # Phase 4: Validate Result / Outcome Payload
                if event_type == "TASK_COMPLETED":
                    if result_id is not None:
                        res_stmt = select(WorkerResultModel).where(
                            WorkerResultModel.id == result_id
                        )
                        worker_result = (await session.execute(res_stmt)).scalar_one_or_none()
                        if worker_result is None:
                            raise WorkerResultNotFoundError(f"WorkerResult {result_id} not found")
                        if (
                            worker_result.task_id != task_id
                            or worker_result.execution_id != execution_id
                        ):
                            raise ExecutionMismatchError("WorkerResult task/execution mismatch")
                        task_output = worker_result.output_ref or {}
                    else:
                        task_output = event_output or {}
                elif event_type == "TASK_FAILED":
                    task_output = {
                        "error": event_error or {"reason": "task_failed"},
                        "status": "FAILED",
                    }
                elif event_type == "TASK_CANCELLED":
                    task_output = {
                        "error": event_error or {"reason": "task_cancelled"},
                        "status": "CANCELLED",
                    }
                else:
                    task_output = event_output or {}

                # Phase 5: Replay-safe LangGraph Checkpoint State Update
                checkpoint_updated = False
                marker_id = str(result_id) if result_id is not None else f"{task_id}:{attempt}"

                if active_graph is not None:
                    config = {"configurable": {"thread_id": str(execution_id)}}
                    current_state = await active_graph.aget_state(config=config)
                    state_values = (
                        current_state.values if current_state and current_state.values else {}
                    )
                    accepted_markers = list(state_values.get("accepted_result_ids", []))
                    existing_outputs = dict(state_values.get("task_outputs", {}))

                    if marker_id in accepted_markers:
                        # Marker already accepted by checkpoint: bypass graph update
                        checkpoint_updated = False
                    else:
                        # Merge outputs and markers without overwriting previous results
                        merged_markers = accepted_markers + [marker_id]
                        merged_outputs = {**existing_outputs, str(task_id): task_output}
                        node_target = (
                            "worker_node"
                            if hasattr(active_graph, "nodes") and "worker_node" in active_graph.nodes
                            else None
                        )
                        await active_graph.aupdate_state(
                            config=config,
                            values={
                                "accepted_result_ids": merged_markers,
                                "task_outputs": merged_outputs,
                            },
                            as_node=node_target,
                        )
                        if hasattr(active_graph, "checkpointer") and hasattr(active_graph.checkpointer, "flush"):
                            await active_graph.checkpointer.flush(session, str(execution_id))
                        checkpoint_updated = True

                # Phase 6: Receipt Confirmation
                await session.execute(
                    text(
                        "UPDATE completion_receipts SET applied_at = :now WHERE event_id = :event_id AND applied_at IS NULL"
                    ),
                    {"now": now, "event_id": event_id},
                )

                # Phase 7: Check Execution Step Completion and Persist Durable Graph Resume Action
                remaining_tasks_res = await session.execute(
                    select(sa.func.count(TaskModel.id)).where(
                        TaskModel.execution_id == execution_id,
                        TaskModel.status.in_(["PENDING", "CLAIMED", "RUNNING"]),
                    )
                )
                remaining_count = remaining_tasks_res.scalar() or 0
                graph_resumed = False
                resume_event_id: uuid.UUID | None = None

                if remaining_count == 0:
                    failed_tasks_res = await session.execute(
                        select(sa.func.count(TaskModel.id)).where(
                            TaskModel.execution_id == execution_id,
                            TaskModel.status.in_(["FAILED", "CANCELLED"]),
                        )
                    )
                    failed_count = failed_tasks_res.scalar() or 0

                    if failed_count > 0:
                        # Tasks failed or cancelled: transition execution to FAILED so joins do not wait forever
                        if execution.status != "FAILED":
                            execution.status = "FAILED"
                            execution.updated_at = now
                            fail_event_id = uuid.uuid4()
                            session.add(
                                OutboxEventModel(
                                    id=fail_event_id,
                                    execution_id=execution_id,
                                    aggregate_id=execution_id,
                                    task_id=task_id,
                                    kind="EXECUTION_FAILED",
                                    event_type="EXECUTION_FAILED",
                                    payload_ref={
                                        "execution_id": str(execution_id),
                                        "reason": "tasks_failed_or_cancelled",
                                    },
                                    payload={
                                        "execution_id": str(execution_id),
                                        "reason": "tasks_failed_or_cancelled",
                                    },
                                    due_at=now,
                                    attempts=0,
                                    created_at=now,
                                )
                            )
                            await record_job_event(
                                session,
                                execution_id=execution_id,
                                event_type="EXECUTION_FAILED",
                                safe_payload={
                                    "status": "FAILED",
                                    "reason": "tasks_failed_or_cancelled",
                                },
                            )
                    elif execution.status == "WAITING_TASKS":
                        # All tasks completed successfully: persist durable graph-resume action
                        execution.status = "RUNNING"
                        execution.updated_at = now
                        resume_event_id = uuid.uuid4()
                        session.add(
                            OutboxEventModel(
                                id=resume_event_id,
                                execution_id=execution_id,
                                aggregate_id=execution_id,
                                task_id=task_id,
                                kind="EXECUTION_RESUME",
                                event_type="EXECUTION_RESUME",
                                payload_ref={
                                    "execution_id": str(execution_id),
                                    "action": "resume_graph",
                                    "triggered_by_task_id": str(task_id),
                                },
                                payload={
                                    "execution_id": str(execution_id),
                                    "action": "resume_graph",
                                    "triggered_by_task_id": str(task_id),
                                },
                                due_at=now,
                                attempts=0,
                                created_at=now,
                            )
                        )
                        await record_job_event(
                            session,
                            execution_id=execution_id,
                            event_type="GRAPH_RESUMED",
                            safe_payload={
                                "status": "RUNNING",
                                "action": "resume_graph",
                            },
                        )
                        graph_resumed = True

                result_status = (
                    "APPLIED"
                    if event_type == "TASK_COMPLETED"
                    else (
                        "FAILED_TASK_APPLIED"
                        if event_type == "TASK_FAILED"
                        else "CANCELLED_TASK_APPLIED"
                    )
                )

                return ReconciliationResult(
                    event_id=event_id,
                    execution_id=execution_id,
                    task_id=task_id,
                    status=result_status,
                    checkpoint_updated=checkpoint_updated,
                    graph_resumed=graph_resumed,
                    resume_event_id=resume_event_id,
                )

    async def reconcile_stalled_receipts(
        self,
        *,
        batch_size: int | None = None,
        execution_id: uuid.UUID | None = None,
        stalled_interval_seconds: int | None = None,
        graph: Any | None = None,
    ) -> list[ReconciliationResult]:
        """Scans for unapplied completion receipts older than threshold and reconciles them."""
        effective_batch_size = max(1, min(batch_size or self._batch_size, self._max_batch_size))
        threshold_seconds = stalled_interval_seconds or self._stalled_interval_seconds
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=threshold_seconds)

        scan_stmt = select(
            CompletionReceiptModel.event_id,
            CompletionReceiptModel.task_id,
            CompletionReceiptModel.execution_id,
            CompletionReceiptModel.attempt,
            CompletionReceiptModel.event_type,
            CompletionReceiptModel.result_id,
        ).where(
            CompletionReceiptModel.applied_at.is_(None),
            CompletionReceiptModel.received_at <= cutoff,
        )
        if execution_id is not None:
            scan_stmt = scan_stmt.where(CompletionReceiptModel.execution_id == execution_id)

        scan_stmt = scan_stmt.order_by(CompletionReceiptModel.received_at.asc()).limit(
            effective_batch_size
        )

        async with self._session_factory() as session:
            rows = (await session.execute(scan_stmt)).all()

        results: list[ReconciliationResult] = []
        for row in rows:
            event = TaskCompletionEvent(
                event_id=row.event_id,
                task_id=row.task_id,
                execution_id=row.execution_id,
                attempt=row.attempt,
                event_type=row.event_type,
                result_id=row.result_id,
            )
            res = await self.reconcile_task_event(event=event, graph=graph)
            results.append(res)

        return results

    async def run_reconciliation_loop(
        self,
        *,
        max_runtime_seconds: float | None = None,
        execution_id: uuid.UUID | None = None,
        batch_size: int | None = None,
        graph: Any | None = None,
    ) -> ReconciliationRunSummary:
        """Runs the scheduled reconciliation loop until backlog cleared or runtime budget expires.

        Bounds database lock and query waits using asyncio.wait_for with remaining time budget.
        """
        runtime_limit = max_runtime_seconds or self._max_runtime_seconds
        start_time = time.monotonic()
        summary = ReconciliationRunSummary()

        while True:
            elapsed = time.monotonic() - start_time
            remaining_time = runtime_limit - elapsed
            if remaining_time <= 0:
                summary.terminated_by_timeout = True
                break

            try:
                batch_results = await asyncio.wait_for(
                    self.reconcile_stalled_receipts(
                        batch_size=batch_size,
                        execution_id=execution_id,
                        graph=graph,
                    ),
                    timeout=remaining_time,
                )
            except TimeoutError:
                summary.terminated_by_timeout = True
                break

            if not batch_results:
                break

            summary.batches_processed += 1
            summary.total_scanned += len(batch_results)
            for res in batch_results:
                if res.status in (
                    "APPLIED",
                    "FAILED_TASK_APPLIED",
                    "CANCELLED_TASK_APPLIED",
                    "CANCELLED_BARRIER",
                    "DEADLINE_EXCEEDED",
                    "PARENT_TERMINAL",
                    "STALE_CONTEXT",
                    "STALE_ATTEMPT",
                ):
                    summary.total_applied += 1
                elif res.status == "ALREADY_APPLIED":
                    summary.total_skipped_already_applied += 1
                else:
                    summary.total_failed += 1

        summary.elapsed_seconds = time.monotonic() - start_time
        return summary
