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
DEFAULT_CLEANUP_TIMEOUT_SECONDS: float = 2.0
SQS_RESPONSE_SAFETY_MARGIN_MS: int = 1000


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

    safety_margin_ms = max(50, min(SQS_RESPONSE_SAFETY_MARGIN_MS, int(reserve_ms * 0.25)))

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

            coro = process_record(
                rec,
                session_factory,
                adapter=adapter,
            )

            if usable_seconds is None:
                try:
                    await coro
                    return None
                except Exception as exc:
                    logger.exception("Error processing browser record %s: %s", message_id, exc)
                    return {"itemIdentifier": message_id} if message_id else None

            task = asyncio.create_task(coro)
            timed_out = False

            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=usable_seconds)
            except TimeoutError:
                timed_out = True
                logger.warning(
                    "Browser record %s exceeded usable invocation window of %.2fs; initiating cancellation",
                    message_id,
                    usable_seconds,
                )
            except asyncio.CancelledError:
                timed_out = True
                task.cancel()
                raise
            except Exception as exc:
                logger.exception("Error during execution of browser record %s: %s", message_id, exc)
                return {"itemIdentifier": message_id} if message_id else None

            if timed_out:
                task.cancel()
                if context and hasattr(context, "get_remaining_time_in_millis"):
                    try:
                        rem_ms = int(context.get_remaining_time_in_millis())
                    except Exception:
                        rem_ms = reserve_ms
                    cleanup_margin_ms = safety_margin_ms + max(25, int(safety_margin_ms * 0.5))
                    available_cleanup_s = max(0.02, (rem_ms - cleanup_margin_ms) / 1000.0)
                    cleanup_sec = min(DEFAULT_CLEANUP_TIMEOUT_SECONDS, available_cleanup_s)
                else:
                    cleanup_sec = DEFAULT_CLEANUP_TIMEOUT_SECONDS

                try:
                    await asyncio.wait_for(task, timeout=cleanup_sec)
                except (TimeoutError, asyncio.CancelledError):
                    logger.warning(
                        "Browser record %s cleanup exceeded bounded window of %.2fs; abandoning task to protect invocation deadline",
                        message_id,
                        cleanup_sec,
                    )
                except Exception as cleanup_err:
                    logger.warning(
                        "Browser record %s cleanup encountered exception: %s",
                        message_id,
                        cleanup_err,
                    )

                return {"itemIdentifier": message_id} if message_id else None

            if task.done() and not task.cancelled():
                task_exc = task.exception()
                if task_exc is not None:
                    if isinstance(
                        task_exc,
                        (
                            TaskClaimConflictError,
                            TaskLeaseActiveError,
                            TaskAlreadyTerminalError,
                            StaleQueueMessageError,
                        ),
                    ):
                        logger.info("Safely discarding task dispatch for %s: %s", message_id, task_exc)
                        return None
                    logger.exception("Error processing browser record %s: %s", message_id, task_exc)
                    return {"itemIdentifier": message_id} if message_id else None

            return None

        if sem is not None:
            async with sem:
                return await _exec()
        return await _exec()

    if context and hasattr(context, "get_remaining_time_in_millis"):
        try:
            total_rem_ms = int(context.get_remaining_time_in_millis())
            available_batch_ms = total_rem_ms - safety_margin_ms
            batch_hard_timeout_s = (
                max(0.05, available_batch_ms / 1000.0) if available_batch_ms > 0 else 0.05
            )
        except Exception:
            batch_hard_timeout_s = None
    else:
        batch_hard_timeout_s = None

    failures: list[dict[str, str]] = []
    record_tasks = [asyncio.create_task(_process_one(r)) for r in records]

    try:
        if batch_hard_timeout_s is not None:
            results = await asyncio.wait_for(
                asyncio.gather(*record_tasks, return_exceptions=True),
                timeout=batch_hard_timeout_s,
            )
        else:
            results = await asyncio.gather(*record_tasks, return_exceptions=True)
    except (TimeoutError, asyncio.CancelledError):
        logger.warning(
            "Browser batch execution reached invocation hard deadline guard (safety margin %dms); returning partial batch failures immediately",
            safety_margin_ms,
        )
        for t in record_tasks:
            if not t.done():
                t.cancel()
        for i, t in enumerate(record_tasks):
            mid = records[i].get("messageId", "")
            if not mid:
                continue
            if t.done() and not t.cancelled() and t.exception() is None:
                task_res = t.result()
                if isinstance(task_res, dict) and "itemIdentifier" in task_res:
                    failures.append(task_res)
            else:
                failures.append({"itemIdentifier": mid})
        return failures

    for i, item in enumerate(results):
        if isinstance(item, dict) and "itemIdentifier" in item:
            failures.append(item)
        elif isinstance(item, BaseException):
            mid = records[i].get("messageId", "")
            logger.exception("Unexpected exception in browser record %s: %s", mid, item)
            if mid:
                failures.append({"itemIdentifier": mid})

    return failures


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Standard AWS Lambda entrypoint for browser escalation worker consumer."""
    failures = asyncio.run(process_batch(event, context))
    return {"batchItemFailures": failures}
