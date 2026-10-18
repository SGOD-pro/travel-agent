"""Connected smoke acceptance test for remote LAYA decision router conforming to spec 0004 AC-10.

Verifies live Cloudflare tunnel communication against remote Kaggle endpoint:
- Skips cleanly when unconfigured or disabled (RUN_CONNECTED_LAYA_TESTS != '1' or LAYA_ENDPOINT_URL not set).
- Fails with actionable diagnostic on network, authentication, or protocol mismatch when enabled.
- Confirms GET /health responds with 200 OK.
- Confirms POST /v1/systemone accepts wire payload, passes schema validation, and returns normalized DecisionEnvelope.
- Verifies latency recording and policy compliance.
"""

from __future__ import annotations

import os
import uuid

import pytest

from config.config import Settings
from contracts.decision import (
    DecisionAction,
    DecisionPhase,
    DecisionStatus,
    DecisionType,
    LayaDecisionRequest,
)
from functions.decision_router.client import RemoteLayaDecisionAdapter
from functions.orchestrator.decision.policy import LayaPolicyValidator
from functions.orchestrator.decision.repository import compute_canonical_replay_digest


def _is_connected_test_enabled() -> tuple[bool, str]:
    """Check if remote LAYA credentials and run flag are configured."""
    flag = os.getenv("RUN_CONNECTED_LAYA_TESTS", "0").strip().lower()
    if flag not in ("1", "true", "yes"):
        return False, "RUN_CONNECTED_LAYA_TESTS is not enabled (set RUN_CONNECTED_LAYA_TESTS=1)"

    settings = Settings()
    if not settings.LAYA_ENDPOINT_URL or not settings.LAYA_ENDPOINT_URL.strip():
        return False, "LAYA_ENDPOINT_URL is unconfigured"

    return True, ""


@pytest.mark.asyncio
async def test_laya_connected_endpoint_acceptance() -> None:
    """Connects to live Cloudflare tunnel endpoint, validating health probe and decision routing."""
    enabled, reason = _is_connected_test_enabled()
    if not enabled:
        pytest.skip(f"Connected LAYA acceptance test skipped: {reason}")

    settings = Settings()
    adapter = RemoteLayaDecisionAdapter(settings=settings)

    try:
        # 1. Health check probe
        is_healthy = await adapter.check_health()
        assert is_healthy is True, (
            f"Health check probe failed against {settings.LAYA_ENDPOINT_URL}. "
            "Ensure Kaggle notebook and Cloudflare tunnel are active and healthy."
        )

        # 2. Prepare multi-choice evidence routing request
        exec_id = uuid.uuid4()
        phase = DecisionPhase.BROWSER_ELIGIBILITY
        eligible_choices = [
            DecisionAction.DISPATCH_BROWSER_ESCALATION,
            DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
        ]

        sanitized_context = {
            "execution_id": str(exec_id),
            "phase": phase.value,
            "category": "lodging",
            "destination": "Digha",
            "static_search_completed": True,
            "candidate_places_count": 2,
            "unhandled_browser_requests": 1,
            "browser_escalation_slots_remaining": 2,
            "sanitized_summary": "Discovered 2 places. Unhandled dynamic requests: 1.",
        }

        request_digest, idempotency_key = compute_canonical_replay_digest(
            execution_id=exec_id,
            phase=phase,
            context_version=1,
            policy_version=LayaPolicyValidator.POLICY_VERSION,
            question_version="evidence_routing:v1",
            eligible_actions=eligible_choices,
            sanitized_context=sanitized_context,
        )

        decision_req = LayaDecisionRequest(
            decision_id=uuid.uuid4(),
            decision_type=DecisionType.EVIDENCE_ROUTING,
            phase=phase,
            execution_id=exec_id,
            trip_id=exec_id,
            context_version=1,
            policy_version=LayaPolicyValidator.POLICY_VERSION,
            question_version="evidence_routing:v1",
            policy_derived_eligible_choices=eligible_choices,
            budget_remaining=2,
            context=sanitized_context,
            idempotency_key=idempotency_key,
        )

        # 3. Dispatch live inference query
        envelope = await adapter.query_decision(decision_req)

        # 4. Verify envelope schema, status, and telemetry
        assert envelope.decision_id == decision_req.decision_id
        assert envelope.phase == phase
        assert envelope.execution_id == exec_id
        assert envelope.idempotency_key == idempotency_key
        assert envelope.request_digest == request_digest
        assert envelope.latency_ms > 0, f"Expected non-zero latency from live remote call; got {envelope.latency_ms}ms"

        # Action must belong to offered eligible choices
        assert envelope.allowed_choice in eligible_choices, (
            f"Returned action '{envelope.allowed_choice}' must be one of {eligible_choices}"
        )

        # Status must be VALIDATED, REJECTED_BY_POLICY, or FALLBACK_LOW_CONFIDENCE (if below threshold)
        assert envelope.status in (
            DecisionStatus.VALIDATED,
            DecisionStatus.REJECTED_BY_POLICY,
            DecisionStatus.FALLBACK_LOW_CONFIDENCE,
        ), f"Unexpected status {envelope.status}: rationale={envelope.rationale}"

        # If VALIDATED, answer_confidence must satisfy the configured threshold
        if envelope.status == DecisionStatus.VALIDATED:
            assert envelope.answer_confidence is not None
            assert envelope.answer_confidence >= settings.LAYA_CONFIDENCE_THRESHOLD
        elif envelope.status == DecisionStatus.FALLBACK_LOW_CONFIDENCE:
            assert envelope.answer_confidence is not None
            assert envelope.answer_confidence < settings.LAYA_CONFIDENCE_THRESHOLD

        # Confidence scores must be valid finite floats when present
        if envelope.confidence is not None:
            assert 0.0 <= envelope.confidence <= 1.0

        if envelope.answer_confidence is not None:
            assert 0.0 <= envelope.answer_confidence <= 1.0

    finally:
        await adapter.aclose()
