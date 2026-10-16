"""Unit tests for dedicated browser worker SQS consumer and Lambda handler.

Verifies:
1. Message routing and validation for worker="BROWSER" and action="render".
2. Safe discarding of benign fencing claim conflicts without DLQ redrive.
3. Batch item failure reporting for execution errors and Lambda remaining time timeouts.
4. Lambda handler entrypoint execution with asyncio loop.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contracts.browser import BrowserTaskPayload
from functions.browser.consumer import lambda_handler, process_batch, process_record
from functions.browser.fixtures import FixtureBrowserAdapter
from functions.orchestrator.execution.fencing import (
    ExecutionEngineError,
    StaleQueueMessageError,
    TaskAlreadyTerminalError,
    TaskClaimConflictError,
    TaskLeaseActiveError,
)


def _make_sqs_record(
    *,
    message_id: str = "msg-001",
    task_id: uuid.UUID | None = None,
    execution_id: uuid.UUID | None = None,
    worker: str = "BROWSER",
    action: str = "render",
    canonical_url: str = "https://example.com/hotel-spa",
    category: str = "lodging",
) -> dict[str, Any]:
    tid = task_id or uuid.uuid4()
    eid = execution_id or uuid.uuid4()
    payload = BrowserTaskPayload(
        url=canonical_url,
        canonical_url=canonical_url,
        category=category,
        reason="NOSCRIPT_JAVASCRIPT_REQUIRED",
    ).model_dump(mode="json")

    return {
        "messageId": message_id,
        "receiptHandle": f"receipt-{message_id}",
        "body": {
            "task_id": str(tid),
            "execution_id": str(eid),
            "worker": worker,
            "action": action,
            "attempt": 0,
            "context_version": 1,
            "payload": payload,
        },
    }


class DummyAsyncSessionContext:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_process_record_successful_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that a valid BROWSER render record executes successfully via service."""
    record = _make_sqs_record()
    mock_session = AsyncMock()
    session_factory = MagicMock(return_value=DummyAsyncSessionContext(mock_session))
    adapter = FixtureBrowserAdapter()

    mock_execute = AsyncMock(return_value=(uuid.uuid4(), MagicMock()))
    monkeypatch.setattr(
        "functions.browser.consumer.BrowserWorkerService.execute_render_task",
        mock_execute,
    )

    await process_record(record, session_factory, adapter=adapter)

    assert mock_execute.await_count == 1
    assert mock_execute.await_args is not None
    call_arg = mock_execute.await_args[0][0]
    assert call_arg.worker == "BROWSER"
    assert call_arg.action == "render"


@pytest.mark.asyncio
async def test_process_record_ignores_non_browser_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that records intended for other workers are safely ignored."""
    record = _make_sqs_record(worker="EVIDENCE")
    mock_session = AsyncMock()
    session_factory = MagicMock(return_value=DummyAsyncSessionContext(mock_session))
    mock_execute = AsyncMock()
    monkeypatch.setattr(
        "functions.browser.consumer.BrowserWorkerService.execute_render_task",
        mock_execute,
    )

    await process_record(record, session_factory)

    mock_execute.assert_not_called()
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_process_record_ignores_non_render_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that non render actions for BROWSER worker are safely ignored."""
    record = _make_sqs_record(action="scrape")
    mock_session = AsyncMock()
    session_factory = MagicMock(return_value=DummyAsyncSessionContext(mock_session))
    mock_execute = AsyncMock()
    monkeypatch.setattr(
        "functions.browser.consumer.BrowserWorkerService.execute_render_task",
        mock_execute,
    )

    await process_record(record, session_factory)

    mock_execute.assert_not_called()
    session_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fencing_error",
    [
        TaskClaimConflictError("Task already claimed by active lease"),
        TaskLeaseActiveError("Lease is currently active"),
        TaskAlreadyTerminalError("Task in SUCCEEDED state"),
        StaleQueueMessageError("Queue message attempt counter stale"),
    ],
)
async def test_process_record_safely_discards_fencing_claim_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    fencing_error: Exception,
) -> None:
    """Verifies that duplicate or stale task deliveries are safely discarded without error."""
    record = _make_sqs_record()
    mock_session = AsyncMock()
    session_factory = MagicMock(return_value=DummyAsyncSessionContext(mock_session))

    monkeypatch.setattr(
        "functions.browser.consumer.BrowserWorkerService.execute_render_task",
        AsyncMock(side_effect=fencing_error),
    )

    # Should not raise
    await process_record(record, session_factory)


