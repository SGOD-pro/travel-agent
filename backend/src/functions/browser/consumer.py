"""Dedicated Lambda consumer for browser escalation TASK_DISPATCH messages.

Reuses permitted 0001 execution support without importing orchestrator evidence policy,
canonical places, or graph runner workflows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.db import get_session_factory
from contracts.browser import BrowserExtractionPort
from functions.browser.driver import PlaywrightBrowserAdapter
from functions.browser.service import (
    BrowserWorkerService,
    parse_browser_dispatch_message,
)
from functions.orchestrator.execution.fencing import (
    ExecutionEngineError,
    StaleQueueMessageError,
    TaskAlreadyTerminalError,
    TaskClaimConflictError,
    TaskLeaseActiveError,
)

logger = logging.getLogger(__name__)

DEFAULT_INVOCATION_RESERVE_MS: int = 5000


def get_remaining_usable_seconds(context: Any, reserve_ms: int) -> float | None:
    """Calculates usable seconds remaining in the Lambda invocation before reserve margin.

    Returns None if context does not support get_remaining_time_in_millis (e.g. local offline mode).
    Returns 0.0 if remaining time is at or below the reserve margin.
    """
    if context and hasattr(context, "get_remaining_time_in_millis"):
        try:
            remaining_ms = int(context.get_remaining_time_in_millis())
        except Exception:
            return None
        usable_ms = remaining_ms - reserve_ms
        if usable_ms <= 0:
            return 0.0
        return usable_ms / 1000.0
    return None


async def process_record(
    record: dict[str, Any],
    session_factory: Any,
    *,
    adapter: BrowserExtractionPort | None = None,
) -> None:
    """Parses a TASK_DISPATCH message and executes browser rendering under lease fencing."""
    envelope = parse_browser_dispatch_message(record)

    if envelope.worker != "BROWSER" or envelope.action != "render":
        logger.warning(
            "Browser worker ignoring message for worker=%s action=%s",
            envelope.worker,
            envelope.action,
        )
        return

    extraction_port: BrowserExtractionPort = (
        adapter if adapter is not None else PlaywrightBrowserAdapter()
    )

    async with session_factory() as session:
        service = BrowserWorkerService(session, extraction_port=extraction_port)
        try:
            await service.execute_render_task(envelope)
        except (
            TaskClaimConflictError,
            TaskLeaseActiveError,
            TaskAlreadyTerminalError,
            StaleQueueMessageError,
        ) as exc:
            logger.info("Safely discarding task dispatch for %s: %s", envelope.task_id, exc)
            return
        except ExecutionEngineError:
            logger.error(
                "Terminal execution engine error for browser task %s; returning as batchItemFailure for DLQ",
                envelope.task_id,
                exc_info=True,
            )
            raise


async def process_batch(
    event: dict[str, Any],
    context: Any = None,
    session_factory: Any | None = None,
    adapter: BrowserExtractionPort | None = None,
    *,
    reserve_ms: int = DEFAULT_INVOCATION_RESERVE_MS,
    max_concurrency: int | None = None,
) -> list[dict[str, str]]:
    """Processes an SQS batch under remaining invocation deadline and returns failed item identifiers."""
    if session_factory is None:
        session_factory = get_session_factory()

    records = event.get("Records", [])
    if not records:
        return []

    initial_usable = get_remaining_usable_seconds(context, reserve_ms)
    if initial_usable is not None and initial_usable <= 0:
        logger.warning(
            "Lambda remaining time under %dms reserve before starting batch; failing all %d messages for safe SQS redrive",
            reserve_ms,
            len(records),
        )
        return [{"itemIdentifier": r["messageId"]} for r in records if r.get("messageId")]

    sem = (
        asyncio.Semaphore(max_concurrency)
        if max_concurrency is not None and max_concurrency > 0
        else None
    )

    async def _process_one(rec: dict[str, Any]) -> dict[str, str] | None:
        message_id = rec.get("messageId", "")

        async def _exec() -> dict[str, str] | None:
            usable_seconds = get_remaining_usable_seconds(context, reserve_ms)
            if usable_seconds is not None and usable_seconds <= 0:
                logger.warning(
                    "Invocation deadline reached before starting browser record %s (%dms reserve); failing for SQS redrive",
                    message_id,
                    reserve_ms,
                )
                return {"itemIdentifier": message_id} if message_id else None

            try:
                if usable_seconds is not None:
                    await asyncio.wait_for(
                        process_record(
                            rec,
                            session_factory,
                            adapter=adapter,
                        ),
                        timeout=usable_seconds,
                    )
                else:
                    await process_record(
                        rec,
                        session_factory,
                        adapter=adapter,
                    )
                return None
            except (TimeoutError, asyncio.CancelledError):
                logger.warning(
                    "Browser record %s exceeded invocation deadline or was cancelled; failing for SQS redrive",
                    message_id,
                )
                return {"itemIdentifier": message_id} if message_id else None
            except Exception as exc:
                logger.exception("Error processing browser record %s: %s", message_id, exc)
                return {"itemIdentifier": message_id} if message_id else None

        if sem is not None:
            async with sem:
                return await _exec()
        return await _exec()

    results = await asyncio.gather(
        *[_process_one(r) for r in records],
        return_exceptions=True,
    )

    failures: list[dict[str, str]] = []
    for i, res in enumerate(results):
        if isinstance(res, dict) and "itemIdentifier" in res:
            failures.append(res)
        elif isinstance(res, Exception):
            mid = records[i].get("messageId", "")
            logger.exception("Unexpected exception in browser record %s: %s", mid, res)
            if mid:
                failures.append({"itemIdentifier": mid})

    return failures


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Standard AWS Lambda entrypoint for browser escalation worker consumer."""
    failures = asyncio.run(process_batch(event, context))
    return {"batchItemFailures": failures}
