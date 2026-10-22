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
from functions.orchestrator.decision.policy import InsufficientRequiredEvidenceError
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    _is_valid_source_evidence_for_destination,
    route_evidence_decision_edge,
    route_evidence_decision_node,
)


class MockLayaDecisionAdapter(LayaDecisionPort):
    """Controllable test adapter for graph node testing."""

    is_fixture: bool = True

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
        "_offline_fixture_mode": True,
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
                        "excerpt_text": "Digha sea beach hotels and travel guide",
                        "extraction_method": "SCRAPLING_HTTP",
                        "artifact_ref": {"gps": {"lat": 21.6266, "lng": 87.5074}},
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
                        "excerpt_text": "Digha sea beach hotels and travel guide",
                        "extraction_method": "SCRAPLING_HTTP",
                        "artifact_ref": {"gps": {"lat": 21.6266, "lng": 87.5074}},
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
                        "excerpt_text": "Digha sea beach hotels and travel guide",
                        "extraction_method": "SCRAPLING_HTTP",
                        "artifact_ref": {"gps": {"lat": 21.6266, "lng": 87.5074}},
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
    failing_adapter.is_fixture = True
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
                        "excerpt_text": "Digha sea beach hotels and travel guide",
                        "extraction_method": "SCRAPLING_HTTP",
                        "artifact_ref": {"gps": {"lat": 21.6266, "lng": 87.5074}},
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
    mock_obs = MagicMock()
    mock_obs.canonical_url = "https://example.com/digha"
    mock_obs.excerpt_text = "Digha sea beach hotels and travel guide"
    mock_obs.extraction_method = "SCRAPLING_HTTP"
    mock_obs.content_hash = "hash123"
    mock_obs.artifact_ref = {"gps": {"lat": 21.6266, "lng": 87.5074}}
    mock_result.all.return_value = [mock_obs]
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


@pytest.mark.asyncio
async def test_evidence_sufficiency_rejects_js_placeholders() -> None:
    """Verifies that NEEDS_BROWSER placeholder observations do NOT satisfy required evidence."""
    exec_id = str(uuid.uuid4())
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
                        "canonical_url": "https://hotel.example.com/rates",
                        "extraction_method": "NEEDS_BROWSER",
                        "excerpt_text": "Requires JavaScript",
                    }
                ],
                "browser_requests": [
                    {"url": "https://hotel.example.com/rates", "canonical_url": "https://hotel.example.com/rates"}
                ],
            }
        },
        "static_search_completed": True,
        "remaining_search_budget": 0,
        "_laya_adapter": MockLayaDecisionAdapter(),
    }

    result = await route_evidence_decision_node(state)
    # Because evidence is not satisfied and unhandled browser requests exist,
    # evaluation bypass is rejected and browser escalation is routed
    assert result["evidence_routing_action"] == DecisionAction.DISPATCH_BROWSER_ESCALATION.value


@pytest.mark.asyncio
async def test_evidence_sufficiency_rejects_irrelevant_destination_observations() -> None:
    """Verifies that observations for an unrelated destination do NOT satisfy required evidence."""
    exec_id = str(uuid.uuid4())
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
                        "canonical_url": "https://example.com/paris-guide",
                        "extraction_method": "STATIC_HTML",
                        "excerpt_text": "Top Eiffel Tower and Louvre attractions in Paris",
                        "content_hash": "paris_hash",
                    }
                ],
                "browser_requests": [
                    {"url": "https://hotel.example.com/rates", "canonical_url": "https://hotel.example.com/rates"}
                ],
            }
        },
        "static_search_completed": True,
        "remaining_search_budget": 0,
        "_laya_adapter": MockLayaDecisionAdapter(),
    }

    result = await route_evidence_decision_node(state)
    # Irrelevant destination observation means required evidence is NOT satisfied for Digha
    assert result["evidence_routing_action"] == DecisionAction.DISPATCH_BROWSER_ESCALATION.value


@pytest.mark.asyncio
async def test_evidence_sufficiency_accepts_valid_destination_evidence() -> None:
    """Verifies that substantive, destination-relevant observations satisfy required evidence."""
    exec_id = str(uuid.uuid4())
    state: TripPlanningState = {
        "execution_id": exec_id,
        "trip_id": exec_id,
        "version": 2,
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
                        "canonical_url": "https://example.com/digha-sea-beach-hotels",
                        "extraction_method": "STATIC_HTML",
                        "excerpt_text": "Digha sea beach luxury hotels and resorts directory",
                        "content_hash": "digha_hash",
                        "artifact_ref": {"gps": {"lat": 21.6266, "lng": 87.5074}},
                    }
                ],
            }
        },
        "static_search_completed": True,
        "remaining_search_budget": 0,
        "_laya_adapter": MockLayaDecisionAdapter(),
    }

    result = await route_evidence_decision_node(state)
    # Valid Digha observation satisfies required evidence -> singleton evaluation bypass
    assert result["evidence_routing_action"] == DecisionAction.PROCEED_DETERMINISTIC_EVALUATION.value
    assert result["current_step"] == "deterministic_evaluation_routed"
    assert len(result["laya_decisions"]) == 1

    dec = result["laya_decisions"][0]
    assert dec["context_version"] == 2
    assert dec["allowed_choices"] == [DecisionAction.PROCEED_DETERMINISTIC_EVALUATION.value]


