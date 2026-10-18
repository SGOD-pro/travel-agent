"""Unit tests for LAYA decision router domain contracts, wire schemas, config, and fixtures.

Conforms to spec 0004 Slice 1:
1. Domain contracts serialization, validation, and immutability.
2. Distinct confidence metrics:
   answer_confidence reflects top choice probability.
   confidence reflects model distribution entropy confidence (nullable).
3. Deterministic singleton decisions have confidence=null and answer_confidence=null,
   identified by status DETERMINISTIC_SINGLETON.
4. Upstream wire request and response schemas matching NandhaKishorM/laya v0.4.9.
5. Strict mode missing field behavior and unknown truncation metadata handling.
6. Comprehensive probability validation (finite values, normalization, argmax, alignment).
7. Validated configuration with separate Bearer / Cloudflare Access credentials and deadline invariants.
8. Offline FixtureLayaDecisionAdapter execution with zero network dependencies.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from config.config import Settings
from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionPhase,
    DecisionStatus,
    DecisionType,
    LayaDecisionPort,
    LayaDecisionRequest,
)
from functions.decision_router.fixtures import (
    FixtureLayaDecisionAdapter,
    compute_request_digest,
)
from functions.decision_router.schemas import (
    LayaChoiceAnswer,
    LayaWireQuestion,
    LayaWireRequest,
    LayaWireResponse,
    LayaWireRouting,
    LayaWireUsage,
    resolve_answer_confidence,
    resolve_model_version,
    resolve_truncation_status,
    validate_wire_probabilities,
)

SAMPLE_EXECUTION_ID = UUID("3fa85f64-5717-4562-b3fc-2c963f66afa6")
SAMPLE_TRIP_ID = UUID("9bb644b4-0be9-4179-8d76-5fa4537e5843")
SAMPLE_IDEMPOTENCY_KEY = f"{SAMPLE_EXECUTION_ID}:laya_decision:BROWSER_ELIGIBILITY:1234567890abcdef"


def _make_sample_request(
    eligible_choices: list[DecisionAction] | None = None,
    phase: DecisionPhase = DecisionPhase.BROWSER_ELIGIBILITY,
) -> LayaDecisionRequest:
    """Helper to create valid sample LayaDecisionRequest."""
    choices = eligible_choices or [
        DecisionAction.DISPATCH_BROWSER_ESCALATION,
        DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
        DecisionAction.DISPATCH_EVIDENCE_SEARCH,
    ]
    return LayaDecisionRequest(
        decision_id=uuid4(),
        decision_type=DecisionType.EVIDENCE_ROUTING,
        phase=phase,
        execution_id=SAMPLE_EXECUTION_ID,
        trip_id=SAMPLE_TRIP_ID,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="evidence_routing:v1",
        policy_derived_eligible_choices=choices,
        budget_remaining=3,
        context={
            "execution_id": str(SAMPLE_EXECUTION_ID),
            "phase": phase.value,
            "category": "lodging",
            "destination": "Kolkata",
            "static_search_completed": True,
            "candidate_places_count": 4,
            "unhandled_browser_requests": 1,
            "browser_escalation_slots_remaining": 3,
        },
        idempotency_key=SAMPLE_IDEMPOTENCY_KEY,
    )


# ============================================================================
# 1. Domain Contracts: Serialization, Validation, and Semantics
# ============================================================================


def test_decision_action_and_status_enums() -> None:
    """Verifies all required enum members exist and match spec constants."""
    assert DecisionAction.DISPATCH_EVIDENCE_SEARCH == "DISPATCH_EVIDENCE_SEARCH"
    assert DecisionAction.DISPATCH_BROWSER_ESCALATION == "DISPATCH_BROWSER_ESCALATION"
    assert DecisionAction.PROCEED_DETERMINISTIC_EVALUATION == "PROCEED_DETERMINISTIC_EVALUATION"

    assert DecisionType.EVIDENCE_ROUTING == "EVIDENCE_ROUTING"

    assert DecisionPhase.SEARCH_COMPLETION == "SEARCH_COMPLETION"
    assert DecisionPhase.BROWSER_ELIGIBILITY == "BROWSER_ELIGIBILITY"
    assert DecisionPhase.EVALUATION == "EVALUATION"

    assert DecisionStatus.VALIDATED == "VALIDATED"
    assert DecisionStatus.DETERMINISTIC_SINGLETON == "DETERMINISTIC_SINGLETON"
    assert DecisionStatus.REJECTED_BY_POLICY == "REJECTED_BY_POLICY"
    assert DecisionStatus.FALLBACK_TIMEOUT == "FALLBACK_TIMEOUT"
    assert DecisionStatus.FALLBACK_ERROR == "FALLBACK_ERROR"
    assert DecisionStatus.FALLBACK_LOW_CONFIDENCE == "FALLBACK_LOW_CONFIDENCE"
    assert DecisionStatus.FALLBACK_ABSTAINED == "FALLBACK_ABSTAINED"
    assert DecisionStatus.FALLBACK_TRUNCATED == "FALLBACK_TRUNCATED"
    assert DecisionStatus.FALLBACK_MALFORMED == "FALLBACK_MALFORMED"
    assert DecisionStatus.FALLBACK_UNCONFIGURED == "FALLBACK_UNCONFIGURED"


def test_laya_decision_request_serialization_roundtrip() -> None:
    """Verifies LayaDecisionRequest model dumping and validation roundtrip."""
    request = _make_sample_request()
    dumped = request.model_dump(mode="json")

    assert dumped["execution_id"] == str(SAMPLE_EXECUTION_ID)
    assert dumped["decision_type"] == "EVIDENCE_ROUTING"
    assert dumped["phase"] == "BROWSER_ELIGIBILITY"
    assert len(dumped["policy_derived_eligible_choices"]) == 3

    restored = LayaDecisionRequest.model_validate(dumped)
    assert restored == request


def test_decision_envelope_serialization_roundtrip() -> None:
    """Verifies DecisionEnvelope model dumping and validation roundtrip."""
    digest = compute_request_digest(
        execution_id=str(SAMPLE_EXECUTION_ID),
        phase=DecisionPhase.BROWSER_ELIGIBILITY.value,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="evidence_routing:v1",
        eligible_choices=[c.value for c in DecisionAction],
    )
    envelope = DecisionEnvelope(
        decision_id=uuid4(),
        decision_type=DecisionType.EVIDENCE_ROUTING,
        phase=DecisionPhase.BROWSER_ELIGIBILITY,
        execution_id=SAMPLE_EXECUTION_ID,
        trip_id=SAMPLE_TRIP_ID,
        idempotency_key=SAMPLE_IDEMPOTENCY_KEY,
        allowed_choice=DecisionAction.DISPATCH_BROWSER_ESCALATION,
        status=DecisionStatus.VALIDATED,
        confidence=None,
        answer_confidence=0.820,
        model_version="typed-decisions",
        policy_version="laya-policy-v1",
        rationale="policy validated proposal",
        latency_ms=45,
        request_digest=digest,
        created_at="2026-10-17T16:00:00Z",
    )
    dumped = envelope.model_dump(mode="json")
    restored = DecisionEnvelope.model_validate(dumped)
    assert restored == envelope


def test_deterministic_singleton_enforces_null_confidences() -> None:
    """Verifies that DETERMINISTIC_SINGLETON enforces confidence=None and answer_confidence=None."""
    digest = "abcdef1234567890"

    # Valid deterministic singleton decision
    valid_singleton = DecisionEnvelope(
        decision_id=uuid4(),
        decision_type=DecisionType.EVIDENCE_ROUTING,
        phase=DecisionPhase.SEARCH_COMPLETION,
        execution_id=SAMPLE_EXECUTION_ID,
        trip_id=None,
        idempotency_key="singleton_key",
        allowed_choice=DecisionAction.DISPATCH_EVIDENCE_SEARCH,
        status=DecisionStatus.DETERMINISTIC_SINGLETON,
        confidence=None,
        answer_confidence=None,
        model_version="deterministic_singleton",
        policy_version="laya-policy-v1",
        rationale="single eligible choice: deterministic bypass",
        latency_ms=0,
        request_digest=digest,
        created_at="2026-10-17T16:00:00Z",
    )
    assert valid_singleton.confidence is None
    assert valid_singleton.answer_confidence is None
    assert valid_singleton.status == DecisionStatus.DETERMINISTIC_SINGLETON

    # Rejected when answer_confidence is not None
    with pytest.raises(ValidationError, match="Deterministic singleton decisions must have confidence=null and answer_confidence=null"):
        DecisionEnvelope(
            decision_id=uuid4(),
            decision_type=DecisionType.EVIDENCE_ROUTING,
            phase=DecisionPhase.SEARCH_COMPLETION,
            execution_id=SAMPLE_EXECUTION_ID,
            trip_id=None,
            idempotency_key="singleton_key",
            allowed_choice=DecisionAction.DISPATCH_EVIDENCE_SEARCH,
            status=DecisionStatus.DETERMINISTIC_SINGLETON,
            confidence=None,
            answer_confidence=1.0,
            model_version="deterministic_singleton",
            policy_version="laya-policy-v1",
            latency_ms=0,
            request_digest=digest,
            created_at="2026-10-17T16:00:00Z",
        )

    # Rejected when confidence is not None
    with pytest.raises(ValidationError, match="Deterministic singleton decisions must have confidence=null and answer_confidence=null"):
        DecisionEnvelope(
            decision_id=uuid4(),
            decision_type=DecisionType.EVIDENCE_ROUTING,
            phase=DecisionPhase.SEARCH_COMPLETION,
            execution_id=SAMPLE_EXECUTION_ID,
            trip_id=None,
            idempotency_key="singleton_key",
            allowed_choice=DecisionAction.DISPATCH_EVIDENCE_SEARCH,
            status=DecisionStatus.DETERMINISTIC_SINGLETON,
            confidence=0.85,
            answer_confidence=None,
            model_version="deterministic_singleton",
            policy_version="laya-policy-v1",
            latency_ms=0,
            request_digest=digest,
            created_at="2026-10-17T16:00:00Z",
        )


def test_confidence_finite_value_bounds() -> None:
    """Verifies that non-finite or out-of-range confidence scores raise ValidationError."""
    digest = "abcdef1234567890"

    with pytest.raises(ValidationError, match="answer_confidence must be a finite float between 0.0 and 1.0"):
        DecisionEnvelope(
            decision_id=uuid4(),
            decision_type=DecisionType.EVIDENCE_ROUTING,
            phase=DecisionPhase.BROWSER_ELIGIBILITY,
            execution_id=SAMPLE_EXECUTION_ID,
            trip_id=None,
            idempotency_key="test_key",
            allowed_choice=DecisionAction.DISPATCH_BROWSER_ESCALATION,
            status=DecisionStatus.VALIDATED,
            answer_confidence=1.5,
            policy_version="laya-policy-v1",
            latency_ms=10,
            request_digest=digest,
            created_at="2026-10-17T16:00:00Z",
        )

    with pytest.raises(ValidationError, match="confidence must be a finite float between 0.0 and 1.0"):
        DecisionEnvelope(
            decision_id=uuid4(),
            decision_type=DecisionType.EVIDENCE_ROUTING,
            phase=DecisionPhase.BROWSER_ELIGIBILITY,
            execution_id=SAMPLE_EXECUTION_ID,
            trip_id=None,
            idempotency_key="test_key",
            allowed_choice=DecisionAction.DISPATCH_BROWSER_ESCALATION,
            status=DecisionStatus.VALIDATED,
            confidence=-0.1,
            answer_confidence=0.820,
            policy_version="laya-policy-v1",
            latency_ms=10,
            request_digest=digest,
            created_at="2026-10-17T16:00:00Z",
        )


# ============================================================================
# 2. Upstream Wire Schemas: Extended and Strict Responses
# ============================================================================


def test_wire_request_serialization() -> None:
    """Verifies LayaWireRequest packaging and JSON serialization."""
    req = LayaWireRequest(
        state={
            "execution_id": str(SAMPLE_EXECUTION_ID),
            "phase": "BROWSER_ELIGIBILITY",
            "category": "lodging",
        },
        questions={
            "evidence_routing": LayaWireQuestion(
                type="choice",
                instructions="Select routing action",
                criteria={"DISPATCH_BROWSER_ESCALATION": "Escalate"},
            )
        },
        min_confidence=0.700,
        model="typed-decisions",
    )
    dumped = req.model_dump(mode="json")
    assert dumped["min_confidence"] == 0.700
    assert dumped["model"] == "typed-decisions"
    assert "evidence_routing" in dumped["questions"]


def test_extended_wire_response_fixture_compliance() -> None:
    """Verifies illustrative fixture with x_jev_confidence=0.730, answer_confidence=0.820, confidence=null."""
    response = FixtureLayaDecisionAdapter.make_sample_extended_wire_response()

    assert response.model == "laya-rl-agent"
    assert "evidence_routing" in response.answers
    ans = response.answers["evidence_routing"]

    # Invariant: calibrated Jev confidence is exactly 0.730
    assert ans.x_jev_confidence == 0.730

    # Invariant: answer_confidence is 0.820, matching top choice probability
    assert ans.answer_confidence == 0.820
    assert ans.probabilities["DISPATCH_BROWSER_ESCALATION"] == 0.820

    # Invariant: distribution entropy confidence is null (not conflated with answer_confidence)
    assert ans.confidence is None

    # Invariant: abstention passed
    assert ans.abstention == "passed"
    assert ans.low_confidence is False

    # Invariant: truncation false
    assert response.usage is not None
    assert response.usage.truncated is False
    assert resolve_truncation_status(response) == "not_truncated"

    # Invariant: model version resolution
    assert resolve_model_version(response) == "typed-decisions"

    # Probability validation succeeds
    validate_wire_probabilities(ans, allowed_choices=list(ans.probabilities.keys()))


def test_strict_wire_response_missing_fields_defaults() -> None:
    """Verifies strict mode response where optional fields are omitted."""
    strict_response = FixtureLayaDecisionAdapter.make_sample_strict_wire_response(
        choice="DISPATCH_BROWSER_ESCALATION",
        model="typed-decisions",
    )

    ans = strict_response.answers["evidence_routing"]
    assert ans.answer_confidence is None
    assert ans.confidence is None
    assert ans.x_jev_confidence is None
    assert ans.abstention is None
    assert strict_response.usage is None
    assert strict_response.routing is None

    # Missing answer_confidence is derived deterministically from probabilities
    derived_confidence = resolve_answer_confidence(ans)
    assert derived_confidence == 0.820

    # Missing routing resolves to root model
    assert resolve_model_version(strict_response) == "typed-decisions"

    # Missing usage resolves truncation status to unknown without assuming proof
    assert resolve_truncation_status(strict_response) == "unknown"


def test_resolve_model_version_fallback_hierarchy() -> None:
    """Verifies model resolution priority: routing.model -> model -> remote-laya-unknown."""
    # 1. routing.model takes precedence
    resp_with_routing = LayaWireResponse(
        model="root-model",
        routing=LayaWireRouting(model="routing-model"),
    )
    assert resolve_model_version(resp_with_routing) == "routing-model"

    # 2. root model used when routing is absent
    resp_with_root = LayaWireResponse(model="root-model")
    assert resolve_model_version(resp_with_root) == "root-model"

    # 3. fallback default when both are absent
    resp_empty = LayaWireResponse()
    assert resolve_model_version(resp_empty) == "remote-laya-unknown"


def test_resolve_truncation_status_cases() -> None:
    """Verifies truncation status mapping across true, false, and omitted usage."""
    resp_truncated = LayaWireResponse(usage=LayaWireUsage(truncated=True))
    assert resolve_truncation_status(resp_truncated) == "truncated"

    resp_not_truncated = LayaWireResponse(usage=LayaWireUsage(truncated=False))
    assert resolve_truncation_status(resp_not_truncated) == "not_truncated"

    resp_usage_none = LayaWireResponse(usage=None)
    assert resolve_truncation_status(resp_usage_none) == "unknown"

    resp_usage_truncated_none = LayaWireResponse(usage=LayaWireUsage(truncated=None))
    assert resolve_truncation_status(resp_usage_truncated_none) == "unknown"


# ============================================================================
# 3. Probability Validation and Mathematical Invariants
# ============================================================================


def test_validate_wire_probabilities_valid() -> None:
    """Verifies that valid normalized distributions pass probability validation."""
    ans = LayaChoiceAnswer(
        choice="ACTION_A",
        probabilities={"ACTION_A": 0.820, "ACTION_B": 0.110, "ACTION_C": 0.070},
        answer_confidence=0.820,
    )
    validate_wire_probabilities(ans, allowed_choices=["ACTION_A", "ACTION_B", "ACTION_C"])


def test_validate_wire_probabilities_missing_choice() -> None:
    """Verifies that choice absent from probabilities raises ValueError."""
    ans = LayaChoiceAnswer(
        choice="ACTION_UNKNOWN",
        probabilities={"ACTION_A": 0.7, "ACTION_B": 0.3},
    )
    with pytest.raises(ValueError, match="not present in probabilities"):
        validate_wire_probabilities(ans)


def test_validate_wire_probabilities_unoffered_keys() -> None:
    """Verifies that keys not in allowed_choices raise ValueError."""
    ans = LayaChoiceAnswer(
        choice="ACTION_A",
        probabilities={"ACTION_A": 0.7, "ACTION_FORBIDDEN": 0.3},
    )
    with pytest.raises(ValueError, match="Probabilities contain unoffered keys"):
        validate_wire_probabilities(ans, allowed_choices=["ACTION_A", "ACTION_B"])


def test_validate_wire_probabilities_non_finite_or_out_of_range() -> None:
    """Verifies that NaN, negative, or > 1.0 probability values raise ValueError."""
    ans_nan = LayaChoiceAnswer(
        choice="ACTION_A",
        probabilities={"ACTION_A": float("nan"), "ACTION_B": 0.3},
    )
    with pytest.raises(ValueError, match="must be a finite float between 0.0 and 1.0"):
        validate_wire_probabilities(ans_nan)

    ans_negative = LayaChoiceAnswer(
        choice="ACTION_A",
        probabilities={"ACTION_A": -0.2, "ACTION_B": 1.2},
    )
    with pytest.raises(ValueError, match="must be a finite float between 0.0 and 1.0"):
        validate_wire_probabilities(ans_negative)


def test_validate_wire_probabilities_normalization_failure() -> None:
    """Verifies that probability distribution not summing to 1.0 raises ValueError."""
    ans_unnormalized = LayaChoiceAnswer(
        choice="ACTION_A",
        probabilities={"ACTION_A": 0.500, "ACTION_B": 0.300},
    )
    with pytest.raises(ValueError, match="must normalize to 1.0 within 0.01 tolerance"):
        validate_wire_probabilities(ans_unnormalized)


def test_validate_wire_probabilities_argmax_inconsistency() -> None:
    """Verifies that choice diverging from argmax key raises ValueError."""
    ans_mismatch = LayaChoiceAnswer(
        choice="ACTION_B",
        probabilities={"ACTION_A": 0.700, "ACTION_B": 0.300},
    )
    with pytest.raises(ValueError, match="does not match argmax key"):
        validate_wire_probabilities(ans_mismatch)


def test_validate_wire_probabilities_alignment_mismatch() -> None:
    """Verifies that answer_confidence diverging from probabilities[choice] raises ValueError."""
    ans_align_mismatch = LayaChoiceAnswer(
        choice="ACTION_A",
        probabilities={"ACTION_A": 0.700, "ACTION_B": 0.300},
        answer_confidence=0.950,
    )
    with pytest.raises(ValueError, match="does not align with probabilities"):
        validate_wire_probabilities(ans_align_mismatch)


# ============================================================================
# 4. Configuration and Settings Validation
# ============================================================================


def test_laya_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies default LAYA configuration values and field presence."""
    monkeypatch.delenv("LAYA_ENDPOINT", raising=False)
    monkeypatch.delenv("LAYA_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    monkeypatch.delenv("CF_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)
    settings = Settings(_env_file=None)

    assert settings.LAYA_ENDPOINT_URL is None
    assert settings.LAYA_API_KEY is None
    assert settings.CF_ACCESS_CLIENT_ID is None
    assert settings.CF_ACCESS_CLIENT_SECRET is None
    assert settings.LAYA_REQUEST_TIMEOUT_SECONDS == 2.5
    assert settings.LAYA_TOTAL_DEADLINE_SECONDS == 5.0
    assert settings.LAYA_MAX_RETRIES == 2
    assert settings.LAYA_CONFIDENCE_THRESHOLD == 0.700
    assert settings.LAYA_MODEL_NAME == "typed-decisions"


