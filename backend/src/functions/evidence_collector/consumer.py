"""Dedicated Lambda consumer for evidence collector TASK_DISPATCH messages.

Reuses permitted 0001 execution support without importing orchestrator evidence policy,
canonical places, or graph runner workflows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.db import get_session_factory
from functions.evidence_collector.service import (
    EvidenceCollectorService,
    parse_task_dispatch_envelope,
)
from functions.orchestrator.execution.fencing import (
    ExecutionEngineError,
    StaleQueueMessageError,
    TaskAlreadyTerminalError,
    TaskClaimConflictError,
    TaskLeaseActiveError,
)

logger = logging.getLogger(__name__)


async def process_record(
    record: dict[str, Any],
    session_factory: Any,
    *,
    adapter: Any | None = None,
    extraction_adapter: Any | None = None,
) -> None:
    """Parses a TASK_DISPATCH message and executes the collector under lease fencing."""
    envelope = parse_task_dispatch_envelope(record)

    if envelope.worker != "EVIDENCE" or envelope.action != "collect":
        logger.warning(
            "Evidence collector ignoring message for worker=%s action=%s",
            envelope.worker,
            envelope.action,
        )
        return

    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        try:
            await service.execute_collection_task(
                envelope,
                adapter=adapter,
                extraction_adapter=extraction_adapter,
            )
        except (
            TaskClaimConflictError,
            TaskLeaseActiveError,
            TaskAlreadyTerminalError,
            StaleQueueMessageError,
        ) as e:
            logger.info("Safely discarding task dispatch for %s: %s", envelope.task_id, e)
            return
        except ExecutionEngineError:
            logger.error(
                "Terminal execution engine error for task %s; returning as batchItemFailure for DLQ",
                envelope.task_id,
                exc_info=True,
            )
            raise


async def process_batch(
    event: dict[str, Any],
    context: Any = None,
    session_factory: Any | None = None,
    adapter: Any | None = None,
    extraction_adapter: Any | None = None,
) -> list[dict[str, str]]:
    """Processes an SQS batch concurrently and returns failed item identifiers."""
    if session_factory is None:
        session_factory = get_session_factory()

    records = event.get("Records", [])
    if not records:
        return []

    deadline_failures: list[dict[str, str]] = []
    processable: list[dict[str, Any]] = []

    for record in records:
        message_id = record.get("messageId", "")
        if context and hasattr(context, "get_remaining_time_in_millis"):
            if context.get_remaining_time_in_millis() < 3000:
                logger.warning(
                    "Lambda remaining time under 3000ms, failing message %s to trigger safe SQS redrive",
                    message_id,
                )
                deadline_failures.append({"itemIdentifier": message_id})
                continue
        processable.append(record)

    async def _process_one(record: dict[str, Any]) -> dict[str, str] | None:
        message_id = record.get("messageId", "")
        try:
            await process_record(
                record,
                session_factory,
                adapter=adapter,
                extraction_adapter=extraction_adapter,
            )
            return None
        except Exception as e:
            logger.exception("Error processing evidence record %s: %s", message_id, e)
            return {"itemIdentifier": message_id} if message_id else None

    results = await asyncio.gather(*[_process_one(r) for r in processable])
    failures = deadline_failures + [r for r in results if r is not None]
    return failures


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Standard AWS Lambda entrypoint for evidence collector consumer."""
    failures = asyncio.run(process_batch(event, context))
    return {"batchItemFailures": failures}
