"""Unit tests for bounded scheduled Lambda handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from functions.orchestrator.execution.scheduled import (
    _compute_remaining_budget_seconds,
    reconciliation_handler,
    recovery_handler,
    relay_handler,
)


class MockContext:
    def __init__(self, remaining_ms: int = 15000):
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


def test_compute_remaining_budget_seconds() -> None:
    # Context with 15000ms -> (15000 - 3000) / 1000 = 12.0s
    ctx = MockContext(remaining_ms=15000)
    assert _compute_remaining_budget_seconds(ctx) == 12.0

    # Context with very low time -> bounded at minimum 1.0s
    ctx_low = MockContext(remaining_ms=2000)
    assert _compute_remaining_budget_seconds(ctx_low) == 1.0

    # None context -> default 25.0s
    assert _compute_remaining_budget_seconds(None) == 25.0


def test_relay_handler_invokes_bounded_loop() -> None:
    mock_summary = MagicMock()
    mock_summary.batches_processed = 2
    mock_summary.total_claimed = 10
    mock_summary.total_delivered = 10
    mock_summary.total_retry_scheduled = 0
    mock_summary.total_exhausted = 0
    mock_summary.total_fenced_out = 0
    mock_summary.elapsed_seconds = 1.2
    mock_summary.terminated_by_timeout = False

    with patch(
        "functions.orchestrator.execution.scheduled.OutboxRelayService"
    ) as mock_service_cls:
        mock_instance = mock_service_cls.return_value
        mock_instance.run_relay_loop = AsyncMock(return_value=mock_summary)

        res = relay_handler({}, MockContext(remaining_ms=10000))

        assert res["statusCode"] == 200
        assert res["summary"]["batches_processed"] == 2
        mock_instance.run_relay_loop.assert_awaited_once_with(max_runtime_seconds=7.0)


def test_recovery_handler_invokes_bounded_loop() -> None:
    mock_summary = MagicMock()
    mock_summary.batches_processed = 1
    mock_summary.total_scanned = 5
    mock_summary.total_retried = 2
    mock_summary.total_failed = 0
    mock_summary.total_cancelled = 0
    mock_summary.total_skipped = 3
    mock_summary.elapsed_seconds = 0.8
    mock_summary.terminated_by_timeout = False
    mock_summary.executions_recovered = 1

    with patch(
        "functions.orchestrator.execution.scheduled.TaskRecoveryService"
    ) as mock_service_cls:
        mock_instance = mock_service_cls.return_value
        mock_instance.run_recovery_loop = AsyncMock(return_value=mock_summary)

        res = recovery_handler({}, MockContext(remaining_ms=12000))

        assert res["statusCode"] == 200
        assert res["summary"]["total_retried"] == 2
        mock_instance.run_recovery_loop.assert_awaited_once_with(max_runtime_seconds=9.0)


def test_reconciliation_handler_invokes_bounded_loop() -> None:
    mock_summary = MagicMock()
    mock_summary.batches_processed = 1
    mock_summary.total_scanned = 3
    mock_summary.total_reconciled = 3
    mock_summary.total_discarded = 0
    mock_summary.elapsed_seconds = 0.5
    mock_summary.terminated_by_timeout = False

    with patch(
        "functions.orchestrator.execution.scheduled.CompletionReconciliationService"
    ) as mock_service_cls:
        mock_instance = mock_service_cls.return_value
        mock_instance.run_reconciliation_loop = AsyncMock(return_value=mock_summary)

        res = reconciliation_handler({}, MockContext(remaining_ms=20000))

        assert res["statusCode"] == 200
        assert res["summary"]["total_reconciled"] == 3
        mock_instance.run_reconciliation_loop.assert_awaited_once_with(max_runtime_seconds=17.0)