def test_laya_settings_credentials_separation() -> None:
    """Verifies separate fields for Bearer authentication and Cloudflare Access tokens."""
    settings = Settings(
        LAYA_ENDPOINT_URL="https://laya.example.cloudflarestream.com",
        LAYA_API_KEY="secret-bearer-token",
        CF_ACCESS_CLIENT_ID="cf-service-client-id",
        CF_ACCESS_CLIENT_SECRET="cf-service-client-secret",
    )
    assert settings.LAYA_API_KEY == "secret-bearer-token"
    assert settings.CF_ACCESS_CLIENT_ID == "cf-service-client-id"
    assert settings.CF_ACCESS_CLIENT_SECRET == "cf-service-client-secret"


def test_laya_settings_deadline_validation() -> None:
    """Verifies that LAYA_TOTAL_DEADLINE_SECONDS must be >= LAYA_REQUEST_TIMEOUT_SECONDS."""
    # Valid when deadline >= timeout
    valid_settings = Settings(
        LAYA_REQUEST_TIMEOUT_SECONDS=2.5,
        LAYA_TOTAL_DEADLINE_SECONDS=5.0,
    )
    assert valid_settings.LAYA_TOTAL_DEADLINE_SECONDS == 5.0

    # Rejected when deadline < timeout
    with pytest.raises(ValidationError, match="LAYA_TOTAL_DEADLINE_SECONDS must be greater than or equal to LAYA_REQUEST_TIMEOUT_SECONDS"):
        Settings(
            LAYA_REQUEST_TIMEOUT_SECONDS=4.0,
            LAYA_TOTAL_DEADLINE_SECONDS=2.0,
        )


