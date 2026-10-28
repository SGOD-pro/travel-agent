"""Background outbox processor for local acceptance testing with injected MockValhallaClient.

Drives execution events through the complete platform vertical slice:
1. EXECUTION_START -> GraphRunnerService.handle_execution_start()
2. TASK_DISPATCH -> process_record(valhalla_client=MockValhallaClient())
3. TASK_COMPLETED -> CompletionReconciliationService.reconcile_task_event()
4. EXECUTION_RESUME -> GraphRunnerService.handle_execution_resume()
"""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from functions.orchestrator.execution import (
    CompletionReconciliationService,
    ExecutionModel,
    ExecutionResumeEvent,
    ExecutionStartEvent,
    GraphRunnerService,
    OutboxEventModel,
    TaskCompletionEvent,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.graphs.route_optimizer import (
    compile_route_optimizer_workflow,
)
from functions.route_optimizer.closures import ClosureAvoidanceService
from functions.route_optimizer.consumer import process_record
from functions.route_optimizer.fixtures import MockValhallaClient

logger = logging.getLogger(__name__)

from acceptance_database import (
    _resilient_getaddrinfo,
    validate_acceptance_database_url,
)

# Resilient DNS caching for PostgreSQL connectivity
socket.getaddrinfo = _resilient_getaddrinfo
from config.config import settings

DEFAULT_ALLOWED_OWNER_IDS = (
    uuid.UUID("00000000-0000-0000-0000-000000000001"),
    uuid.UUID("99999999-9999-9999-9999-999999999999"),
)

DEFAULT_ALLOWED_TRIP_IDS = (
    uuid.UUID("00000000-0000-0000-0000-000000000001"),
    uuid.UUID("40000000-0000-0000-0000-000000000001"),
    uuid.UUID("50000000-0000-0000-0000-000000000001"),
    uuid.UUID("60000000-0000-0000-0000-000000000001"),
    uuid.UUID("99999999-0000-0000-0000-000000000001"),
)


class LocalAcceptanceWorker:
    """Processes outbox events locally with injected routing provider for browser acceptance."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        valhalla_client: Any | None = None,
        closure_service: ClosureAvoidanceService | None = None,
        allowed_owner_ids: tuple[uuid.UUID, ...] | None = None,
        allowed_trip_ids: tuple[uuid.UUID, ...] | None = None,
    ) -> None:
        validate_acceptance_database_url(
            settings.ACCEPTANCE_DATABASE_URL,
            settings.DATABASE_URL,
        )
        self._session_factory = session_factory
        self._valhalla_client = valhalla_client or MockValhallaClient()
        self._closure_service = closure_service or ClosureAvoidanceService()
        self._allowed_owner_ids = allowed_owner_ids or DEFAULT_ALLOWED_OWNER_IDS
        self._allowed_trip_ids = allowed_trip_ids or DEFAULT_ALLOWED_TRIP_IDS

        checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
        route_graph = compile_route_optimizer_workflow(checkpointer=checkpointer)

        self._runner = GraphRunnerService(
            session_factory,
            workflow_graphs={"ROUTE_OPTIMIZER_GRAPH": route_graph},
        )
        self._reconciler = CompletionReconciliationService(
            session_factory,
            workflow_graphs={"ROUTE_OPTIMIZER_GRAPH": route_graph},
        )
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def process_pending_events_once(self) -> int:
        """Processes all currently pending outbox events across graph, worker, and reconciliation."""
        now = datetime.now(UTC)
        recent_cutoff = now - timedelta(hours=2)
        processed_count = 0

        async with self._session_factory() as session:
            stmt = (
                select(OutboxEventModel)
                .join(ExecutionModel, OutboxEventModel.execution_id == ExecutionModel.id)
                .where(
                    OutboxEventModel.delivered_at.is_(None),
                    OutboxEventModel.failed_at.is_(None),
                    OutboxEventModel.created_at >= recent_cutoff,
                    ExecutionModel.owner_id.in_(self._allowed_owner_ids),
                    ExecutionModel.trip_id.in_(self._allowed_trip_ids),
                    ExecutionModel.workflow == "ROUTE_OPTIMIZER_GRAPH",
                )
                .order_by(OutboxEventModel.created_at.asc())
                .limit(20)
            )
            res = await session.execute(stmt)
            events = list(res.scalars().all())

        for evt in events:
            kind = evt.kind
            exec_id = evt.execution_id
            payload = evt.payload_ref or evt.payload or {}

            try:
                if kind == "EXECUTION_START":
                    start_evt = ExecutionStartEvent(
                        execution_id=exec_id,
                        event_id=evt.id,
                        workflow=payload.get("workflow", "ROUTE_OPTIMIZER_GRAPH"),
                        context_version=payload.get("context_version", 1),
                        payload=payload,
                    )
                    await self._runner.handle_execution_start(event=start_evt)
                    await self._mark_delivered(evt.id)
                    processed_count += 1

                elif kind == "TASK_DISPATCH":
                    msg = evt.payload_ref or evt.payload or {}
                    if not isinstance(msg, dict):
                        msg = {}
                    else:
                        msg = dict(msg)
                    if "task_id" not in msg and evt.task_id:
                        msg["task_id"] = str(evt.task_id)
                    if "execution_id" not in msg and exec_id:
                        msg["execution_id"] = str(exec_id)
                    await process_record(
                        msg,
                        self._session_factory,
                        valhalla_client=self._valhalla_client,
                        closure_service=self._closure_service,
                    )
                    await self._mark_delivered(evt.id)
                    processed_count += 1

                elif kind == "TASK_COMPLETED":
                    result_id = None
                    attempt = 0
                    async with self._session_factory() as session:
                        if evt.task_id:
                            task_stmt = select(TaskModel).where(TaskModel.id == evt.task_id)
                            task_row = (await session.execute(task_stmt)).scalar_one_or_none()
                            if task_row:
                                result_id = task_row.result_id
                                attempt = task_row.attempt

                        if result_id is None:
                            res_stmt = (
                                select(WorkerResultModel)
                                .where(WorkerResultModel.execution_id == exec_id)
                                .order_by(WorkerResultModel.created_at.desc())
                                .limit(1)
                            )
                            wr = (await session.execute(res_stmt)).scalar_one_or_none()
                            if wr:
                                result_id = wr.id
                                attempt = wr.attempt

                    if result_id is not None:
                        comp_evt = TaskCompletionEvent(
                            event_id=evt.id,
                            task_id=evt.task_id or uuid.uuid4(),
                            execution_id=exec_id,
                            attempt=attempt,
                            event_type=evt.event_type or "TASK_COMPLETED",
                            result_id=result_id,
                        )
                        await self._reconciler.reconcile_task_event(event=comp_evt)
                    await self._mark_delivered(evt.id)
                    processed_count += 1

                elif kind == "EXECUTION_RESUME":
                    resume_evt = ExecutionResumeEvent(
                        execution_id=exec_id,
                        event_id=evt.id,
                        triggered_by_task_id=evt.task_id,
                        context_version=payload.get("context_version", 1),
                    )
                    await self._runner.handle_execution_resume(event=resume_evt)
                    await self._mark_delivered(evt.id)
                    processed_count += 1

                else:
                    # Terminal notification events or non-actionable outbox kinds (EXECUTION_SUCCEEDED, EXECUTION_FAILED, etc.)
                    await self._mark_delivered(evt.id)
                    processed_count += 1

            except Exception as e:
                logger.error("Error processing acceptance outbox event %s (%s): %s", evt.id, kind, e)
                async with self._session_factory() as session:
                    update_stmt = (
                        select(OutboxEventModel).where(OutboxEventModel.id == evt.id)
                    )
                    row = (await session.execute(update_stmt)).scalar_one_or_none()
                    if row:
                        row.last_error = str(e)
                        row.failed_at = now
                        await session.commit()

        return processed_count

    async def _mark_delivered(self, event_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            stmt = select(OutboxEventModel).where(OutboxEventModel.id == event_id)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row:
                row.delivered_at = datetime.now(UTC)
                await session.commit()

    async def run_loop(self, poll_interval_s: float = 0.25) -> None:
        """Background execution loop polling and executing outbox events continuously."""
        self._running = True
        while self._running:
            try:
                processed = await self.process_pending_events_once()
                if processed == 0:
                    await asyncio.sleep(poll_interval_s)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Unexpected error in acceptance worker loop: %s", e)
                await asyncio.sleep(1.0)

    def start(self, poll_interval_s: float = 0.25) -> asyncio.Task[None]:
        """Starts worker in background task."""
        self._task = asyncio.create_task(self.run_loop(poll_interval_s))
        return self._task

    def stop(self) -> None:
        """Stops the worker."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
