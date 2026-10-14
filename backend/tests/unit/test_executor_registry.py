"""Unit tests for worker executor registry and dispatch mechanism.

Validates executor registration, dynamic dispatch, and missing executor errors
after removing the echo-success stub.
"""

from __future__ import annotations

from typing import Any

import pytest

from functions.orchestrator.execution.executor_registry import (
    EXECUTOR_REGISTRY,
    WorkerNotImplementedError,
    dispatch,
)


@pytest.mark.asyncio
async def test_echo_success_executor_removed_from_registry() -> None:
    """Verifies that ('EVIDENCE', 'collect') has no synthetic echo stub in EXECUTOR_REGISTRY."""
    assert ("EVIDENCE", "collect") not in EXECUTOR_REGISTRY


@pytest.mark.asyncio
async def test_dispatch_evidence_collect_without_registered_executor_raises_error() -> None:
    """Verifies that dispatching ('EVIDENCE', 'collect') raises WorkerNotImplementedError when unregistered."""
    with pytest.raises(WorkerNotImplementedError) as exc_info:
        await dispatch("EVIDENCE", "collect", {"queries": ["hotels in Kolkata"]})

    assert "No executor registered for worker='EVIDENCE' action='collect'" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_unregistered_worker_raises_error() -> None:
    """Verifies that an unknown worker and action pair raises WorkerNotImplementedError."""
    with pytest.raises(WorkerNotImplementedError) as exc_info:
        await dispatch("NON_EXISTENT_WORKER", "non_existent_action", {})

    assert "No executor registered for worker='NON_EXISTENT_WORKER' action='non_existent_action'" in str(
        exc_info.value
    )


@pytest.mark.asyncio
async def test_custom_executor_registration_and_dispatch() -> None:
    """Verifies that dynamically registered executors can be dispatched and executed."""
    async def mock_custom_executor(payload: dict[str, Any]) -> dict[str, Any]:
        return {"custom_result": payload.get("data", 0) * 2}

    key = ("CUSTOM_WORKER", "calculate")
    EXECUTOR_REGISTRY[key] = mock_custom_executor
    try:
        res = await dispatch("CUSTOM_WORKER", "calculate", {"data": 21})
        assert res["custom_result"] == 42
    finally:
        EXECUTOR_REGISTRY.pop(key, None)
