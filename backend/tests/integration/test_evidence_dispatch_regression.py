"""Offline regression test proving real evidence dispatch and adapter wiring.

Conforms to spec 0002.
Verifies that:
1. SQS TASK_DISPATCH envelope processed through the runtime entrypoint constructs
   live adapters, executes against provider responses, immutably persists observations
   into PostgreSQL source_observations, and completes under lease fencing.
2. Missing provider credentials produce explicit unavailable results in worker_results,
   never synthetic fixture evidence and never successful no-op collection.
3. SqsTransportAdapter routes EVIDENCE worker dispatches to the dedicated queue.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select

from config.config import settings
from functions.api.models import TripModel
from functions.evidence_collector.consumer import process_record as evidence_process_record
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.execution.models import (
    ExecutionModel,
    OutboxEventModel,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.execution.transport import OutboxMessage, SqsTransportAdapter


async def _create_test_task_and_execution(
    session_factory: Any,
    *,
    worker: str = "EVIDENCE",
    action: str = "collect",
    payload: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper creating a test execution and task in PostgreSQL."""
    now = datetime.now(UTC)
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with session_factory() as session:
        trip = TripModel(
            id=trip_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(trip)

        execution = ExecutionModel(
            id=execution_id,
            owner_id=owner_id,
            trip_id=trip_id,
            workflow="TRAVEL",
            status="WAITING_TASKS",
            context_version=1,
            budget={
                "remaining_tool_calls": 5,
                "remaining_cost_minor": 500,
                "currency": "INR",
                "reserved_tool_calls": 0,
                "reserved_cost_minor": 0,
                "spent_tool_calls": 0,
                "spent_cost_minor": 0,
            },
            deadline_at=now + timedelta(minutes=10),
            created_at=now,
            updated_at=now,
        )
        session.add(execution)

        task = TaskModel(
            id=task_id,
            execution_id=execution_id,
            parent_task_id=None,
            worker=worker,
            action=action,
            payload_ref=payload or {},
            input_hash="hash_test_regression",
            idempotency_key=f"idem-{task_id}",
            attempt=0,
            max_attempts=3,
            context_version=1,
            status="PENDING",
            deadline_at=now + timedelta(minutes=5),
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.commit()

    return execution_id, task_id


@pytest.mark.asyncio
async def test_offline_evidence_dispatch_envelope_runtime_persistence_and_fencing(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies actual dispatch envelope executes live adapter against mocked provider,

    persisting immutable observations into PostgreSQL and completing under lease fencing.
    """
    monkeypatch.setattr(settings, "SERPAPI_API_KEY", "test_serpapi_token_12345")

    mock_serpapi_response = {
        "search_metadata": {"status": "Success"},
        "organic_results": [
            {
                "position": 1,
                "title": "Dakshineswar Kali Temple Timings",
                "link": "https://dakshineswarkalitemple.org/timings",
                "snippet": "Temple opens daily at 06:00 AM and closes at 12:30 PM, reopens at 03:00 PM.",
                "displayed_link": "https://dakshineswarkalitemple.org",
            }
        ],
    }

    task_payload = {
        "queries": ["Dakshineswar temple opening timings"],
        "budget": {
            "remaining_tool_calls": 5,
            "remaining_cost_minor": 500,
            "currency": "INR",
        },
    }
    execution_id, task_id = await _create_test_task_and_execution(
        session_factory,
        payload=task_payload,
    )

    event_id = uuid.uuid4()
    sqs_record = {
        "messageId": f"msg-{event_id}",
        "body": json.dumps(
            {
                "event_id": str(event_id),
                "execution_id": str(execution_id),
                "task_id": str(task_id),
                "kind": "TASK_DISPATCH",
                "attempts": 1,
                "payload": {
                    "task_id": str(task_id),
                    "execution_id": str(execution_id),
                    "worker": "EVIDENCE",
                    "action": "collect",
                    "attempt": 0,
                    "context_version": 1,
                    "payload": task_payload,
                },
            }
        ),
    }

    # Mock HTTP client response to execute provider parser offline without internet
    mock_resp = httpx.Response(
        status_code=200,
        json=mock_serpapi_response,
        request=httpx.Request("GET", "https://serpapi.com/search"),
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp

        # Invoke dedicated collector consumer with NO injected adapters or fixtures
        await evidence_process_record(sqs_record, session_factory)

    # 1. Verify immutable observation persistence in PostgreSQL
    async with session_factory() as session:
        stmt = select(SourceObservationModel).where(
            SourceObservationModel.execution_id == execution_id
        )
        res = await session.execute(stmt)
        observations = res.scalars().all()

        assert len(observations) == 1
        obs = observations[0]
        assert obs.canonical_url == "https://dakshineswarkalitemple.org/timings"
        assert obs.publisher == "dakshineswarkalitemple.org"
        assert obs.extraction_method == "SERPAPI_SEARCH"
        assert "Temple opens daily at 06:00 AM" in obs.excerpt_text
        assert obs.content_hash is not None

        # 2. Verify fenced task completion
        task_stmt = select(TaskModel).where(TaskModel.id == task_id)
        task_row = (await session.execute(task_stmt)).scalar_one()
        assert task_row.status == "COMPLETED"
        assert task_row.lease_token is None
        assert task_row.result_id is not None

        # 3. Verify worker result links persisted observation
        res_stmt = select(WorkerResultModel).where(WorkerResultModel.id == task_row.result_id)
        result_row = (await session.execute(res_stmt)).scalar_one()
        output = result_row.output_ref
        assert len(output["observations"]) == 1
        assert output["observations"][0]["observation_id"] == str(obs.id)
        assert output["unavailable_sources"] == []
        assert output["budget_exhausted"] is False

        # 4. Verify outbox event emitted for graph resumption
        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.task_id == task_id,
            OutboxEventModel.kind == "TASK_COMPLETED",
        )
        outbox_row = (await session.execute(outbox_stmt)).scalar_one_or_none()
        assert outbox_row is not None


@pytest.mark.asyncio
async def test_offline_evidence_dispatch_missing_config_produces_unavailable_result(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that missing provider configuration produces an explicit unavailable result,

    never synthetic evidence and never successful no-op collection.
    """
    monkeypatch.setattr(settings, "SERPAPI_API_KEY", None)

    task_payload = {
        "queries": ["hotels near Victoria Memorial"],
        "budget": {
            "remaining_tool_calls": 5,
            "remaining_cost_minor": 500,
            "currency": "INR",
        },
    }
    execution_id, task_id = await _create_test_task_and_execution(
        session_factory,
        payload=task_payload,
    )

    event_id = uuid.uuid4()
    sqs_record = {
        "messageId": f"msg-{event_id}",
        "body": json.dumps(
            {
                "event_id": str(event_id),
                "execution_id": str(execution_id),
                "task_id": str(task_id),
                "kind": "TASK_DISPATCH",
                "attempts": 1,
                "payload": {
                    "task_id": str(task_id),
                    "execution_id": str(execution_id),
                    "worker": "EVIDENCE",
                    "action": "collect",
                    "attempt": 0,
                    "context_version": 1,
                    "payload": task_payload,
                },
            }
        ),
    }

    # Execute dedicated collector consumer without any injected adapters
    await evidence_process_record(sqs_record, session_factory)

    async with session_factory() as session:
        # Zero synthetic observations persisted
        obs_stmt = select(SourceObservationModel).where(
            SourceObservationModel.execution_id == execution_id
        )
        observations = (await session.execute(obs_stmt)).scalars().all()
        assert len(observations) == 0

        # Task is completed with explicit unavailable result
        task_stmt = select(TaskModel).where(TaskModel.id == task_id)
        task_row = (await session.execute(task_stmt)).scalar_one()
        assert task_row.status == "COMPLETED"

        res_stmt = select(WorkerResultModel).where(WorkerResultModel.id == task_row.result_id)
        result_row = (await session.execute(res_stmt)).scalar_one()
        output = result_row.output_ref

        # Explicit unavailable record detailing missing credentials
        assert output["observations"] == []
        assert len(output["unavailable_sources"]) == 1
        unavailable = output["unavailable_sources"][0]
        assert unavailable["status"] == "UNAVAILABLE"
        assert unavailable["source_type"] == "SEARCH"
        assert "SERPAPI_API_KEY is not configured" in unavailable["error"]


@pytest.mark.asyncio
async def test_outbox_transport_routes_evidence_worker_to_dedicated_queue() -> None:
    """Verifies that SqsTransportAdapter routes EVIDENCE worker dispatches to the dedicated queue."""
    mock_sqs = MagicMock()
    mock_sqs.send_message = MagicMock(return_value={"MessageId": "msg-123"})

    transport = SqsTransportAdapter(
        queue_url="https://sqs.ap-south-1.amazonaws.com/123/generic-queue",
        sqs_client=mock_sqs,
        worker_queues={
            "EVIDENCE": "https://sqs.ap-south-1.amazonaws.com/123/evidence-collector-queue",
        },
    )

    now = datetime.now(UTC)
    evidence_msg = OutboxMessage(
        event_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        kind="TASK_DISPATCH",
        payload={"worker": "EVIDENCE", "action": "collect"},
        attempts=0,
        created_at=now,
    )
    await transport.publish(evidence_msg)

    assert mock_sqs.send_message.call_count == 1
    call_args = mock_sqs.send_message.call_args[1]
    assert call_args["QueueUrl"] == "https://sqs.ap-south-1.amazonaws.com/123/evidence-collector-queue"

    other_msg = OutboxMessage(
        event_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        kind="TASK_DISPATCH",
        payload={"worker": "DECISION", "action": "plan"},
        attempts=0,
        created_at=now,
    )
    await transport.publish(other_msg)

    assert mock_sqs.send_message.call_count == 2
    second_call_args = mock_sqs.send_message.call_args_list[1][1]
    assert second_call_args["QueueUrl"] == "https://sqs.ap-south-1.amazonaws.com/123/generic-queue"
