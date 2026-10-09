"""Integration tests for the Graph Runner Lambda/SQS consumer."""

import json
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from functions.orchestrator.execution.consumer import process_batch
from functions.orchestrator.execution.models import ExecutionModel, TaskModel
from tests.integration.test_graph_runner import (
    build_test_runner_graph,
    create_test_execution,
)


@pytest.fixture
def mock_create_graph(postgres_checkpointer):
    """Mocks the graph creation to use our test graph."""
    graph = build_test_runner_graph(postgres_checkpointer)
    with patch(
        "functions.orchestrator.execution.consumer.build_trip_planning_graph"
    ) as mock:
        mock.return_value.compile.return_value = graph
        yield mock


@pytest.fixture
def mock_get_session_factory(session_factory):
    """Mocks get_session_factory to use the test session factory."""
    with patch(
        "functions.orchestrator.execution.consumer.get_session_factory",
        return_value=session_factory,
    ) as mock:
        yield mock


class MockContext:
    def __init__(self, remaining_ms: int = 5000):
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


@pytest.mark.asyncio
async def test_consumer_handles_start_and_resume_batch(
    session_factory, postgres_checkpointer, mock_create_graph, mock_get_session_factory
) -> None:
    """Verifies that the consumer successfully handles a batch of valid START and RESUME events."""
    exec_id_start = await create_test_execution(session_factory, status="CREATED")
    exec_id_resume = await create_test_execution(session_factory, status="WAITING_TASKS")

    event = {
        "Records": [
            {
                "messageId": "msg-start",
                "body": json.dumps(
                    {
                        "id": str(uuid.uuid4()),
                        "kind": "EXECUTION_START",
                        "execution_id": str(exec_id_start),
                        "workflow": "TRAVEL",
                        "context_version": 1,
                        "payload_ref": {"options": {"budget": "medium"}},
                    }
                ),
            },
            {
                "messageId": "msg-resume",
                "body": json.dumps(
                    {
                        "id": str(uuid.uuid4()),
                        "kind": "EXECUTION_RESUME",
                        "execution_id": str(exec_id_resume),
                        "task_id": str(uuid.uuid4()),
                        "context_version": 1,
                    }
                ),
            },
        ]
    }

    # Process batch directly (awaiting the async function instead of using lambda_handler)
    # since lambda_handler uses asyncio.run() which can conflict with pytest-asyncio event loop.
    failures = await process_batch(event, MockContext())
    assert failures == []

    # Verify START worked
    async with session_factory() as session:
        execution = (
            await session.execute(
                select(ExecutionModel).where(ExecutionModel.id == exec_id_start)
            )
        ).scalar_one_or_none()
        assert execution is not None
        assert execution.status == "WAITING_TASKS"
        tasks = (
            await session.execute(
                select(TaskModel).where(TaskModel.execution_id == exec_id_start)
            )
        ).scalars().all()
        assert len(tasks) == 1

        # Verify RESUME worked (note: our test graph requires task_outputs to advance,
        # but at least the execution shouldn't fail and should be processed)
        execution_resume = (
            await session.execute(
                select(ExecutionModel).where(ExecutionModel.id == exec_id_resume)
            )
        ).scalar_one_or_none()
        assert execution_resume is not None


@pytest.mark.asyncio
async def test_consumer_handles_malformed_records(
    session_factory, postgres_checkpointer, mock_create_graph, mock_get_session_factory
) -> None:
    """Verifies that malformed JSON or missing fields result in batchItemFailures without crashing the batch."""
    exec_id = await create_test_execution(session_factory, status="CREATED")

    event = {
        "Records": [
            {
                "messageId": "msg-malformed-json",
                "body": "not-valid-json",
            },
            {
                "messageId": "msg-missing-fields",
                "body": json.dumps({"kind": "EXECUTION_START"}),  # missing execution_id
            },
            {
                "messageId": "msg-valid",
                "body": json.dumps(
                    {
                        "id": str(uuid.uuid4()),
                        "kind": "EXECUTION_START",
                        "execution_id": str(exec_id),
                        "context_version": 1,
                    }
                ),
            },
        ]
    }

    failures = await process_batch(event, MockContext())

    # First two should fail, third should succeed
    failed_ids = {f["itemIdentifier"] for f in failures}
    assert "msg-malformed-json" in failed_ids
    assert "msg-missing-fields" in failed_ids
    assert "msg-valid" not in failed_ids


@pytest.mark.asyncio
async def test_consumer_lambda_timeout(
    session_factory, postgres_checkpointer, mock_create_graph, mock_get_session_factory
) -> None:
    """Verifies that items are failed and remaining processing skipped if Lambda timeout is near."""
    exec_id_1 = await create_test_execution(session_factory, status="CREATED")
    exec_id_2 = await create_test_execution(session_factory, status="CREATED")

    event = {
        "Records": [
            {
                "messageId": "msg-timeout-1",
                "body": json.dumps(
                    {
                        "kind": "EXECUTION_START",
                        "execution_id": str(exec_id_1),
                    }
                ),
            },
            {
                "messageId": "msg-timeout-2",
                "body": json.dumps(
                    {
                        "kind": "EXECUTION_START",
                        "execution_id": str(exec_id_2),
                    }
                ),
            },
        ]
    }

    # 1500ms remaining is below the 2000ms threshold
    failures = await process_batch(event, MockContext(remaining_ms=1500))

    # Both should be failed because the loop hits the threshold and immediately fails them
    failed_ids = {f["itemIdentifier"] for f in failures}
    assert "msg-timeout-1" in failed_ids
    assert "msg-timeout-2" in failed_ids
    assert len(failed_ids) == 2


@pytest.mark.asyncio
async def test_consumer_handles_duplicates_gracefully(
    session_factory, postgres_checkpointer, mock_create_graph, mock_get_session_factory
) -> None:
    """Verifies that duplicate deliveries are acknowledged (no failures) due to fencing."""
    # Create an execution already in WAITING_TASKS (meaning START already ran)
    exec_id = await create_test_execution(session_factory, status="WAITING_TASKS")

    event = {
        "Records": [
            {
                "messageId": "msg-duplicate",
                "body": json.dumps(
                    {
                        "id": str(uuid.uuid4()),
                        "kind": "EXECUTION_START",
                        "execution_id": str(exec_id),
                        "context_version": 1,
                    }
                ),
            },
        ]
    }

    failures = await process_batch(event, MockContext())

    # Should be acknowledged without failure, because GraphRunnerService gracefully
    # returns GraphRunnerResult with tasks_dispatched=0.
    assert failures == []
