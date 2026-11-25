"""Dedicated Lambda consumer for browser escalation TASK_DISPATCH messages.

Reuses permitted 0001 execution support without importing orchestrator evidence policy,
canonical places, or graph runner workflows.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import concurrent.futures.thread as cft
import logging
import threading
import weakref
from typing import Any, cast

from config.db import get_session_factory
from contracts.browser import BrowserExtractionPort
from functions.browser.driver import PlaywrightBrowserAdapter, terminate_all_browser_processes
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


class BoundedDaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """Container scoped thread pool creating daemon threads to prevent unbounded thread leaks and exit hangs.

    Relies on CPython ThreadPoolExecutor internals (_worker, _idle_semaphore, _work_queue, _threads_queues)
    and defensively unlinks threads from _threads_queues so normal process exit never joins daemon threads.
    """

    def _adjust_thread_count(self) -> None:
        if getattr(self, "_idle_semaphore", None) and self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_: Any, q: Any = self._work_queue) -> None:
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = f"{self._thread_name_prefix or self}_{num_threads}"
            t = threading.Thread(
                name=thread_name,
                target=cft._worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            t.start()
            cast(set[Any], self._threads).add(t)
            # Defensive unlink: _adjust_thread_count omits _threads_queues registration so atexit won't join
            cast(dict[Any, Any], cft._threads_queues).pop(t, None)


_CONTAINER_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def get_container_executor(max_workers: int = 2) -> concurrent.futures.ThreadPoolExecutor:
    """Returns a container scoped bounded executor with daemon threads."""
    global _CONTAINER_EXECUTOR
    if _CONTAINER_EXECUTOR is None or getattr(_CONTAINER_EXECUTOR, "_shutdown", False):
        _CONTAINER_EXECUTOR = BoundedDaemonThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="browser_worker",
        )
    return _CONTAINER_EXECUTOR


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
                    done_clean, pending_clean = await asyncio.wait({task}, timeout=cleanup_sec)
                    if pending_clean:
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
                        logger.info(
                            "Safely discarding task dispatch for %s: %s", message_id, task_exc
                        )
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
            done_batch, pending_batch = await asyncio.wait(
                set(record_tasks),
                timeout=batch_hard_timeout_s,
            )
        else:
            done_batch, pending_batch = await asyncio.wait(set(record_tasks))
    except Exception as batch_err:
        logger.warning(
            "Browser batch execution encountered error during task wait: %s",
            batch_err,
        )
        done_batch, pending_batch = set(), set(record_tasks)

    if pending_batch:
        logger.warning(
            "Browser batch execution reached invocation hard deadline guard (safety margin %dms); returning partial batch failures immediately",
            safety_margin_ms,
        )
        for t in pending_batch:
            t.cancel()

    for i, t in enumerate(record_tasks):
        mid = records[i].get("messageId", "")
        if not mid:
            continue
        if t in done_batch and not t.cancelled():
            task_exc = t.exception()
            if task_exc is not None:
                logger.exception("Unexpected exception in browser record %s: %s", mid, task_exc)
                failures.append({"itemIdentifier": mid})
            else:
                task_res = t.result()
                if isinstance(task_res, dict) and "itemIdentifier" in task_res:
                    failures.append(task_res)
        else:
            failures.append({"itemIdentifier": mid})

    return failures


def lambda_handler(
    event: dict[str, Any],
    context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Standard AWS Lambda entrypoint for browser escalation worker consumer.

    Enforces a strict wall clock bound on batch execution and event loop shutdown,
    ensuring that partial batch failure responses are returned to SQS before the
    invocation deadline even if records or background tasks suppress cancellation.
    """
    records = event.get("Records", []) if isinstance(event, dict) else []
    if not records:
        return {"batchItemFailures": []}

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    container_exec = get_container_executor()
    loop.set_default_executor(container_exec)

    failures: list[dict[str, str]] = []
    try:
        rem_ms: int | None = None
        if context and hasattr(context, "get_remaining_time_in_millis"):
            try:
                rem_ms = int(context.get_remaining_time_in_millis())
            except Exception:
                rem_ms = None

        if rem_ms is not None and rem_ms <= 100:
            logger.warning(
                "Lambda invocation remaining time (%dms) below critical threshold; failing all records for SQS retry",
                rem_ms,
            )
            return {
                "batchItemFailures": [
                    {"itemIdentifier": r["messageId"]} for r in records if r.get("messageId")
                ]
            }

        # Calculate handler execution timeout reserving time for loop shutdown and browser process cleanup
        if rem_ms is not None:
            # Reserve 250ms for handler loop task cancellation, asyncgens shutdown, SIGKILL teardown, and SQS return
            handler_exec_timeout_s = max(0.05, (rem_ms - 250) / 1000.0)
        else:
            handler_exec_timeout_s = None

        batch_task = loop.create_task(process_batch(event, context, **kwargs))
        try:
            if handler_exec_timeout_s is not None:
                done_b, pending_b = loop.run_until_complete(
                    asyncio.wait({batch_task}, timeout=handler_exec_timeout_s)
                )
                if pending_b:
                    batch_task.cancel()
                    logger.warning(
                        "Handler execution reached invocation deadline guard; returning batchItemFailures immediately"
                    )
                    failures = [
                        {"itemIdentifier": r["messageId"]} for r in records if r.get("messageId")
                    ]
                else:
                    failures = batch_task.result()
            else:
                failures = loop.run_until_complete(batch_task)
        except Exception as e:
            logger.exception("Unexpected exception in handler execution: %s", e)
            failures = [{"itemIdentifier": r["messageId"]} for r in records if r.get("messageId")]

        # Bounded shutdown of all lingering tasks on this invocation's event loop
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        if pending:
            for t in pending:
                t.cancel()

            if rem_ms is not None and hasattr(context, "get_remaining_time_in_millis"):
                try:
                    cur_rem = int(context.get_remaining_time_in_millis())
                except Exception:
                    cur_rem = 150
                shutdown_s = (
                    max(0.01, min(0.5, (cur_rem - 150) / 1000.0)) if cur_rem > 150 else 0.01
                )
            else:
                shutdown_s = 0.5

            try:
                done, still_pending = loop.run_until_complete(
                    asyncio.wait(set(pending), timeout=shutdown_s)
                )
                if still_pending:
                    logger.warning(
                        "Handler task cancellation exceeded shutdown budget of %.2fs; abandoning %d pending tasks",
                        shutdown_s,
                        len(still_pending),
                    )
            except Exception as e:
                logger.warning("Handler task cancellation encountered error: %s", e)

        # Shutdown async generators cleanly
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=0.05))
        except Exception:
            pass

        return {"batchItemFailures": failures}
    finally:
        if container_exec is not None:
            try:
                work_q = getattr(container_exec, "_work_queue", None)
                if work_q is not None:
                    while not work_q.empty():
                        try:
                            work_item = work_q.get_nowait()
                            if work_item is None:
                                work_q.put(None)
                                break
                            fut = getattr(work_item, "future", None)
                            if fut is not None:
                                fut.cancel()
                        except Exception:
                            break
            except Exception as exc:
                logger.debug("Error draining container executor: %s", exc)

        custom_executor = getattr(loop, "_default_executor", None)
        if custom_executor is not None and custom_executor is not container_exec:
            try:
                custom_executor.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:
                logger.debug("Error shutting down custom executor: %s", exc)
        adapter = kwargs.get("adapter")
        if adapter is not None and hasattr(adapter, "terminate_residual_processes"):
            try:
                adapter.terminate_residual_processes()
            except Exception as exc:
                logger.debug("Error terminating residual processes on adapter: %s", exc)
        try:
            terminate_all_browser_processes()
        except Exception as exc:
            logger.debug("Error terminating residual browser processes: %s", exc)
        try:
            # CPython 3.12+ BaseEventLoop.close() shuts down _default_executor; unlink to keep container pool alive
            loop._default_executor = None  # type: ignore[attr-defined]
            loop.close()
        except Exception as exc:
            logger.warning("Error closing event loop: %s", exc)
        asyncio.set_event_loop(None)
