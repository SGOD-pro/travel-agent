"""Bounded scheduled entrypoints for outbox relay, task recovery, and stalled reconciliation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, is_dataclass
from typing import Any

from config.db import get_session_factory
from functions.orchestrator.execution.reconciliation import CompletionReconciliationService
from functions.orchestrator.execution.recovery import TaskRecoveryService
from functions.orchestrator.execution.relay import OutboxRelayService
from functions.orchestrator.execution.transport import build_sqs_transport

logger = logging.getLogger(__name__)


def _summary_to_dict(summary: Any) -> dict[str, Any]:
    if is_dataclass(summary) and not isinstance(summary, type):
        return asdict(summary)
    if hasattr(summary, "__dict__"):
        return {k: v for k, v in summary.__dict__.items() if not k.startswith("_")}
    if isinstance(summary, dict):
        return summary
    return {"summary": str(summary)}


def _compute_remaining_budget_seconds(context: Any, default_seconds: float = 25.0) -> float:
    """Computes max runtime budget in seconds from Lambda context with a safety buffer."""
    if context and hasattr(context, "get_remaining_time_in_millis"):
        remaining_ms = context.get_remaining_time_in_millis()
        # Leave a 3 second buffer for graceful teardown/flush
        budget_seconds = (remaining_ms - 3000) / 1000.0
        return max(1.0, budget_seconds)
    return default_seconds


async def _run_relay_async(context: Any) -> dict[str, Any]:
    runtime_limit = _compute_remaining_budget_seconds(context, default_seconds=25.0)
    session_factory = get_session_factory()
    transport = build_sqs_transport()
    relay_service = OutboxRelayService(
        session_factory=session_factory,
        transport=transport,
        max_runtime_seconds=runtime_limit,
    )
    summary = await relay_service.run_relay_loop(max_runtime_seconds=runtime_limit)
    return _summary_to_dict(summary)


async def _run_recovery_async(context: Any) -> dict[str, Any]:
    runtime_limit = _compute_remaining_budget_seconds(context, default_seconds=25.0)
    session_factory = get_session_factory()
    recovery_service = TaskRecoveryService(
        session_factory=session_factory,
        max_runtime_seconds=runtime_limit,
    )
    summary = await recovery_service.run_recovery_loop(max_runtime_seconds=runtime_limit)
    return _summary_to_dict(summary)


async def _run_reconciliation_async(context: Any) -> dict[str, Any]:
    runtime_limit = _compute_remaining_budget_seconds(context, default_seconds=25.0)
    session_factory = get_session_factory()
    reconciler = CompletionReconciliationService(
        session_factory=session_factory,
        max_runtime_seconds=runtime_limit,
    )
    summary = await reconciler.run_reconciliation_loop(max_runtime_seconds=runtime_limit)
    return _summary_to_dict(summary)


def relay_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entrypoint for EventBridge-scheduled bounded outbox relay."""
    logger.info("Executing scheduled outbox relay")
    summary = asyncio.run(_run_relay_async(context))
    logger.info("Scheduled outbox relay completed: %s", summary)
    return {"statusCode": 200, "summary": summary}


def recovery_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entrypoint for EventBridge-scheduled bounded task and execution recovery."""
    logger.info("Executing scheduled task recovery")
    summary = asyncio.run(_run_recovery_async(context))
    logger.info("Scheduled task recovery completed: %s", summary)
    return {"statusCode": 200, "summary": summary}


def reconciliation_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entrypoint for EventBridge-scheduled bounded stalled receipt reconciliation."""
    logger.info("Executing scheduled completion reconciliation")
    summary = asyncio.run(_run_reconciliation_async(context))
    logger.info("Scheduled completion reconciliation completed: %s", summary)
    return {"statusCode": 200, "summary": summary}