# ============================================================================
# 5. Offline Fixture Adapter Execution
# ============================================================================


@pytest.mark.asyncio
async def test_fixture_adapter_conforms_to_port() -> None:
    """Verifies FixtureLayaDecisionAdapter conforms to LayaDecisionPort interface."""
    adapter = FixtureLayaDecisionAdapter()
    assert isinstance(adapter, LayaDecisionPort)
    assert adapter.is_test_fixture is True

    health = await adapter.check_health()
    assert health is True


@pytest.mark.asyncio
async def test_fixture_adapter_deterministic_singleton_bypass() -> None:
    """Verifies that single eligible choice triggers deterministic singleton bypass."""
    adapter = FixtureLayaDecisionAdapter()
    single_choice_request = _make_sample_request(
        eligible_choices=[DecisionAction.DISPATCH_EVIDENCE_SEARCH],
        phase=DecisionPhase.SEARCH_COMPLETION,
    )

    envelope = await adapter.query_decision(single_choice_request)

    assert envelope.status == DecisionStatus.DETERMINISTIC_SINGLETON
    assert envelope.allowed_choice == DecisionAction.DISPATCH_EVIDENCE_SEARCH
    assert envelope.confidence is None
    assert envelope.answer_confidence is None
    assert envelope.model_version == "deterministic_singleton"
    assert envelope.latency_ms == 0
    assert "single eligible choice" in (envelope.rationale or "")
    assert adapter.call_count == 1
    assert len(adapter.recorded_requests) == 1


