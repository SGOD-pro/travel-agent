"""Lambda handler for EXECUTION_START and EXECUTION_RESUME SQS consumer."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select

from config.db import get_session_factory
from functions.api.service import build_api_composition_workflow
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.models import ExecutionModel
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
        logger.error(f"Malformed SQS message body: {body_str}")
        raise ValueError("Malformed JSON") from e

    kind = body.get("kind")
    execution_id_str = body.get("execution_id")
    if not kind or not execution_id_str:
        logger.error(f"Missing required fields 'kind' or 'execution_id' in body: {body}")
        raise ValueError("Missing required fields")

    execution_id = uuid.UUID(execution_id_str)
    event_id_value = body.get("event_id") or body.get("id")
    event_id = uuid.UUID(event_id_value) if event_id_value else None
    outbox_payload = body.get("payload") or body.get("payload_ref") or {}

    if kind == "EXECUTION_START":
        workflow = str(outbox_payload.get("workflow", body.get("workflow", "TRAVEL")))
        input_payload = outbox_payload.get("input", outbox_payload)
        active_graph = None
        if workflow == "API_COMPOSITION":
            checkpointer = runner.checkpointer
            if checkpointer is None:
                raise ValueError("Composition workflow requires a configured checkpointer")
            active_graph = build_api_composition_workflow(checkpointer=checkpointer)
        event_start = ExecutionStartEvent(
            execution_id=execution_id,
            event_id=event_id,
            workflow=workflow,
            context_version=int(outbox_payload.get("context_version", body.get("context_version", 1))),
            payload=input_payload,
        )
        await runner.handle_execution_start(event=event_start, graph=active_graph)

    elif kind == "EXECUTION_RESUME":
        task_id = body.get("task_id")
        resume_workflow: Any = outbox_payload.get("workflow")
        if resume_workflow is None:
            async with get_session_factory()() as session:
                execution_workflow = (
                    await session.execute(
                        select(ExecutionModel.workflow).where(ExecutionModel.id == execution_id)
                    )
                ).scalar_one_or_none()
        else:
            execution_workflow = resume_workflow
        active_graph = None
        if execution_workflow == "API_COMPOSITION":
            checkpointer = runner.checkpointer
            if checkpointer is None:
                raise ValueError("Composition workflow requires a configured checkpointer")
            active_graph = build_api_composition_workflow(checkpointer=checkpointer)
        event_resume = ExecutionResumeEvent(
            execution_id=execution_id,
            event_id=event_id,
            triggered_by_task_id=uuid.UUID(task_id) if task_id else None,
            context_version=body.get("context_version", 1),
        )
        await runner.handle_execution_resume(event=event_resume, graph=active_graph)

    else:
        logger.error(f"Unsupported event kind: {kind}")
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
        graph = compile_trip_planning_workflow(checkpointer=checkpointer)
        runner = GraphRunnerService(session_factory, default_graph=graph)

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
