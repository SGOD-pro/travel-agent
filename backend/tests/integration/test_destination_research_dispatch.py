"""Integration tests for serialized destination research dispatch and collector execution.

Conforms to 0007 bounded destination research requirements:
1. Serialized dispatch carries explicit destination coverage and query purposes.
2. Actual collector execution with controlled providers tags observations with query_purpose, destination, and engine.
3. Road/advisory research routes to engine=google; venues route to engine=google_maps.
4. Budget accounting enforces tool call and cost ceilings (<= 4 queries, 200 minor INR).
5. Uncovered destinations are explicitly recorded when destinations exceed query ceilings.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from config.config import settings
from functions.api.models import TripModel
from functions.evidence_collector.consumer import process_record as evidence_process_record
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.query_planner import QueryPlanningService
from functions.orchestrator.execution.models import (
    ExecutionModel,
    TaskModel,
    WorkerResultModel,
)


async def _create_test_execution_and_task(
    session_factory: Any,
    *,
    payload: dict[str, Any],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Creates a test execution and task record in PostgreSQL."""
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
                "remaining_tool_calls": 4,
                "remaining_cost_minor": 200,
                "currency": "INR",
                "reserved_tool_calls": 0,
                "reserved_cost_minor": 0,
                "spent_tool_calls": 0,
                "spent_cost_minor": 0,
            },
            deadline_at=now + timedelta(minutes=15),
            created_at=now,
            updated_at=now,
        )
        session.add(execution)

        task = TaskModel(
            id=task_id,
            execution_id=execution_id,
            parent_task_id=None,
            worker="EVIDENCE",
            action="collect",
            payload_ref=payload,
            input_hash="test_destination_dispatch_hash",
            idempotency_key=f"dest-task-{task_id}",
            attempt=0,
            max_attempts=3,
            context_version=1,
            status="PENDING",
            deadline_at=now + timedelta(minutes=10),
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.commit()

    return execution_id, task_id


@pytest.mark.asyncio
async def test_serialized_dispatch_multi_destination_collector_execution(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies serialized dispatch -> actual collector with controlled SerpAPI responses."""
    monkeypatch.setattr(settings, "SERPAPI_API_KEY", "controlled_test_key_123")

    brief = {
        "origin_name": "Bengaluru",
        "destinations": [
            {"name": "Mysuru"},
            {"name": "Madikeri"},
        ],
        "traveler_preferences": ["coffee", "heritage"],
    }

    # 1. Plan queries using deterministic coverage planner
    planned = QueryPlanningService.plan_destination_queries(brief, max_queries=4)
    assert len(planned) == 4
    assert planned.covered_destinations == ["Mysuru", "Madikeri"]

    budget_dict = QueryPlanningService.calculate_collector_budget(planned)
    assert budget_dict["remaining_tool_calls"] == 4
    assert budget_dict["remaining_cost_minor"] == 200

    payload = {
        "queries": [dict(q) for q in planned],
        "budget": budget_dict,
        "destination": "Mysuru",
        "destinations": ["Mysuru", "Madikeri"],
        "covered_destinations": planned.covered_destinations,
        "uncovered_destinations": planned.uncovered_destinations,
        "coverage_reason": planned.coverage_reason,
        "origin": "Bengaluru",
    }

    # 2. Persist task and execution
    execution_id, task_id = await _create_test_execution_and_task(
        session_factory, payload=payload
    )

    # 3. Controlled mock provider: returns place results for venue queries and organic for advisory
    captured_requests: list[dict[str, Any]] = []

    mock_client = AsyncMock()

    async def mock_get(url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> MagicMock:
        p = dict(params or {})
        captured_requests.append(p)
        engine = p.get("engine", "google")
        q_str = p.get("q", "")

        resp = MagicMock()
        resp.status_code = 200

        if engine == "google":
            # Road advisory organic results
            resp.json.return_value = {
                "search_metadata": {"id": f"search_{len(captured_requests)}", "status": "Success"},
                "organic_results": [
                    {
                        "title": "NH275 Bengaluru-Mysuru Expressway Advisory",
                        "link": "https://kpwd.gov.in/advisories/nh275",
                        "snippet": "Expressway is operational with active tolling. Moderate fog in morning.",
                    }
                ],
            }
        else:
            # Place results on google_maps
            if "Madikeri" in q_str:
                dest_in_query = "Madikeri"
            elif "food" in q_str or "restaurant" in q_str or "dining" in q_str:
                dest_in_query = "Mysuru-Dining"
            else:
                dest_in_query = "Mysuru"
            resp.json.return_value = {
                "search_metadata": {"id": f"search_{len(captured_requests)}", "status": "Success"},
                "places_results": [
                    {
                        "title": f"{dest_in_query} Sights Discovery",
                        "link": f"https://karnatakatourism.org/{dest_in_query.lower()}",
                        "description": f"Top attraction in {dest_in_query}",
                        "gps_coordinates": {"latitude": 12.3, "longitude": 76.6},
                    }
                ],
            }
        return resp

    mock_client.get = mock_get

    # 4. Construct SQS record representing TASK_DISPATCH event
    envelope_data = {
        "kind": "TASK_DISPATCH",
        "event_id": str(uuid.uuid4()),
        "task_id": str(task_id),
        "execution_id": str(execution_id),
        "context_version": 1,
        "worker": "EVIDENCE",
        "action": "collect",
        "payload": payload,
        "attempt": 0,
        "max_attempts": 3,
        "deadline_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    sqs_record = {
        "messageId": f"msg-{task_id}",
        "receiptHandle": "rh-12345",
        "body": json.dumps(envelope_data),
    }

    # 5. Execute through evidence collector consumer
    with patch("httpx.AsyncClient", return_value=mock_client):
        await evidence_process_record(sqs_record, session_factory=session_factory)

    # 6. Verify HTTP engine routing
    search_requests = [r for r in captured_requests if "engine" in r]
    assert len(search_requests) == 4
    # Advisory query routed to engine=google
    adv_requests = [r for r in search_requests if r.get("engine") == "google"]
    assert len(adv_requests) == 1
    assert "road travel advisory" in adv_requests[0].get("q", "")

    # Venue queries routed to engine=google_maps
    maps_requests = [r for r in search_requests if r.get("engine") == "google_maps"]
    assert len(maps_requests) == 3
    # Mysuru attractions and Madikeri attractions are both requested
    assert any("Mysuru" in r.get("q", "") for r in maps_requests)
    assert any("Madikeri" in r.get("q", "") for r in maps_requests)

    # 7. Verify persisted observations and metadata in PostgreSQL
    async with session_factory() as session:
        obs_rows = (
            (
                await session.execute(
                    select(SourceObservationModel).where(
                        SourceObservationModel.execution_id == execution_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(obs_rows) >= 4

        # Verify observation metadata preservation
        advisory_obs = [
            o for o in obs_rows
            if o.artifact_ref and o.artifact_ref.get("query_purpose") == "advisory"
        ]
        assert len(advisory_obs) >= 1
        assert advisory_obs[0].artifact_ref["engine"] == "google"

        mysuru_obs = [
            o for o in obs_rows
            if o.artifact_ref
            and o.artifact_ref.get("destination") == "Mysuru"
            and o.artifact_ref.get("query_purpose") == "attractions"
        ]
        assert len(mysuru_obs) >= 1
        assert mysuru_obs[0].artifact_ref["engine"] == "google_maps"

        madikeri_obs = [
            o for o in obs_rows
            if o.artifact_ref
            and o.artifact_ref.get("destination") == "Madikeri"
            and o.artifact_ref.get("query_purpose") == "attractions"
        ]
        assert len(madikeri_obs) >= 1
        assert madikeri_obs[0].artifact_ref["engine"] == "google_maps"

        # 8. Verify WorkerResultModel and TaskModel status
        task_row = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task_row.status == "COMPLETED"

        wr_row = (
            await session.execute(
                select(WorkerResultModel).where(WorkerResultModel.task_id == task_id)
            )
        ).scalar_one()
        assert wr_row.status == "SUCCESS"
        assert wr_row.output_ref is not None

        # Verify budget usage
        usage = wr_row.output_ref.get("usage", {})
        assert usage.get("tool_calls_made") == 4
        assert usage.get("cost_minor") == 200
        assert usage.get("currency") == "INR"


@pytest.mark.asyncio
async def test_more_destinations_than_query_slots_dispatch_and_uncovered_metadata(
    session_factory: Any,
) -> None:
    """Verifies that exceeding query slots carries uncovered_destinations and coverage_reason."""
    brief = {
        "origin_name": "Bengaluru",
        "destinations": [
            {"name": "Mysuru"},
            {"name": "Madikeri"},
            {"name": "Ooty"},
            {"name": "Coonoor"},
            {"name": "Wayanad"},
        ],
    }

    planned = QueryPlanningService.plan_destination_queries(brief, max_queries=4)

    assert len(planned) == 4
    assert planned.covered_destinations == ["Mysuru", "Madikeri", "Ooty"]
    assert planned.uncovered_destinations == ["Coonoor", "Wayanad"]
    assert planned.is_complete_coverage is False
    assert planned.coverage_reason is not None

    payload = {
        "queries": [dict(q) for q in planned],
        "budget": QueryPlanningService.calculate_collector_budget(planned),
        "destination": "Mysuru",
        "destinations": ["Mysuru", "Madikeri", "Ooty", "Coonoor", "Wayanad"],
        "covered_destinations": planned.covered_destinations,
        "uncovered_destinations": planned.uncovered_destinations,
        "coverage_reason": planned.coverage_reason,
        "origin": "Bengaluru",
    }

    execution_id, task_id = await _create_test_execution_and_task(
        session_factory, payload=payload
    )

    async with session_factory() as session:
        task_row = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task_row.payload_ref["covered_destinations"] == ["Mysuru", "Madikeri", "Ooty"]
        assert task_row.payload_ref["uncovered_destinations"] == ["Coonoor", "Wayanad"]
        assert "Destination count (5) exceeds available research query slots (3)" in task_row.payload_ref["coverage_reason"]
        assert len(task_row.payload_ref["queries"]) == 4
