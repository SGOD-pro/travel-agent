"""Lambda handler for TASK_COMPLETED and TASK_FAILED completion receipt SQS consumer."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from config.db import get_session_factory
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.reconciliation import (
    CompletionReconciliationService,
    TaskCompletionEvent,
)
from functions.orchestrator.graphs.trip_planner.graph import build_trip_planning_graph

logger = logging.getLogger(__name__)


async def process_record(
    record: dict[str, Any], reconciler: CompletionReconciliationService
) -> None:
    """Parses and validates a single SQS record and delegates to the reconciler."""
    body_str = record.get("body", "{}")
    try:
        body = json.loads(body_str)
    except json.JSONDecodeError as e:
        logger.error(f"Malformed SQS message body: {body_str}")
        raise ValueError("Malformed JSON") from e

    kind = body.get("kind")
    if kind not in ("TASK_COMPLETED", "TASK_FAILED", "TASK_CANCELLED"):
        logger.error(f"Unsupported event kind: {kind}")
        raise ValueError(f"Unsupported kind: {kind}")

    event = TaskCompletionEvent(
        event_id=uuid.UUID(body.get("id")) if body.get("id") else uuid.uuid4(),
        task_id=uuid.UUID(body.get("task_id")),
        execution_id=uuid.UUID(body.get("execution_id")),
        attempt=body.get("attempt", 1),
        event_type=kind,
        result_id=body.get("result_id"),
        output=body.get("output"),
        error=body.get("error"),
    )

    await reconciler.reconcile_task_event(event=event)


async def process_batch(
    event: dict[str, Any],
    context: Any,
    reconciler: CompletionReconciliationService | None = None,
) -> list[dict[str, str]]:
    """Processes an SQS batch and returns item identifiers for failed records."""
    failures: list[dict[str, str]] = []

    if reconciler is None:
        session_factory = get_session_factory()
        checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
        graph = build_trip_planning_graph().compile(checkpointer=checkpointer)
        reconciler = CompletionReconciliationService(session_factory, default_graph=graph)

    records = event.get("Records", [])
    if not records:
        return []

    over_budget: list[dict[str, str]] = []
    processable: list[dict[str, Any]] = []

    for record in records:
        message_id = record.get("messageId", "")
        if context and hasattr(context, "get_remaining_time_in_millis"):
            if context.get_remaining_time_in_millis() < 2000:
                logger.warning(
                    "Lambda remaining time too low (%dms), failing remaining message %s to trigger retry",
                    context.get_remaining_time_in_millis(),
                    message_id,
                )
                over_budget.append({"itemIdentifier": message_id})
                continue
        processable.append(record)

    async def _process_one(record: dict[str, Any]) -> dict[str, str] | None:
        message_id = record.get("messageId", "")
        try:
            await process_record(record, reconciler)
            return None
        except Exception as e:
            logger.exception("Error processing record %s: %s", message_id, e)
            return {"itemIdentifier": message_id} if message_id else None

    results = await asyncio.gather(*[_process_one(r) for r in processable])
    failures = over_budget + [r for r in results if r is not None]

    return failures


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Standard AWS Lambda entrypoint for Completion Receipt SQS consumer."""
    failures = asyncio.run(process_batch(event, context))
    return {"batchItemFailures": failures}