@pytest.mark.asyncio
async def test_fixture_adapter_multi_choice_query() -> None:
    """Verifies default multi choice routing evaluation."""
    adapter = FixtureLayaDecisionAdapter()
    multi_choice_request = _make_sample_request(
        eligible_choices=[
            DecisionAction.DISPATCH_BROWSER_ESCALATION,
            DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
        ]
    )

    envelope = await adapter.query_decision(multi_choice_request)

    assert envelope.status == DecisionStatus.VALIDATED
    assert envelope.allowed_choice == DecisionAction.DISPATCH_BROWSER_ESCALATION
    assert envelope.answer_confidence == 0.820
    assert envelope.confidence is None
    assert envelope.model_version == "typed-decisions"
    assert adapter.call_count == 1


@pytest.mark.asyncio
async def test_fixture_adapter_canned_wire_response() -> None:
    """Verifies that canned wire responses translate accurately through query_decision."""
    canned_wire = FixtureLayaDecisionAdapter.make_sample_extended_wire_response(
        choice="PROCEED_DETERMINISTIC_EVALUATION",
        probabilities={
            "PROCEED_DETERMINISTIC_EVALUATION": 0.850,
            "DISPATCH_BROWSER_ESCALATION": 0.150,
        },
        answer_confidence=0.850,
        x_jev_confidence=0.740,
        model="custom-laya-model",
        routing_model="custom-routed-model",
    )
    adapter = FixtureLayaDecisionAdapter(canned_wire_response=canned_wire)
    multi_choice_request = _make_sample_request(
        eligible_choices=[
            DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
            DecisionAction.DISPATCH_BROWSER_ESCALATION,
        ]
    )

    envelope = await adapter.query_decision(multi_choice_request)

    assert envelope.status == DecisionStatus.VALIDATED
    assert envelope.allowed_choice == DecisionAction.PROCEED_DETERMINISTIC_EVALUATION
    assert envelope.answer_confidence == 0.850
    assert envelope.confidence is None
    assert envelope.model_version == "custom-routed-model"


@pytest.mark.asyncio
async def test_fixture_adapter_canned_envelope_override() -> None:
    """Verifies that injecting an explicit canned envelope returns it directly."""
    request = _make_sample_request()
    custom_envelope = DecisionEnvelope(
        decision_id=uuid4(),
        decision_type=DecisionType.EVIDENCE_ROUTING,
        phase=DecisionPhase.BROWSER_ELIGIBILITY,
        execution_id=SAMPLE_EXECUTION_ID,
        trip_id=None,
        idempotency_key="custom_key",
        allowed_choice=DecisionAction.DISPATCH_EVIDENCE_SEARCH,
        status=DecisionStatus.FALLBACK_TIMEOUT,
        confidence=None,
        answer_confidence=None,
        model_version=None,
        policy_version="laya-policy-v1",
        rationale="remote call timed out",
        latency_ms=5000,
        request_digest="digest123",
        created_at="2026-10-17T16:00:00Z",
    )
    adapter = FixtureLayaDecisionAdapter(canned_envelope=custom_envelope)

    result = await adapter.query_decision(request)
    assert result == custom_envelope
    assert result.status == DecisionStatus.FALLBACK_TIMEOUT
    assert adapter.call_count == 1