@pytest.mark.asyncio
async def test_database_lookup_failure_with_in_memory_observations_fails_closed() -> None:
    """Verifies that database lookup failure fails closed and does not permit evaluation, even if plausible in-memory observations exist."""
    exec_id = str(uuid.uuid4())
    mock_failing_session = AsyncMock()
    mock_failing_session.execute.side_effect = RuntimeError("Database connection timed out")

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
                        "excerpt_text": "Digha sea beach hotels and travel guide",
                        "extraction_method": "SCRAPLING_HTTP",
                    }
                ],
                "search_completed": True,
            }
        },
        "static_search_completed": True,
        "_db_session": mock_failing_session,
    }

    # Must raise persistence failure and fail closed without permitting evaluation
    with pytest.raises((RuntimeError, InsufficientRequiredEvidenceError)):
        await route_evidence_decision_node(state)


@pytest.mark.asyncio
async def test_stale_required_evidence_satisfied_flag_overridden_by_authoritative_validation() -> None:
    """Verifies that a stale required_evidence_satisfied=True flag is overridden when authoritative evidence is missing."""
    exec_id = str(uuid.uuid4())
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
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
        "task_outputs": {},
        "static_search_completed": True,
        "required_evidence_satisfied": True,  # Stale flag in state
        "remaining_search_budget": 0,
        "_db_session": mock_session,
    }

    # Must reject stale flag, determine evidence is NOT satisfied, and raise InsufficientRequiredEvidenceError
    with pytest.raises(InsufficientRequiredEvidenceError):
        await route_evidence_decision_node(state)


