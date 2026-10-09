"""Lambda handler for TASK_DISPATCH worker consumer."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from config.db import get_session_factory
from functions.orchestrator.execution.executor_registry import dispatch as dispatch_worker
from functions.orchestrator.execution.fencing import (
    ExecutionEngineError,
    StaleQueueMessageError,
    TaskAlreadyTerminalError,
    TaskClaimConflictError,
    TaskLeaseActiveError,
    TaskQueueMessage,
    WorkerFencingService,
)

logger = logging.getLogger(__name__)


async def process_record(record: dict[str, Any], session_factory) -> None:
    """Parses and validates a TASK_DISPATCH record and executes the worker claim loop."""
    body_str = record.get("body", "{}")
    try:
        body = json.loads(body_str)
    except json.JSONDecodeError as e:
        logger.error(f"Malformed SQS message body: {body_str}")
        raise ValueError("Malformed JSON") from e

    kind = body.get("kind")
    if kind != "TASK_DISPATCH":
        logger.error(f"Unsupported event kind: {kind}")
        raise ValueError(f"Unsupported kind: {kind}")

    task_id_str = body.get("task_id")
    execution_id_str = body.get("execution_id")
    if not task_id_str or not execution_id_str:
        logger.error("Missing required task_id or execution_id")
        raise ValueError("Missing required fields")

    task_id = uuid.UUID(task_id_str)
    execution_id = uuid.UUID(execution_id_str)
    attempt = body.get("attempt")
    context_version = body.get("context_version", 1)

    # In a real worker, these would match the worker's assigned role
    # For this trace, we derive them from the payload if possible, or use a dummy
    payload = body.get("payload_ref") or {}
    worker_role = payload.get("worker", "EVIDENCE")
    action = payload.get("action", "collect")

    message = TaskQueueMessage(
        task_id=task_id,
        execution_id=execution_id,
        attempt=attempt
    )

    async with session_factory() as session:
        fencing_service = WorkerFencingService(session)

        try:
            claim_result = await fencing_service.claim_task(
                message=message,
                worker=worker_role,
                action=action,
                expected_context_version=context_version,
                lease_seconds=30,
            )
        except (TaskClaimConflictError, TaskLeaseActiveError, TaskAlreadyTerminalError, StaleQueueMessageError) as e:
            logger.info(f"Discarding task dispatch for {task_id}: {e}")
            return  # Do not fail batch, safely discard
        except ExecutionEngineError as e:
            logger.error(f"Fatal fencing error claiming task {task_id}: {e}")
            return  # Terminal domain error, do not retry

        # Execute actual worker logic via the executor registry.
        # Unknown (worker, action) pairs raise WorkerNotImplementedError,
        # which propagates as an exception causing FAILED task status and a
        # batchItemFailure return so SQS can retry or route to the DLQ.
        try:
            output = await dispatch_worker(worker_role, action, payload)
            await fencing_service.complete_task(
                task_id=task_id,
                execution_id=execution_id,
                lease_token=claim_result.lease_token,
                expected_context_version=context_version,
                output=output,
            )
        except Exception as e:
            logger.exception("Worker execution failed for task %s: %s", task_id, e)
            try:
                await fencing_service.fail_task(
                    task_id=task_id,
                    execution_id=execution_id,
                    lease_token=claim_result.lease_token,
                    expected_context_version=context_version,
                    error={"message": str(e)},
                )
            except Exception as rollback_err:
                logger.error("Failed to record task failure for %s: %s", task_id, rollback_err)
                raise


async def process_batch(
    event: dict[str, Any],
    context: Any,
    session_factory: Any | None = None,
) -> list[dict[str, str]]:
    """Processes an SQS batch concurrently and returns item identifiers for failed records."""
    if session_factory is None:
        session_factory = get_session_factory()

    records = event.get("Records", [])
    if not records:
        return []

    # Separate records that are already over-budget (must be failed immediately)
    # from those that can be processed.
    over_budget: list[dict[str, str]] = []
    processable: list[dict[str, Any]] = []
    for record in records:
        message_id = record.get("messageId", "")
        if context and hasattr(context, "get_remaining_time_in_millis"):
            if context.get_remaining_time_in_millis() < 2000:
                logger.warning(
                    "Lambda remaining time too low (%dms), failing message %s to trigger retry",
                    context.get_remaining_time_in_millis(),
                    message_id,
                )
                over_budget.append({"itemIdentifier": message_id})
                continue
        processable.append(record)

    async def _process_one(record: dict[str, Any]) -> dict[str, str] | None:
        message_id = record.get("messageId", "")
        try:
            await process_record(record, session_factory)
            return None
        except Exception as e:
            logger.exception("Error processing record %s: %s", message_id, e)
            return {"itemIdentifier": message_id} if message_id else None

    results = await asyncio.gather(*[_process_one(r) for r in processable])
    failures = over_budget + [r for r in results if r is not None]
    return failures


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Standard AWS Lambda entrypoint for TASK_DISPATCH Worker consumer."""
    failures = asyncio.run(process_batch(event, context))
    return {"batchItemFailures": failures}
