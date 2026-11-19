"""Lambda handler for EXECUTION_START and EXECUTION_RESUME SQS consumer."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from config.db import get_session_factory
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.runner import (
    ExecutionResumeEvent,
    ExecutionStartEvent,
    GraphRunnerService,
)
from functions.orchestrator.graphs.trip_planner import (
    compile_trip_planning_workflow,
)

logger = logging.getLogger(__name__)


async def process_record(record: dict[str, Any], runner: GraphRunnerService) -> None:
    """Parses and validates a single SQS record and delegates to the runner."""
    body_str = record.get("body", "{}")
    try:
        body = json.loads(body_str)
    except json.JSONDecodeError as e:
        logger.error("Malformed SQS message body: %s", body_str)
        raise ValueError("Malformed JSON") from e

    kind = body.get("kind")
    execution_id_str = body.get("execution_id")
    if not kind or not execution_id_str:
        logger.error("Missing required fields 'kind' or 'execution_id' in body: %s", body)
        raise ValueError("Missing required fields")

    execution_id = uuid.UUID(str(execution_id_str))
    raw_event_id = body.get("event_id") or body.get("id")
    event_id = uuid.UUID(str(raw_event_id)) if raw_event_id else None

    raw_payload = body.get("payload")
    if raw_payload is None:
        raw_payload = body.get("payload_ref")
    if raw_payload is not None and not isinstance(raw_payload, dict):
        logger.warning(
            "Non-dict payload encountered in SQS message for execution %s: %s",
            execution_id,
            type(raw_payload).__name__,
        )
    payload_dict = raw_payload if isinstance(raw_payload, dict) else {}

    raw_workflow = body.get("workflow") or payload_dict.get("workflow")
    if not raw_workflow:
        logger.warning(
            "Workflow not specified in SQS record body or payload for execution %s; defaulting to 'TRAVEL'",
            execution_id,
        )
        workflow = "TRAVEL"
    else:
        workflow = str(raw_workflow)

    raw_context_version = body.get("context_version")
    if raw_context_version is None:
        raw_context_version = payload_dict.get("context_version", 1)
    context_version = int(raw_context_version)

    if kind == "EXECUTION_START":
        event_start = ExecutionStartEvent(
            execution_id=execution_id,
            event_id=event_id,
            workflow=workflow,
            context_version=context_version,
            payload=raw_payload if isinstance(raw_payload, dict) else None,
        )
        await runner.handle_execution_start(event=event_start)

    elif kind == "EXECUTION_RESUME":
        raw_task_id = (
            body.get("task_id")
            or payload_dict.get("triggered_by_task_id")
            or payload_dict.get("task_id")
        )
        action = str(body.get("action") or payload_dict.get("action") or "resume_graph")
        event_resume = ExecutionResumeEvent(
            execution_id=execution_id,
            event_id=event_id,
            triggered_by_task_id=uuid.UUID(str(raw_task_id)) if raw_task_id else None,
            action=action,
            context_version=context_version,
        )
        await runner.handle_execution_resume(event=event_resume)

    else:
        logger.error("Unsupported event kind: %s", kind)
        raise ValueError(f"Unsupported kind: {kind}")


async def process_batch(
    event: dict[str, Any],
    context: Any,
    runner: GraphRunnerService | None = None,
) -> list[dict[str, str]]:
    """Processes an SQS batch and returns item identifiers for failed records."""
    failures: list[dict[str, str]] = []

    if runner is None:
        session_factory = get_session_factory()
        checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
        trip_graph = compile_trip_planning_workflow(checkpointer=checkpointer)
        from functions.orchestrator.graphs.route_optimizer import (
            compile_route_optimizer_workflow,
        )

        route_graph = compile_route_optimizer_workflow(checkpointer=checkpointer)
        runner = GraphRunnerService(
            session_factory,
            default_graph=trip_graph,
            workflow_graphs={
                "ROUTE_OPTIMIZER_GRAPH": route_graph,
                "ROUTE_OPTIMIZATION": route_graph,
                "ROUTE_OPTIMIZER": route_graph,
                "TRAVEL": trip_graph,
            },
        )

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
            await process_record(record, runner)
            return None
        except Exception as e:
            logger.exception("Error processing record %s: %s", message_id, e)
            return {"itemIdentifier": message_id} if message_id else None

    results = await asyncio.gather(*[_process_one(r) for r in processable])
    failures = over_budget + [r for r in results if r is not None]

    return failures


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Standard AWS Lambda entrypoint for Graph Runner SQS consumer."""
    failures = asyncio.run(process_batch(event, context))
    return {"batchItemFailures": failures}