def test_destination_name_only_text_urls_and_needs_browser_placeholders_rejected() -> None:
    """Verifies that destination-name-only URLs, destination-name-only text, NEEDS_BROWSER placeholders, and conflicting coordinates are rejected."""
    dest_name = "Digha"
    dest_lat = 21.6266
    dest_lng = 87.5074

    # 1. Destination-name-only URL (destination name in URL but missing from substantive text)
    dest_url_only_obs = {
        "canonical_url": "https://example.com/destinations/digha.html",
        "content_hash": "hash_url_only",
        "excerpt_text": "Click here to see more details and contact info",
        "extraction_method": "SCRAPLING_HTTP",
        "artifact_ref": {},
    }
    assert _is_valid_source_evidence_for_destination(dest_url_only_obs, dest_name, dest_lat, dest_lng) is False

    # 2. Destination-name-only text (excerpt text is just the destination name)
    dest_name_only_text_obs = {
        "canonical_url": "https://example.com/hotels",
        "content_hash": "hash_name_only",
        "excerpt_text": "Digha",
        "extraction_method": "SCRAPLING_HTTP",
        "artifact_ref": {},
    }
    assert _is_valid_source_evidence_for_destination(dest_name_only_text_obs, dest_name, dest_lat, dest_lng) is False

    # 3. NEEDS_BROWSER dynamic rendering placeholder
    needs_browser_obs = {
        "canonical_url": "https://hotel.example.com/digha-rates",
        "content_hash": "hash_placeholder",
        "excerpt_text": "[DYNAMIC_PAGE: JAVASCRIPT_REQUIRED] Dynamic rendering required for https://hotel.example.com/digha-rates (DYNAMIC_JAVASCRIPT_REQUIRED)",
        "extraction_method": "NEEDS_BROWSER",
        "artifact_ref": {"browser_reason": "DYNAMIC_JAVASCRIPT_REQUIRED"},
    }
    assert _is_valid_source_evidence_for_destination(needs_browser_obs, dest_name, dest_lat, dest_lng) is False

    # 4. Conflicting coordinates (Mumbai coordinates for Digha destination, distance ~1600 km)
    mumbai_coords_obs = {
        "canonical_url": "https://example.com/hotels/digha-palace",
        "content_hash": "hash_mumbai",
        "excerpt_text": "Digha Palace Hotel luxury booking and suites",
        "extraction_method": "SCRAPLING_HTTP",
        "artifact_ref": {"gps": {"lat": 19.0760, "lng": 72.8777}},
    }
    assert _is_valid_source_evidence_for_destination(mumbai_coords_obs, dest_name, dest_lat, dest_lng) is False

    # 5. Valid destination evidence with substantive excerpt and matching coordinates
    valid_obs = {
        "canonical_url": "https://example.com/hotels/digha-beach",
        "content_hash": "hash_valid",
        "excerpt_text": "Digha sea beach resort with deluxe sea view rooms and breakfast",
        "extraction_method": "SCRAPLING_HTTP",
        "artifact_ref": {"gps": {"lat": 21.6266, "lng": 87.5074}},
    }
    assert _is_valid_source_evidence_for_destination(valid_obs, dest_name, dest_lat, dest_lng) is True

    # 6. Missing source coordinates (valid substantive text, but artifact_ref has no coordinates)
    missing_coords_obs = {
        "canonical_url": "https://example.com/destinations/digha.html",
        "content_hash": "hash_missing_coords",
        "excerpt_text": "Digha is a beautiful coastal resort destination with clean beaches",
        "extraction_method": "SCRAPLING_HTTP",
        "artifact_ref": {},
    }
    assert _is_valid_source_evidence_for_destination(missing_coords_obs, dest_name, dest_lat, dest_lng) is False

    # 7. Malformed source coordinates (string conversion error)
    malformed_coords_obs = {
        "canonical_url": "https://example.com/destinations/digha.html",
        "content_hash": "hash_malformed_coords",
        "excerpt_text": "Digha is a beautiful coastal resort destination with clean beaches",
        "extraction_method": "SCRAPLING_HTTP",
        "artifact_ref": {"gps": {"lat": "malformed_lat", "lng": "invalid_lng"}},
    }
    assert _is_valid_source_evidence_for_destination(malformed_coords_obs, dest_name, dest_lat, dest_lng) is False

    # 8. Out-of-bounds coordinates (lat 120.0 > 90.0)
    out_of_bounds_coords_obs = {
        "canonical_url": "https://example.com/destinations/digha.html",
        "content_hash": "hash_oob_coords",
        "excerpt_text": "Digha is a beautiful coastal resort destination with clean beaches",
        "extraction_method": "SCRAPLING_HTTP",
        "artifact_ref": {"gps": {"lat": 120.0, "lng": 87.5074}},
    }
    assert _is_valid_source_evidence_for_destination(out_of_bounds_coords_obs, dest_name, dest_lat, dest_lng) is False

    # 9. Unavailable destination information in excerpt text
    unavailable_text_obs = {
        "canonical_url": "https://example.com/destinations/digha.html",
        "content_hash": "hash_unavailable_text",
        "excerpt_text": "Digha destination information is unavailable",
        "extraction_method": "SCRAPLING_HTTP",
        "artifact_ref": {"gps": {"lat": 21.6266, "lng": 87.5074}},
    }
    assert _is_valid_source_evidence_for_destination(unavailable_text_obs, dest_name, dest_lat, dest_lng) is False

    # 10. Explicit unavailable status in artifact_ref
    unavailable_status_obs = {
        "canonical_url": "https://example.com/destinations/digha.html",
        "content_hash": "hash_status_unavail",
        "excerpt_text": "Digha luxury coastal beach resort booking and guide",
        "extraction_method": "SCRAPLING_HTTP",
        "artifact_ref": {"status": "UNAVAILABLE", "gps": {"lat": 21.6266, "lng": 87.5074}},
    }
    assert _is_valid_source_evidence_for_destination(unavailable_status_obs, dest_name, dest_lat, dest_lng) is False

    # 11. Unavailable reason in artifact_ref
    unavailable_reason_obs = {
        "canonical_url": "https://example.com/destinations/digha.html",
        "content_hash": "hash_reason_unavail",
        "excerpt_text": "Digha luxury coastal beach resort booking and guide",
        "extraction_method": "SCRAPLING_HTTP",
        "artifact_ref": {"unavailable_reason": "Blocked by target server", "gps": {"lat": 21.6266, "lng": 87.5074}},
    }
    assert _is_valid_source_evidence_for_destination(unavailable_reason_obs, dest_name, dest_lat, dest_lng) is False


@pytest.mark.asyncio
async def test_valid_persisted_evidence_permits_evaluation() -> None:
    """Verifies that valid persisted evidence permits PROCEED_DETERMINISTIC_EVALUATION."""
    exec_id = str(uuid.uuid4())
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_obs = MagicMock()
    mock_obs.canonical_url = "https://example.com/digha-beach-resort"
    mock_obs.excerpt_text = "Digha Sea Beach luxury resort and suites"
    mock_obs.extraction_method = "SCRAPLING_HTTP"
    mock_obs.content_hash = "valid_hash_456"
    mock_obs.artifact_ref = {"gps": {"lat": 21.6266, "lng": 87.5074}}
    mock_result.all.return_value = [mock_obs]
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
        "task_outputs": {},
        "static_search_completed": True,
        "remaining_search_budget": 0,
        "_db_session": mock_session,
    }

    result = await route_evidence_decision_node(state)
    assert result["evidence_routing_action"] == DecisionAction.PROCEED_DETERMINISTIC_EVALUATION.value
    assert result["current_step"] == "deterministic_evaluation_routed"
    assert len(result["laya_decisions"]) == 1
    assert result["laya_decisions"][0]["action"] == DecisionAction.PROCEED_DETERMINISTIC_EVALUATION.value




