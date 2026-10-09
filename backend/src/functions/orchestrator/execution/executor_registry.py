"""Worker executor registry for the TASK_DISPATCH Lambda consumer.

Maps (worker, action) pairs to async executor coroutines that perform the
actual task work.  Unknown or unimplemented pairs raise WorkerNotImplementedError
immediately so they are not silently recorded as successful completions.

To register a real executor, add it to EXECUTOR_REGISTRY below.
Test fixtures inject mock executors directly via the session_factory parameter
in process_batch and do not go through this module.
"""

from __future__ import annotations

from typing import Any


class WorkerNotImplementedError(NotImplementedError):
    """Raised when a (worker, action) pair has no registered executor.

    This exception propagates back to worker_consumer.process_record which
    records the task as FAILED and returns a batchItemFailure so SQS can retry
    or route the message to the DLQ.  It must never silently succeed.
    """


# Registry mapping (worker_role, action) -> async callable(payload) -> dict[str, Any]
# Add entries here as real worker implementations are built.
EXECUTOR_REGISTRY: dict[tuple[str, str], Any] = {}


async def dispatch(worker_role: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatches a task to the registered executor for (worker_role, action).

    Raises WorkerNotImplementedError for unregistered (worker_role, action) pairs.
    Registered executors must be async callables that accept a single payload dict
    and return a dict[str, Any] result.
    """
    key = (worker_role, action)
    executor = EXECUTOR_REGISTRY.get(key)
    if executor is None:
        raise WorkerNotImplementedError(
            f"No executor registered for worker={worker_role!r} action={action!r}. "
            "Register one in EXECUTOR_REGISTRY or implement the worker before deploying."
        )
    return await executor(payload)
