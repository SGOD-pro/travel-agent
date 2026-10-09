"""Regression tests covering review findings for 0001 execution engine slice."""

import pytest
import uuid
import os
import asyncio
from unittest.mock import patch, MagicMock

from config.db import async_sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, delete

from functions.orchestrator.execution.transport import build_sqs_transport, NullTransportAdapter, SqsTransportAdapter
from functions.orchestrator.execution.executor_registry import dispatch, WorkerNotImplementedError
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer


def test_invalid_transport_configuration_fails_clearly():
    """Verify SqsTransportAdapter raises when sqs_client is None."""
    with pytest.raises(ValueError, match="requires a real SQS client"):
        SqsTransportAdapter(queue_url="dummy", sqs_client=None)


def test_build_sqs_transport_fallback():
    """Verify build_sqs_transport returns NullTransportAdapter when no URL is set."""
    with patch.dict(os.environ, {}, clear=True):
        adapter = build_sqs_transport()
        assert isinstance(adapter, NullTransportAdapter)


@pytest.mark.asyncio
async def test_unknown_worker_action_raises():
    """Verify dispatching to unknown (worker, action) raises WorkerNotImplementedError."""
    with pytest.raises(WorkerNotImplementedError):
        await dispatch("unknown_worker", "unknown_action", {})


@pytest.mark.asyncio
async def test_checkpointer_mid_flush_failure_retains_pending(session_factory):
    """Verify mid-flush failures do not pop from the buffer so retries see them."""
    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    thread_id = str(uuid.uuid4())

    # Add a mock pending checkpoint
    checkpointer.put(
        {
            "configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": "1"}
        },
        {"v": 1, "id": "1", "ts": "2024-01-01T00:00:00Z", "channel_values": {}},
        {"source": "input", "step": -1, "writes": None, "parents": {}},
        {},
    )

    assert len(checkpointer._pending_flushes[thread_id]) == 1

    # Simulate a mid-flush failure
    async with session_factory() as session:
        # Patch session.execute to raise an Exception
        original_execute = session.execute

        async def failing_execute(*args, **kwargs):
            raise RuntimeError("Injected DB failure")

        session.execute = failing_execute

        with pytest.raises(RuntimeError, match="Injected DB failure"):
            await checkpointer.flush(session, thread_id)

    # Verify the pending item is STILL in the buffer, not popped!
    assert len(checkpointer._pending_flushes[thread_id]) == 1

    # Flush again with real DB
    async with session_factory() as session:
        async with session.begin():
            await checkpointer.flush(session, thread_id)

    # Verify it flushed successfully
    assert thread_id not in checkpointer._pending_flushes

