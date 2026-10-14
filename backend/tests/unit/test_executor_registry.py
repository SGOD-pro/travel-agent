"""Unit tests for worker executor registry and dispatch mechanism.

Conforms to spec 0002 AC-15.
Validates executor registration, default evidence collection dispatch, and missing executor errors.
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
async def test_evidence_collect_executor_registered() -> None:
    """AC-15: Verifies that ('EVIDENCE', 'collect') is registered in EXECUTOR_REGISTRY."""
    assert ("EVIDENCE", "collect") in EXECUTOR_REGISTRY
    executor = EXECUTOR_REGISTRY[("EVIDENCE", "collect")]
    assert callable(executor)


@pytest.mark.asyncio
async def test_dispatch_evidence_collect_happy_path() -> None:
    """AC-15: Verifies default dispatch execution for EVIDENCE collect task."""
    payload = {
        "observations": ["obs_1", "obs_2"],
        "queries": ["hotels in Kolkata"],
        "urls": ["https://example.com/hotels"],
    }
    result = await dispatch("EVIDENCE", "collect", payload)

    assert result["status"] == "COMPLETED"
    assert result["observations"] == ["obs_1", "obs_2"]
    assert result["queries"] == ["hotels in Kolkata"]
    assert result["urls"] == ["https://example.com/hotels"]


@pytest.mark.asyncio
async def test_dispatch_evidence_collect_empty_payload() -> None:
    """AC-15: Verifies default dispatch execution when payload fields are absent."""
    result = await dispatch("EVIDENCE", "collect", {})

    assert result["status"] == "COMPLETED"
    assert result["observations"] == []
    assert result["queries"] == []
    assert result["urls"] == []


@pytest.mark.asyncio
async def test_dispatch_evidence_collect_pages_fallback() -> None:
    """AC-15: Verifies payload fallback when pages key is provided instead of urls."""
    payload = {
        "pages": ["https://example.com/page1"],
    }
    result = await dispatch("EVIDENCE", "collect", payload)

    assert result["urls"] == ["https://example.com/page1"]


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
