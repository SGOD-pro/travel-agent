"""Unit tests for spec 0003 Slice 2: Orchestrator Transport Routing and LangGraph Escalation Node.

Covers:
- AC-1: LangGraph escalation node enforces 3-task ceiling in emission order, state-level
  URL deduplication, replay-safe slot limits, and deterministic idempotency keys.
- AC-2: SqsTransportAdapter routes worker="BROWSER" task dispatches to BROWSER_WORKER_QUEUE_URL
  via environment configuration and constructor injection without importing browser worker code.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from functions.orchestrator.execution.transport import (
    OutboxMessage,
    SqsTransportAdapter,
    build_sqs_transport,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    build_trip_planning_graph,
    escalate_dynamic_evidence_node,
)

GENERIC_QUEUE_URL = "https://sqs.ap-south-1.amazonaws.com/123/generic-queue"
BROWSER_QUEUE_URL = "https://sqs.ap-south-1.amazonaws.com/123/browser-worker-queue"
EVIDENCE_QUEUE_URL = "https://sqs.ap-south-1.amazonaws.com/123/evidence-collector-queue"


# ============================================================================
# 1. Transport Routing Tests (AC-2)
# ============================================================================


@pytest.mark.asyncio
async def test_sqs_transport_routes_browser_worker_to_dedicated_queue_via_worker_queues() -> None:
    """Verifies that SqsTransportAdapter routes worker='BROWSER' to injected BROWSER queue URL."""
    mock_sqs = MagicMock()
    mock_sqs.send_message = MagicMock(return_value={"MessageId": "msg-browser-1"})

    transport = SqsTransportAdapter(
        queue_url=GENERIC_QUEUE_URL,
        sqs_client=mock_sqs,
        worker_queues={
            "BROWSER": BROWSER_QUEUE_URL,
            "EVIDENCE": EVIDENCE_QUEUE_URL,
        },
    )

    msg = OutboxMessage(
        event_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        kind="TASK_DISPATCH",
        payload={
            "worker": "BROWSER",
            "action": "render",
            "payload": {"url": "https://example.com/dynamic"},
        },
        attempts=0,
        created_at=datetime.now(UTC),
    )

    await transport.publish(msg)

    assert mock_sqs.send_message.call_count == 1
    call_args = mock_sqs.send_message.call_args[1]
    assert call_args["QueueUrl"] == BROWSER_QUEUE_URL


@pytest.mark.asyncio
async def test_sqs_transport_routes_browser_worker_fallback_to_env_var() -> None:
    """Verifies that SqsTransportAdapter falls back to BROWSER_WORKER_QUEUE_URL in os.environ."""
    mock_sqs = MagicMock()
    mock_sqs.send_message = MagicMock(return_value={"MessageId": "msg-browser-env"})

    transport = SqsTransportAdapter(
        queue_url=GENERIC_QUEUE_URL,
        sqs_client=mock_sqs,
        worker_queues={},  # Empty worker_queues dict
    )

    msg = OutboxMessage(
        event_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        kind="TASK_DISPATCH",
        payload={
            "worker": "BROWSER",
            "action": "render",
        },
        attempts=0,
        created_at=datetime.now(UTC),
    )

    with patch.dict("os.environ", {"BROWSER_WORKER_QUEUE_URL": BROWSER_QUEUE_URL}):
        await transport.publish(msg)

    assert mock_sqs.send_message.call_count == 1
    call_args = mock_sqs.send_message.call_args[1]
    assert call_args["QueueUrl"] == BROWSER_QUEUE_URL


@pytest.mark.asyncio
async def test_sqs_transport_routes_non_browser_worker_to_generic_queue() -> None:
    """Verifies that non-browser workers route to generic queue when unconfigured."""
    mock_sqs = MagicMock()
    mock_sqs.send_message = MagicMock(return_value={"MessageId": "msg-other"})

    transport = SqsTransportAdapter(
        queue_url=GENERIC_QUEUE_URL,
        sqs_client=mock_sqs,
        worker_queues={"BROWSER": BROWSER_QUEUE_URL},
    )

    msg = OutboxMessage(
        event_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        kind="TASK_DISPATCH",
        payload={"worker": "OPTIMIZER", "action": "solve"},
        attempts=0,
        created_at=datetime.now(UTC),
    )

    await transport.publish(msg)

    assert mock_sqs.send_message.call_count == 1
    call_args = mock_sqs.send_message.call_args[1]
    assert call_args["QueueUrl"] == GENERIC_QUEUE_URL


def test_build_sqs_transport_populates_browser_queue_from_env() -> None:
    """Verifies build_sqs_transport initializes BROWSER in worker_queues when env var is present."""
    mock_boto = MagicMock()
    mock_client = MagicMock()
    mock_boto.client.return_value = mock_client

    env_vars = {
        "OUTBOX_QUEUE_URL": GENERIC_QUEUE_URL,
        "BROWSER_WORKER_QUEUE_URL": BROWSER_QUEUE_URL,
        "EVIDENCE_COLLECTOR_QUEUE_URL": EVIDENCE_QUEUE_URL,
    }

    with patch.dict("os.environ", env_vars), patch.dict("sys.modules", {"boto3": mock_boto}):
        transport = build_sqs_transport()
        assert isinstance(transport, SqsTransportAdapter)
        assert transport.worker_queues.get("BROWSER") == BROWSER_QUEUE_URL
        assert transport.worker_queues.get("EVIDENCE") == EVIDENCE_QUEUE_URL


def test_transport_has_zero_imports_from_browser_function() -> None:
    """Verifies strict architectural modularity: transport does not import functions.browser."""
    assert "functions.browser" not in sys.modules.get("functions.orchestrator.execution.transport", {}).__class__.__module__
    import functions.orchestrator.execution.transport as t_mod

    # Inspect module globals
    for name, val in vars(t_mod).items():
        if hasattr(val, "__module__") and val.__module__:
            assert not val.__module__.startswith("functions.browser"), (
                f"Forbidden reverse dependency detected: {name} from {val.__module__}"
            )


# ============================================================================
# 2. LangGraph Escalation Node Tests (AC-1)
# ============================================================================


def test_escalate_dynamic_evidence_node_empty_state() -> None:
    """Verifies that an empty state emits no pending tasks and empty dispatched list."""
    state: TripPlanningState = {}
    result = escalate_dynamic_evidence_node(state)
    assert result["pending_tasks"] == []
    assert result["dispatched_browser_urls"] == []


def test_escalate_dynamic_evidence_node_enqueues_up_to_3_tasks_in_emission_order() -> None:
    """Verifies AC-1: enqueues up to 3 BROWSER tasks in emission order when 5 requests arrive."""
    exec_id = str(uuid.uuid4())
    requests: list[dict[str, Any]] = [
        {
            "url": f"https://example.com/hotel/{i}",
            "canonical_url": f"https://example.com/hotel/{i}",
            "category": "lodging",
            "reason": f"DYNAMIC_JS_{i}",
        }
        for i in range(1, 6)  # 5 requests
    ]

    state: TripPlanningState = {
        "execution_id": exec_id,
        "task_outputs": {
            "evidence-task-1": {
                "observations": [],
                "browser_requests": requests,
            }
        },
    }

    result = escalate_dynamic_evidence_node(state)

    # 1. Exactly 3 tasks enqueued (max ceiling)
    pending_tasks = result["pending_tasks"]
    assert len(pending_tasks) == 3

    # 2. Preserves emission order (hotels 1, 2, 3)
    for idx, task in enumerate(pending_tasks, start=1):
        assert task["worker"] == "BROWSER"
        assert task["action"] == "render"
        assert task["deadline_seconds"] == 60
        assert task["max_attempts"] == 3

        expected_url = f"https://example.com/hotel/{idx}"
        expected_hash = hashlib.sha256(expected_url.encode("utf-8")).hexdigest()
        assert task["idempotency_key"] == f"{exec_id}:render:{expected_hash[:16]}"

        payload = task["payload"]
        assert payload["url"] == expected_url
        assert payload["canonical_url"] == expected_url
        assert payload["wait_until"] == "networkidle"
        assert payload["timeout_seconds"] == 20

    # 3. State records dispatched_browser_urls
    dispatched = result["dispatched_browser_urls"]
    assert len(dispatched) == 3
    assert set(dispatched) == {
        "https://example.com/hotel/1",
        "https://example.com/hotel/2",
        "https://example.com/hotel/3",
    }
    assert result["current_step"] == "dynamic_evidence_escalated"


def test_escalate_dynamic_evidence_node_replay_safe_deduplication() -> None:
    """Verifies that replaying the node on an existing checkpoint does not duplicate tasks."""
    exec_id = str(uuid.uuid4())
    requests: list[dict[str, Any]] = [
        {
            "url": "https://example.com/hotel/1",
            "canonical_url": "https://example.com/hotel/1",
            "category": "lodging",
            "reason": "DYNAMIC_JS_1",
        },
        {
            "url": "https://example.com/hotel/2",
            "canonical_url": "https://example.com/hotel/2",
            "category": "lodging",
            "reason": "DYNAMIC_JS_2",
        },
    ]

    # Replay simulation: state already holds hotel/1 and hotel/2 in dispatched_browser_urls
    state: TripPlanningState = {
        "execution_id": exec_id,
        "dispatched_browser_urls": [
            "https://example.com/hotel/1",
            "https://example.com/hotel/2",
        ],
        "task_outputs": {
            "evidence-task-1": {
                "browser_requests": requests,
            }
        },
    }

    result = escalate_dynamic_evidence_node(state)

    # All requests already dispatched -> zero new pending tasks
    assert result["pending_tasks"] == []
    assert set(result["dispatched_browser_urls"]) == {
        "https://example.com/hotel/1",
        "https://example.com/hotel/2",
    }


def test_escalate_dynamic_evidence_node_remaining_slots_budget() -> None:
    """Verifies that with 2 existing dispatched URLs, only 1 new task is enqueued from 3 candidates."""
    exec_id = str(uuid.uuid4())
    requests: list[dict[str, Any]] = [
        {"url": f"https://example.com/hotel/{i}", "canonical_url": f"https://example.com/hotel/{i}"}
        for i in range(3, 6)  # hotels 3, 4, 5
    ]

    state: TripPlanningState = {
        "execution_id": exec_id,
        "dispatched_browser_urls": [
            "https://example.com/hotel/1",
            "https://example.com/hotel/2",
        ],
        "task_outputs": {
            "evidence-task-1": {
                "browser_requests": requests,
            }
        },
    }

    result = escalate_dynamic_evidence_node(state)

    # 1. Only 1 slot was available (3 - 2 = 1)
    assert len(result["pending_tasks"]) == 1
    assert result["pending_tasks"][0]["payload"]["canonical_url"] == "https://example.com/hotel/3"

    # 2. Total dispatched is now 3
    assert len(result["dispatched_browser_urls"]) == 3
    assert set(result["dispatched_browser_urls"]) == {
        "https://example.com/hotel/1",
        "https://example.com/hotel/2",
        "https://example.com/hotel/3",
    }


def test_escalate_dynamic_evidence_node_deduplicates_duplicate_urls_in_same_batch() -> None:
    """Verifies that duplicate URLs in the same task output batch are deduplicated."""
    exec_id = str(uuid.uuid4())
    requests: list[dict[str, Any]] = [
        {"url": "https://example.com/hotel/dup", "canonical_url": "https://example.com/hotel/dup"},
        {"url": "https://example.com/hotel/dup?src=1", "canonical_url": "https://example.com/hotel/dup"},
        {"url": "https://example.com/hotel/unique", "canonical_url": "https://example.com/hotel/unique"},
    ]

    state: TripPlanningState = {
        "execution_id": exec_id,
        "task_outputs": {"task-1": {"browser_requests": requests}},
    }

    result = escalate_dynamic_evidence_node(state)
    assert len(result["pending_tasks"]) == 2
    urls = [t["payload"]["canonical_url"] for t in result["pending_tasks"]]
    assert urls == ["https://example.com/hotel/dup", "https://example.com/hotel/unique"]


def test_escalate_dynamic_evidence_node_preserves_optional_fields() -> None:
    """Verifies custom wait_until, timeout, selector_target, and UUID placeholder propagation."""
    placeholder_uuid = uuid.uuid4()
    req = {
        "url": "https://example.com/spa",
        "canonical_url": "https://example.com/spa",
        "category": "transit",
        "reason": "NOSCRIPT_REQUIRED",
        "wait_until": "domcontentloaded",
        "timeout_seconds": 45,
        "selector_target": "div.train-table",
        "placeholder_observation_id": str(placeholder_uuid),
        "deadline_seconds": 90,
        "max_attempts": 2,
    }

    state: TripPlanningState = {
        "trip_id": "trip-abc",
        "task_outputs": {"task-1": {"browser_requests": [req]}},
    }

    result = escalate_dynamic_evidence_node(state)
    assert len(result["pending_tasks"]) == 1
    task = result["pending_tasks"][0]

    assert task["deadline_seconds"] == 90
    assert task["max_attempts"] == 2

    payload = task["payload"]
    assert payload["wait_until"] == "domcontentloaded"
    assert payload["timeout_seconds"] == 45
    assert payload["selector_target"] == "div.train-table"
    assert payload["placeholder_observation_id"] == str(placeholder_uuid)


@pytest.mark.asyncio
async def test_trip_planning_workflow_e2e_with_escalation_node() -> None:
    """Verifies that trip_planning_workflow compiles and executes cleanly end-to-end with escalation node."""
    graph = build_trip_planning_graph()
    workflow = graph.compile()

    state = {
        "trip_id": str(uuid.uuid4()),
        "version": 1,
        "brief": {
            "origin_name": "Kolkata",
            "origin_lat": 22.5726,
            "origin_lng": 88.3639,
            "destinations": [
                {
                    "name": "Digha",
                    "latitude": 21.6266,
                    "longitude": 87.5074,
                    "stay_days": 2,
                    "confidence": 0.95,
                }
            ],
            "start_date": "2026-11-01",
            "end_date": "2026-11-03",
            "travelers": 2,
            "rooms": 1,
            "budget": {"amount": "25000.00", "currency": "INR"},
        },
        "_offline_fixture_mode": True,
        "task_outputs": {
            "search-1": {
                "observations": [
                    {
                        "observation_id": str(uuid.uuid4()),
                        "canonical_url": "https://example.com/digha",
                        "content_hash": "hash123",
                        "excerpt_text": "Digha beach resort hotel lodging and sightseeing travel guide",
                        "extraction_method": "SCRAPLING_HTTP",
                        "artifact_ref": {"gps": {"lat": 21.6266, "lng": 87.5074}},
                    }
                ],
                "search_completed": True,
            }
        },
        "static_search_completed": True,
        "required_evidence_satisfied": True,
    }

    result = await workflow.ainvoke(state)
    assert result["status"] == "completed"
    assert result["current_step"] == "proposal_synthesized"
    assert "proposal" in result
    assert result.get("pending_tasks") == []


def test_escalate_dynamic_evidence_node_configurable_budget_ceiling() -> None:
    """Verifies that BROWSER_MAX_ESCALATION_TASKS environment variable adjusts escalation ceiling."""
    requests = [
        {"url": f"https://example.com/hotel/{i}", "canonical_url": f"https://example.com/hotel/{i}"}
        for i in range(1, 6)
    ]
    state: TripPlanningState = {
        "execution_id": "test-exec-budget",
        "task_outputs": {"task-1": {"browser_requests": requests}},
    }

    # Override budget ceiling to 2 via environment
    with patch.dict(os.environ, {"BROWSER_MAX_ESCALATION_TASKS": "2"}):
        result = escalate_dynamic_evidence_node(state)
        assert len(result["pending_tasks"]) == 2
        assert len(result["dispatched_browser_urls"]) == 2

    # Override budget ceiling to 4 via environment
    with patch.dict(os.environ, {"BROWSER_MAX_ESCALATION_TASKS": "4"}):
        result = escalate_dynamic_evidence_node(state)
        assert len(result["pending_tasks"]) == 4
        assert len(result["dispatched_browser_urls"]) == 4


def test_escalate_dynamic_evidence_node_deterministic_fallback_when_execution_id_missing() -> None:
    """Verifies that absent execution_id and trip_id produce deterministic idempotency keys across replays."""
    state: TripPlanningState = {
        "task_outputs": {
            "task-1": {
                "browser_requests": [
                    {"url": "https://example.com/resort", "canonical_url": "https://example.com/resort"}
                ]
            }
        }
    }

    result1 = escalate_dynamic_evidence_node(state)
    result2 = escalate_dynamic_evidence_node(state)

    assert len(result1["pending_tasks"]) == 1
    assert len(result2["pending_tasks"]) == 1

    key1 = result1["pending_tasks"][0]["idempotency_key"]
    key2 = result2["pending_tasks"][0]["idempotency_key"]

    assert key1.startswith("default-execution:render:")
    assert key1 == key2, "Idempotency key must remain deterministic across evaluations even when execution_id is absent"

