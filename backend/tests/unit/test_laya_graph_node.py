"""Unit tests for LangGraph route_evidence_decision_node and route_evidence_decision_edge.

Verifies:
- Phase 1: Static search dispatch enqueues search task in pending_tasks.
- Phase 2: Multi-choice evaluation dispatches browser escalation or advances to evaluation.
- Phase 3: Singleton evaluation bypass advances directly to solve_schedule.
- Edge routing: route_evidence_decision_edge directs to solve_schedule on evaluation, escalate_dynamic_evidence otherwise.
- Result reuse from laya_decisions state history.
- Telemetry recording in laya_decisions list.
- Fallback handling on client adapter failure.
"""

from __future__ import annotations

import uuid
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionPhase,
    DecisionStatus,
    DecisionType,
    LayaDecisionPort,
    LayaDecisionRequest,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    route_evidence_decision_edge,
    route_evidence_decision_node,
)


class MockLayaDecisionAdapter(LayaDecisionPort):
    """Controllable test adapter for graph node testing."""

    def __init__(self, envelope_to_return: DecisionEnvelope | None = None) -> None:
        self.envelope_to_return = envelope_to_return
        self.queried_requests: list[LayaDecisionRequest] = []

    async def query_decision(self, request: LayaDecisionRequest) -> DecisionEnvelope:
        self.queried_requests.append(request)
        if self.envelope_to_return is not None:
            return self.envelope_to_return
        return DecisionEnvelope(
            decision_id=request.decision_id,
            decision_type=request.decision_type,
            phase=request.phase,
            execution_id=request.execution_id,
            trip_id=request.trip_id,
            idempotency_key=request.idempotency_key,
            allowed_choice=DecisionAction.DISPATCH_BROWSER_ESCALATION,
            status=DecisionStatus.VALIDATED,
            confidence=0.85,
            answer_confidence=0.85,
            model_version="mock-laya",
            policy_version="laya-policy-v1",
            rationale="Approved browser escalation",
            latency_ms=12,
            request_digest="digest-123",
            created_at="2026-10-18T12:00:00Z",
        )

    async def check_health(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_route_evidence_decision_phase1_static_search_dispatch() -> None:
    """When static search is incomplete and required evidence is missing, enqueues collect task."""
    exec_id = str(uuid.uuid4())
    state: TripPlanningState = {
        "execution_id": exec_id,
        "trip_id": exec_id,
        "version": 1,
        "status": "in_progress",
        "brief": {
            "origin_name": "Kolkata",
            "destination_name": "Puri",
            "destinations": [{"name": "Puri", "latitude": None, "longitude": None}],
        },
        "discovered_places": [],
        "task_outputs": {},
        "static_search_completed": False,
        "remaining_search_budget": 1,
    }

    result = await route_evidence_decision_node(state)

    assert result["current_step"] == "evidence_search_routed"
    assert result["evidence_routing_action"] == DecisionAction.DISPATCH_EVIDENCE_SEARCH.value
    assert len(result["pending_tasks"]) == 1

    search_task = result["pending_tasks"][0]
    assert search_task["worker"] == "EVIDENCE"
    assert search_task["action"] == "collect"
    assert "queries" in search_task["payload"]
    assert len(search_task["payload"]["queries"]) >= 1
    assert search_task["payload"]["budget"]["cost_per_call_minor"] == 50
    assert search_task["payload"]["destination"] == "Puri"
    assert search_task["payload"]["origin"] == "Kolkata"
    assert search_task["payload"]["categories"] == ["attractions", "lodging", "transit"]

    # Verify edge routing directs to await_evidence_results before scheduling
    merged_state = cast(TripPlanningState, {**state, **result})
    assert route_evidence_decision_edge(merged_state) == "await_evidence_results"

    # Verify decision telemetry recorded in laya_decisions
    decisions = result["laya_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["phase"] == DecisionPhase.SEARCH_COMPLETION.value
    assert decisions[0]["action"] == DecisionAction.DISPATCH_EVIDENCE_SEARCH.value
    assert decisions[0]["status"] == DecisionStatus.DETERMINISTIC_SINGLETON.value
    assert decisions[0]["confidence"] is None
    assert decisions[0]["answer_confidence"] is None


@pytest.mark.asyncio
async def test_route_evidence_decision_phase2_browser_escalation_dispatch() -> None:
    """When unhandled browser requests exist and budget remains, dispatches browser escalation."""
    exec_id = str(uuid.uuid4())
    adapter = MockLayaDecisionAdapter()

    state: TripPlanningState = {
        "execution_id": exec_id,
        "trip_id": exec_id,
        "version": 1,
        "status": "in_progress",
        "brief": {
            "origin_name": "Kolkata",
            "destination_name": "Digha",
            "destinations": [{"name": "Digha", "latitude": 21.6266, "longitude": 87.5074}],
        },
        "discovered_places": [{"name": "Digha", "lat": 21.6266, "lng": 87.5074}],
        "task_outputs": {
            "search-1": {
                "observations": [
                    {
                        "observation_id": str(uuid.uuid4()),
                        "canonical_url": "https://example.com/digha",
                        "content_hash": "hash123",
                    }
                ],
                "search_completed": True,
            },
            "task-1": {
                "browser_requests": [
                    {"url": "https://hotel.example.com/rates", "canonical_url": "https://hotel.example.com/rates"}
                ]
            },
        },
        "dispatched_browser_urls": [],
        "static_search_completed": True,
        "_laya_adapter": adapter,
    }

    result = await route_evidence_decision_node(state)

    assert result["current_step"] == "browser_escalation_routed"
    assert result["evidence_routing_action"] == DecisionAction.DISPATCH_BROWSER_ESCALATION.value
    assert result["pending_tasks"] == []
    assert len(adapter.queried_requests) == 1
    assert adapter.queried_requests[0].phase == DecisionPhase.BROWSER_ELIGIBILITY

    # Verify edge routing directs to escalate_dynamic_evidence
    merged_state = cast(TripPlanningState, {**state, **result})
    assert route_evidence_decision_edge(merged_state) == "escalate_dynamic_evidence"

    # Verify telemetry
    decisions = result["laya_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["phase"] == DecisionPhase.BROWSER_ELIGIBILITY.value
    assert decisions[0]["action"] == DecisionAction.DISPATCH_BROWSER_ESCALATION.value
    assert decisions[0]["status"] == DecisionStatus.VALIDATED.value
    assert decisions[0]["confidence"] == 0.85


@pytest.mark.asyncio
async def test_route_evidence_decision_phase2_evaluation_bypass_to_solve_schedule() -> None:
    """When decision returns PROCEED_DETERMINISTIC_EVALUATION, routes directly to solve_schedule."""
    exec_id = str(uuid.uuid4())
    adapter = MockLayaDecisionAdapter(
        envelope_to_return=DecisionEnvelope(
            decision_id=uuid.uuid4(),
            decision_type=DecisionType.EVIDENCE_ROUTING,
            phase=DecisionPhase.BROWSER_ELIGIBILITY,
            execution_id=uuid.UUID(exec_id),
            trip_id=uuid.UUID(exec_id),
            idempotency_key="mock-key",
            allowed_choice=DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
            status=DecisionStatus.VALIDATED,
            confidence=0.91,
            answer_confidence=0.91,
            model_version="mock-laya",
            policy_version="laya-policy-v1",
            rationale="Evidence sufficient, bypass browser",
            latency_ms=15,
            request_digest="digest-456",
            created_at="2026-10-18T12:00:00Z",
        )
    )

    state: TripPlanningState = {
        "execution_id": exec_id,
        "trip_id": exec_id,
        "version": 1,
        "status": "in_progress",
        "brief": {
            "origin_name": "Kolkata",
            "destination_name": "Digha",
            "destinations": [{"name": "Digha", "latitude": 21.6266, "longitude": 87.5074}],
        },
        "discovered_places": [{"name": "Digha", "lat": 21.6266, "lng": 87.5074}],
        "task_outputs": {
            "search-1": {
                "observations": [
                    {
                        "observation_id": str(uuid.uuid4()),
                        "canonical_url": "https://example.com/digha",
                        "content_hash": "hash123",
                    }
                ],
                "search_completed": True,
            },
            "task-1": {
                "browser_requests": [
                    {"url": "https://hotel.example.com/rates", "canonical_url": "https://hotel.example.com/rates"}
                ]
            },
        },
        "static_search_completed": True,
        "_laya_adapter": adapter,
    }

    result = await route_evidence_decision_node(state)

    assert result["current_step"] == "deterministic_evaluation_routed"
    assert result["evidence_routing_action"] == DecisionAction.PROCEED_DETERMINISTIC_EVALUATION.value
    assert result["pending_tasks"] == []

    # Verify edge routing directs directly to solve_schedule
    merged_state = cast(TripPlanningState, {**state, **result})
    assert route_evidence_decision_edge(merged_state) == "solve_schedule"


@pytest.mark.asyncio
async def test_route_evidence_decision_result_reuse_from_state() -> None:
    """When decision with matching idempotency key and eligible action exists in state, reuses it."""
    exec_id = str(uuid.uuid4())
    adapter = MockLayaDecisionAdapter()

    # Pre-generate the expected idempotency key format
    from functions.orchestrator.decision.policy import LayaPolicyValidator
    from functions.orchestrator.decision.repository import compute_canonical_replay_digest

    sanitized_context = {
        "execution_id": exec_id,
        "phase": DecisionPhase.BROWSER_ELIGIBILITY.value,
        "category": "lodging",
        "destination": "Digha",
        "static_search_completed": True,
        "candidate_places_count": 1,
        "unhandled_browser_requests": 1,
        "browser_escalation_slots_remaining": 3,
        "sanitized_summary": "Discovered 1 places. Unhandled dynamic requests: 1.",
    }
    eligible = [
        DecisionAction.DISPATCH_BROWSER_ESCALATION,
        DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
    ]
    _, expected_idempotency_key = compute_canonical_replay_digest(
        execution_id=uuid.UUID(exec_id),
        phase=DecisionPhase.BROWSER_ELIGIBILITY,
        context_version=1,
        policy_version=LayaPolicyValidator.POLICY_VERSION,
        question_version="evidence_routing:v1",
        eligible_actions=eligible,
        sanitized_context=sanitized_context,
    )

    prior_decision: dict[str, Any] = {
        "decision_id": str(uuid.uuid4()),
        "phase": DecisionPhase.BROWSER_ELIGIBILITY.value,
        "action": DecisionAction.DISPATCH_BROWSER_ESCALATION.value,
        "status": DecisionStatus.VALIDATED.value,
        "confidence": 0.88,
        "answer_confidence": 0.88,
        "model_version": "cached-laya",
        "rationale": "Cached decision",
        "latency_ms": 10,
        "idempotency_key": expected_idempotency_key,
    }

    state: TripPlanningState = {
        "execution_id": exec_id,
        "trip_id": exec_id,
        "version": 1,
        "status": "in_progress",
        "brief": {
            "origin_name": "Kolkata",
            "destination_name": "Digha",
            "destinations": [{"name": "Digha", "latitude": 21.6266, "longitude": 87.5074}],
        },
        "discovered_places": [{"name": "Digha", "lat": 21.6266, "lng": 87.5074}],
        "task_outputs": {
            "search-1": {
                "observations": [
                    {
                        "observation_id": str(uuid.uuid4()),
                        "canonical_url": "https://example.com/digha",
                        "content_hash": "hash123",
                    }
                ],
                "search_completed": True,
            },
            "task-1": {
                "browser_requests": [
                    {"url": "https://hotel.example.com/rates", "canonical_url": "https://hotel.example.com/rates"}
                ]
            },
        },
        "static_search_completed": True,
        "laya_decisions": [prior_decision],
        "_laya_adapter": adapter,
    }

    result = await route_evidence_decision_node(state)

    # Adapter must NOT be queried due to successful result reuse
    assert len(adapter.queried_requests) == 0
    assert result["evidence_routing_action"] == DecisionAction.DISPATCH_BROWSER_ESCALATION.value


@pytest.mark.asyncio
async def test_route_evidence_decision_fallback_on_adapter_exception() -> None:
    """When adapter raises an exception, node gracefully falls back via deterministic table."""
    exec_id = str(uuid.uuid4())
    failing_adapter = MagicMock(spec=LayaDecisionPort)
    failing_adapter.query_decision = AsyncMock(side_effect=RuntimeError("Connection timeout to remote LAYA"))

    state: TripPlanningState = {
        "execution_id": exec_id,
        "trip_id": exec_id,
        "version": 1,
        "status": "in_progress",
        "brief": {
            "origin_name": "Kolkata",
            "destination_name": "Digha",
            "destinations": [{"name": "Digha", "latitude": 21.6266, "longitude": 87.5074}],
        },
        "discovered_places": [{"name": "Digha", "lat": 21.6266, "lng": 87.5074}],
        "task_outputs": {
            "search-1": {
                "observations": [
                    {
                        "observation_id": str(uuid.uuid4()),
                        "canonical_url": "https://example.com/digha",
                        "content_hash": "hash123",
                    }
                ],
                "search_completed": True,
            },
            "task-1": {
                "browser_requests": [
                    {"url": "https://hotel.example.com/rates", "canonical_url": "https://hotel.example.com/rates"}
                ]
            },
        },
        "static_search_completed": True,
        "_laya_adapter": failing_adapter,
    }

    result = await route_evidence_decision_node(state)

    # Workflow continues cleanly with fallback decision
    assert result["current_step"] in ("browser_escalation_routed", "deterministic_evaluation_routed")
    decisions = result["laya_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["status"] == DecisionStatus.FALLBACK_ERROR.value
    assert "Fallback: Connection timeout" in decisions[0]["rationale"]


@pytest.mark.asyncio
async def test_route_evidence_decision_persists_to_db_session() -> None:
    """When _db_session is provided, persists decision to database via LayaDecisionRepository."""
    exec_id = str(uuid.uuid4())
    adapter = MockLayaDecisionAdapter()
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    state: TripPlanningState = {
        "execution_id": exec_id,
        "trip_id": exec_id,
        "version": 1,
        "status": "in_progress",
        "brief": {
            "origin_name": "Kolkata",
            "destination_name": "Digha",
            "destinations": [{"name": "Digha", "latitude": 21.6266, "longitude": 87.5074}],
        },
        "discovered_places": [{"name": "Digha", "lat": 21.6266, "lng": 87.5074}],
        "task_outputs": {
            "search-1": {
                "observations": [
                    {
                        "observation_id": str(uuid.uuid4()),
                        "canonical_url": "https://example.com/digha",
                        "content_hash": "hash123",
                    }
                ],
                "search_completed": True,
            },
            "task-1": {
                "browser_requests": [
                    {"url": "https://hotel.example.com/rates", "canonical_url": "https://hotel.example.com/rates"}
                ]
            },
        },
        "static_search_completed": True,
        "_laya_adapter": adapter,
        "_db_session": mock_session,
    }

    result = await route_evidence_decision_node(state)
    assert result["current_step"] == "browser_escalation_routed"
    assert len(result["laya_decisions"]) == 1
    # Verify execute was called on session to persist record
    assert mock_session.execute.called or mock_session.add.called


@pytest.mark.asyncio
async def test_escalate_dynamic_evidence_preserves_pending_tasks() -> None:
    """Verifies that escalate_dynamic_evidence_node does not overwrite existing pending tasks."""
    from functions.orchestrator.graphs.trip_planner.graph import escalate_dynamic_evidence_node

    existing_task = {
        "worker": "EVIDENCE",
        "action": "collect",
        "payload": {"queries": ["test"]},
        "idempotency_key": "exec-1:collect:123",
    }
    state: TripPlanningState = {
        "execution_id": str(uuid.uuid4()),
        "pending_tasks": [existing_task],
        "task_outputs": {
            "search-1": {
                "browser_requests": [
                    {"url": "https://hotel.example.com/rates", "canonical_url": "https://hotel.example.com/rates"}
                ]
            }
        },
        "dispatched_browser_urls": [],
    }

    res = escalate_dynamic_evidence_node(state)
    assert len(res["pending_tasks"]) == 2
    assert res["pending_tasks"][0] == existing_task
    assert res["pending_tasks"][1]["worker"] == "BROWSER"
    assert res["pending_tasks"][1]["action"] == "render"


