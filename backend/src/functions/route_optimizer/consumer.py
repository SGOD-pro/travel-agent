"""Dedicated Lambda consumer for route optimizer TASK_DISPATCH messages.

Consumes SQS messages, claims tasks under lease fencing, validates parent execution
fences, and executes route optimization under WorkerFencingService without importing
unrelated workers or presentation layers.
"""

from __future__ import annotations

import logging
from typing import Any

from config.db import get_session_factory
from functions.orchestrator.execution.fencing import (
    ExecutionEngineError,
    ParentCancelledError,
    ParentTerminalError,
    StaleQueueMessageError,
    TaskAlreadyTerminalError,
    TaskClaimConflictError,
    TaskLeaseActiveError,
)
from functions.route_optimizer.closures import ClosureAvoidanceService
from functions.route_optimizer.multimodal import MultimodalTransitionService
from functions.route_optimizer.service import (
    RouteOptimizerWorkerService,
    parse_optimizer_dispatch_message,
)
from functions.route_optimizer.solver import OrToolsRoutingSolver

logger = logging.getLogger(__name__)


async def process_record(
    record: dict[str, Any],
    session_factory: Any,
    *,
    solver: OrToolsRoutingSolver | None = None,
    multimodal_service: MultimodalTransitionService | None = None,
    valhalla_client: Any | None = None,
    closure_service: ClosureAvoidanceService | None = None,
) -> None:
    """Parses a TASK_DISPATCH message and executes route optimization under lease fencing."""
    envelope = parse_optimizer_dispatch_message(record)

    if envelope.worker.upper() not in ("OPTIMIZER", "ROUTE_OPTIMIZER") or envelope.action not in (
        "solve",
        "replan",
    ):
        logger.warning(
            "Route optimizer worker ignoring message for worker=%s action=%s",
            envelope.worker,
            envelope.action,
        )
        return

    async with session_factory() as session:
        service = RouteOptimizerWorkerService(
            session,
            solver=solver,
            multimodal_service=multimodal_service,
            valhalla_client=valhalla_client,
            closure_service=closure_service,
        )
        try:
            await service.execute_task(envelope)
        except (
            TaskClaimConflictError,
            TaskLeaseActiveError,
            TaskAlreadyTerminalError,
            StaleQueueMessageError,
            ParentCancelledError,
            ParentTerminalError,
        ) as exc:
            logger.info("Safely discarding task dispatch for %s: %s", envelope.task_id, exc)
            return
        except ExecutionEngineError:
            logger.error(
                "Terminal execution engine error for optimizer task %s; failing message for DLQ",
                envelope.task_id,
                exc_info=True,
            )
            raise


async def process_batch(
    event: dict[str, Any],
    context: Any = None,
    session_factory: Any | None = None,
    *,
    solver: OrToolsRoutingSolver | None = None,
    multimodal_service: MultimodalTransitionService | None = None,
    valhalla_client: Any | None = None,
    closure_service: ClosureAvoidanceService | None = None,
) -> list[dict[str, str]]:
    """Processes an SQS batch and returns failed item identifiers for partial batch responses."""
    if session_factory is None:
        session_factory = get_session_factory()

    records = event.get("Records", [])
    if not records:
        return []

    failures: list[dict[str, str]] = []

    for record in records:
        message_id = record.get("messageId", "")
        if context and hasattr(context, "get_remaining_time_in_millis"):
            if context.get_remaining_time_in_millis() < 5000:
                logger.warning(
                    "Lambda remaining time under 5000ms, failing message %s to trigger safe SQS redrive",
                    message_id,
                )
                if message_id:
                    failures.append({"itemIdentifier": message_id})
                continue

        try:
            await process_record(
                record,
                session_factory,
                solver=solver,
                multimodal_service=multimodal_service,
                valhalla_client=valhalla_client,
                closure_service=closure_service,
            )
        except Exception as exc:
            logger.error("Failed processing record %s: %s", message_id, exc, exc_info=True)
            if message_id:
                failures.append({"itemIdentifier": message_id})

    return failures


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entrypoint handler for SQS event source mapping."""
    import asyncio

    failures = asyncio.run(process_batch(event, context))
    return {"batchItemFailures": failures}