@pytest.mark.asyncio
async def test_process_record_reraises_execution_engine_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that fatal execution engine errors are re-raised for DLQ redrive."""
    record = _make_sqs_record()
    mock_session = AsyncMock()
    session_factory = MagicMock(return_value=DummyAsyncSessionContext(mock_session))

    monkeypatch.setattr(
        "functions.browser.consumer.BrowserWorkerService.execute_render_task",
        AsyncMock(side_effect=ExecutionEngineError("Database connection lost")),
    )

    with pytest.raises(ExecutionEngineError, match="Database connection lost"):
        await process_record(record, session_factory)


@pytest.mark.asyncio
async def test_process_batch_success_returns_empty_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that an entirely successful batch returns zero batchItemFailures."""
    rec1 = _make_sqs_record(message_id="msg-1")
    rec2 = _make_sqs_record(message_id="msg-2")
    event = {"Records": [rec1, rec2]}

    mock_session = AsyncMock()
    session_factory = MagicMock(return_value=DummyAsyncSessionContext(mock_session))

    monkeypatch.setattr(
        "functions.browser.consumer.BrowserWorkerService.execute_render_task",
        AsyncMock(return_value=(uuid.uuid4(), MagicMock())),
    )

    failures = await process_batch(event, session_factory=session_factory)
    assert failures == []


@pytest.mark.asyncio
async def test_process_batch_reports_remaining_time_deadline_failures() -> None:
    """Verifies that records are failed early when Lambda remaining execution time is under 5000ms."""
    rec1 = _make_sqs_record(message_id="msg-timeout-1")
    rec2 = _make_sqs_record(message_id="msg-timeout-2")
    event = {"Records": [rec1, rec2]}

    mock_context = MagicMock()
    mock_context.get_remaining_time_in_millis.return_value = 3500  # < 5000ms

    mock_session = AsyncMock()
    session_factory = MagicMock(return_value=DummyAsyncSessionContext(mock_session))

    failures = await process_batch(
        event,
        context=mock_context,
        session_factory=session_factory,
    )

    assert len(failures) == 2
    assert failures == [
        {"itemIdentifier": "msg-timeout-1"},
        {"itemIdentifier": "msg-timeout-2"},
    ]


@pytest.mark.asyncio
async def test_process_batch_reports_partial_record_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that failing records are reported in batchItemFailures while successes are omitted."""
    rec1 = _make_sqs_record(message_id="msg-ok")
    rec2 = _make_sqs_record(message_id="msg-fail")
    event = {"Records": [rec1, rec2]}

    mock_session = AsyncMock()
    session_factory = MagicMock(return_value=DummyAsyncSessionContext(mock_session))

    async def _mock_process(rec: dict[str, Any], sf: Any, *, adapter: Any = None) -> None:
        if rec.get("messageId") == "msg-fail":
            raise RuntimeError("Browser process crashed")

    monkeypatch.setattr("functions.browser.consumer.process_record", _mock_process)

    failures = await process_batch(event, session_factory=session_factory)

    assert len(failures) == 1
    assert failures == [{"itemIdentifier": "msg-fail"}]


def test_lambda_handler_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies standard synchronous AWS Lambda entrypoint execution."""
    rec = _make_sqs_record(message_id="msg-sync")
    event = {"Records": [rec]}

    async def _mock_process_batch(ev: Any, ctx: Any = None) -> list[dict[str, str]]:
        return [{"itemIdentifier": "msg-sync"}]

    monkeypatch.setattr("functions.browser.consumer.process_batch", _mock_process_batch)

    response = lambda_handler(event, None)
    assert response == {"batchItemFailures": [{"itemIdentifier": "msg-sync"}]}


@pytest.mark.asyncio
async def test_process_batch_empty_records_returns_empty_list() -> None:
    """Verifies that an event with no records returns empty batchItemFailures list."""
    failures = await process_batch({"Records": []})
    assert failures == []
    failures_empty_dict = await process_batch({})
    assert failures_empty_dict == []


@pytest.mark.asyncio
async def test_process_batch_handles_record_with_missing_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that records lacking a messageId do not crash batch processing."""
    rec = _make_sqs_record(message_id="")
    rec.pop("messageId", None)
    event = {"Records": [rec]}

    mock_session = AsyncMock()
    session_factory = MagicMock(return_value=DummyAsyncSessionContext(mock_session))

    monkeypatch.setattr(
        "functions.browser.consumer.BrowserWorkerService.execute_render_task",
        AsyncMock(return_value=(uuid.uuid4(), MagicMock())),
    )

    failures = await process_batch(event, session_factory=session_factory)
    assert failures == []
