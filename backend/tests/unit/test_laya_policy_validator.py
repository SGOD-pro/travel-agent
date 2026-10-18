"""Unit tests for LayaPolicyValidator action eligibility and deterministic fallback table.

Tests cover spec 0004 Slice 3:
1. Action eligibility across explicit phases:
   - Phase 1 (SEARCH_COMPLETION) yields single choice DISPATCH_EVIDENCE_SEARCH.
   - Phase 2 (BROWSER_ELIGIBILITY) yields multi-choice state when optional requests/budget exist.
   - Phase 3 (EVALUATION) yields single choice PROCEED_DETERMINISTIC_EVALUATION when evidence verified.
2. Zero choice and single choice deterministic bypass handling.
3. Policy gating rules using answer_confidence, probability key validation against criteria,
   finite float validation, unity normalization, argmax consistency, abstention and truncation rejection.
4. Deterministic fallback table mapping timeouts, errors, rejections, truncations, and low confidence.
5. Evidence preservation invariant: never prematurely evaluates without required destination evidence.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from contracts.decision import (
    DecisionAction,
    DecisionPhase,
    DecisionStatus,
    LayaDecisionRequest,
)
from functions.orchestrator.decision.policy import (
    ExecutionCancelledError,
    ExecutionDeadlineExceededError,
    InsufficientRequiredEvidenceError,
    LayaPolicyValidator,
    compute_request_digest,
)


def make_request(
    phase: DecisionPhase = DecisionPhase.BROWSER_ELIGIBILITY,
    eligible_choices: list[DecisionAction] | None = None,
    budget_remaining: int = 3,
) -> LayaDecisionRequest:
    """Helper to create a deterministic LayaDecisionRequest for testing."""
    if eligible_choices is None:
        eligible_choices = [
            DecisionAction.DISPATCH_BROWSER_ESCALATION,
            DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
            DecisionAction.DISPATCH_EVIDENCE_SEARCH,
        ]
    return LayaDecisionRequest(
        decision_id=uuid4(),
        phase=phase,
        execution_id=uuid4(),
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="evidence_routing:v1",
        policy_derived_eligible_choices=eligible_choices,
        budget_remaining=budget_remaining,
        context={"destination": "Kolkata", "category": "lodging"},
        idempotency_key=f"test:{uuid4().hex[:12]}",
    )


class TestActionEligibility:
    """Tests for action eligibility computation per Phase."""

    def test_phase1_search_completion_prerequisites(self) -> None:
        """Phase 1 yields only DISPATCH_EVIDENCE_SEARCH prior to static search completion."""
        eligible = LayaPolicyValidator.compute_eligible_choices(
            DecisionPhase.SEARCH_COMPLETION,
            static_search_completed=False,
            required_evidence_satisfied=False,
            parent_status="RUNNING",
        )
        assert eligible == [DecisionAction.DISPATCH_EVIDENCE_SEARCH]
        assert LayaPolicyValidator.is_singleton(eligible) is True

    def test_phase1_ineligible_when_search_already_completed(self) -> None:
        """Phase 1 yields no actions if static search already completed."""
        eligible = LayaPolicyValidator.compute_eligible_choices(
            DecisionPhase.SEARCH_COMPLETION,
            static_search_completed=True,
            parent_status="RUNNING",
        )
        assert eligible == []

    def test_phase2_valid_multichoice_state(self) -> None:
        """Phase 2 produces valid multi-choice state with search, browser, and evaluation."""
        eligible = LayaPolicyValidator.compute_eligible_choices(
            DecisionPhase.BROWSER_ELIGIBILITY,
            static_search_completed=True,
            required_evidence_satisfied=True,
            unhandled_browser_requests=2,
            remaining_browser_slots=3,
            remaining_search_budget=1,
            parent_status="RUNNING",
        )
        assert DecisionAction.DISPATCH_BROWSER_ESCALATION in eligible
        assert DecisionAction.PROCEED_DETERMINISTIC_EVALUATION in eligible
        assert DecisionAction.DISPATCH_EVIDENCE_SEARCH in eligible
        assert len(eligible) == 3
        assert LayaPolicyValidator.is_singleton(eligible) is False

    def test_phase2_browser_ineligible_when_zero_slots_remaining(self) -> None:
        """Browser escalation excluded when remaining slots is zero."""
        eligible = LayaPolicyValidator.compute_eligible_choices(
            DecisionPhase.BROWSER_ELIGIBILITY,
            static_search_completed=True,
            required_evidence_satisfied=True,
            unhandled_browser_requests=2,
            remaining_browser_slots=0,
            remaining_search_budget=0,
            parent_status="RUNNING",
        )
        assert DecisionAction.DISPATCH_BROWSER_ESCALATION not in eligible
        assert eligible == [DecisionAction.PROCEED_DETERMINISTIC_EVALUATION]
        assert LayaPolicyValidator.is_singleton(eligible) is True

    def test_phase2_evaluation_ineligible_without_required_evidence(self) -> None:
        """Evaluation strictly ineligible in Phase 2 if core required destination evidence is missing."""
        eligible = LayaPolicyValidator.compute_eligible_choices(
            DecisionPhase.BROWSER_ELIGIBILITY,
            static_search_completed=True,
            required_evidence_satisfied=False,
            unhandled_browser_requests=1,
            remaining_browser_slots=2,
            remaining_search_budget=1,
            parent_status="RUNNING",
        )
        assert DecisionAction.PROCEED_DETERMINISTIC_EVALUATION not in eligible
        assert DecisionAction.DISPATCH_BROWSER_ESCALATION in eligible
        assert DecisionAction.DISPATCH_EVIDENCE_SEARCH in eligible

    def test_phase3_evaluation_eligible_only_when_evidence_satisfied(self) -> None:
        """Phase 3 yields PROCEED_DETERMINISTIC_EVALUATION only if required evidence verified."""
        eligible_ok = LayaPolicyValidator.compute_eligible_choices(
            DecisionPhase.EVALUATION,
            required_evidence_satisfied=True,
            parent_status="RUNNING",
        )
        assert eligible_ok == [DecisionAction.PROCEED_DETERMINISTIC_EVALUATION]
        assert LayaPolicyValidator.is_singleton(eligible_ok) is True

        eligible_missing = LayaPolicyValidator.compute_eligible_choices(
            DecisionPhase.EVALUATION,
            required_evidence_satisfied=False,
            parent_status="RUNNING",
        )
        assert eligible_missing == []

    def test_cancellation_and_deadline_halt_eligibility(self) -> None:
        """Parent cancellation or deadline expiration halts all eligibility."""
        eligible_cancelled = LayaPolicyValidator.compute_eligible_choices(
            DecisionPhase.BROWSER_ELIGIBILITY,
            cancelled=True,
        )
        assert eligible_cancelled == []

        eligible_status_cancelled = LayaPolicyValidator.compute_eligible_choices(
            DecisionPhase.BROWSER_ELIGIBILITY,
            parent_status="CANCELLED",
        )
        assert eligible_status_cancelled == []

        eligible_deadline = LayaPolicyValidator.compute_eligible_choices(
            DecisionPhase.BROWSER_ELIGIBILITY,
            deadline_exceeded=True,
        )
        assert eligible_deadline == []


class TestSingletonAndZeroChoiceBypass:
    """Tests for deterministic singleton decisions and zero choice scenarios."""

    def test_deterministic_singleton_decision_semantics(self) -> None:
        """Singleton decision produces DETERMINISTIC_SINGLETON with confidence=null and answer_confidence=null."""
        req = make_request(
            phase=DecisionPhase.SEARCH_COMPLETION,
            eligible_choices=[DecisionAction.DISPATCH_EVIDENCE_SEARCH],
        )
        envelope = LayaPolicyValidator.create_singleton_decision(
            req,
            DecisionAction.DISPATCH_EVIDENCE_SEARCH,
        )
        assert envelope.status == DecisionStatus.DETERMINISTIC_SINGLETON
        assert envelope.allowed_choice == DecisionAction.DISPATCH_EVIDENCE_SEARCH
        assert envelope.confidence is None
        assert envelope.answer_confidence is None
        assert envelope.model_version == "deterministic_singleton"
        assert envelope.latency_ms == 0
        assert envelope.policy_version == "laya-policy-v1"
        assert "deterministic bypass" in (envelope.rationale or "")

    def test_zero_choices_satisfied_evidence_advances_to_evaluation(self) -> None:
        """Zero choices with verified required evidence defaults to deterministic evaluation."""
        req = make_request(
            phase=DecisionPhase.BROWSER_ELIGIBILITY,
            eligible_choices=[DecisionAction.PROCEED_DETERMINISTIC_EVALUATION],
        )
        envelope = LayaPolicyValidator.handle_zero_choices(req, required_evidence_satisfied=True)
        assert envelope.status == DecisionStatus.DETERMINISTIC_SINGLETON
        assert envelope.allowed_choice == DecisionAction.PROCEED_DETERMINISTIC_EVALUATION
        assert envelope.confidence is None
        assert envelope.answer_confidence is None

    def test_zero_choices_missing_evidence_raises_terminal_error(self) -> None:
        """Zero choices without verified required evidence raises InsufficientRequiredEvidenceError."""
        req = make_request(
            phase=DecisionPhase.BROWSER_ELIGIBILITY,
            eligible_choices=[DecisionAction.DISPATCH_EVIDENCE_SEARCH],
        )
        with pytest.raises(InsufficientRequiredEvidenceError, match="Insufficient required destination evidence"):
            LayaPolicyValidator.handle_zero_choices(req, required_evidence_satisfied=False)


class TestPolicyResponseValidation:
    """Tests for LayaPolicyValidator.validate_response."""

    def test_valid_response_acceptance(self) -> None:
        """Valid probabilities, argmax consistency, and answer_confidence >= 0.700 produces VALIDATED."""
        req = make_request()
        probs = {
            "DISPATCH_BROWSER_ESCALATION": 0.820,
            "PROCEED_DETERMINISTIC_EVALUATION": 0.110,
            "DISPATCH_EVIDENCE_SEARCH": 0.070,
        }
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice="DISPATCH_BROWSER_ESCALATION",
            probabilities=probs,
            confidence=0.730,  # Distinct calibrated confidence
            answer_confidence=0.820,
            abstention="passed",
            low_confidence=False,
            usage_truncated=False,
            latency_ms=120,
            model_version="laya-v0.4.9",
        )
        assert envelope.status == DecisionStatus.VALIDATED
        assert envelope.allowed_choice == DecisionAction.DISPATCH_BROWSER_ESCALATION
        assert envelope.answer_confidence == 0.820
        assert envelope.confidence == 0.730
        assert envelope.model_version == "laya-v0.4.9"
        assert envelope.latency_ms == 120
        assert "policy accepted" in (envelope.rationale or "")

    def test_answer_confidence_derived_from_probabilities_if_missing(self) -> None:
        """If answer_confidence is omitted in strict mode, it is derived from probabilities[choice]."""
        req = make_request()
        probs = {
            "DISPATCH_BROWSER_ESCALATION": 0.820,
            "PROCEED_DETERMINISTIC_EVALUATION": 0.110,
            "DISPATCH_EVIDENCE_SEARCH": 0.070,
        }
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice="DISPATCH_BROWSER_ESCALATION",
            probabilities=probs,
            confidence=None,  # Null entropy confidence
            answer_confidence=None,
        )
        assert envelope.status == DecisionStatus.VALIDATED
        assert envelope.answer_confidence == 0.820
        assert envelope.confidence is None

    def test_low_confidence_gating_triggers_fallback(self) -> None:
        """answer_confidence < 0.700 triggers FALLBACK_LOW_CONFIDENCE with phase derived action."""
        req = make_request()
        probs = {
            "DISPATCH_BROWSER_ESCALATION": 0.650,
            "PROCEED_DETERMINISTIC_EVALUATION": 0.200,
            "DISPATCH_EVIDENCE_SEARCH": 0.150,
        }
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice="DISPATCH_BROWSER_ESCALATION",
            probabilities=probs,
            answer_confidence=0.650,
            threshold=0.700,
            unhandled_browser_requests=1,
            remaining_browser_slots=2,
            required_evidence_satisfied=True,
        )
        assert envelope.status == DecisionStatus.FALLBACK_LOW_CONFIDENCE
        assert envelope.allowed_choice == DecisionAction.DISPATCH_BROWSER_ESCALATION
        assert envelope.answer_confidence == 0.650
        assert envelope.confidence is None
        assert "below threshold 0.700" in (envelope.rationale or "")

    def test_explicit_abstention_triggers_fallback(self) -> None:
        """abstention=='abstained' triggers FALLBACK_ABSTAINED."""
        req = make_request()
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice="DISPATCH_BROWSER_ESCALATION",
            probabilities={
                "DISPATCH_BROWSER_ESCALATION": 0.800,
                "PROCEED_DETERMINISTIC_EVALUATION": 0.100,
                "DISPATCH_EVIDENCE_SEARCH": 0.100,
            },
            abstention="abstained",
            unhandled_browser_requests=1,
            remaining_browser_slots=2,
        )
        assert envelope.status == DecisionStatus.FALLBACK_ABSTAINED
        assert envelope.allowed_choice == DecisionAction.DISPATCH_BROWSER_ESCALATION

    def test_tokenizer_truncation_triggers_fallback(self) -> None:
        """usage_truncated==True triggers FALLBACK_TRUNCATED."""
        req = make_request()
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice="DISPATCH_BROWSER_ESCALATION",
            probabilities={
                "DISPATCH_BROWSER_ESCALATION": 0.800,
                "PROCEED_DETERMINISTIC_EVALUATION": 0.100,
                "DISPATCH_EVIDENCE_SEARCH": 0.100,
            },
            usage_truncated=True,
            unhandled_browser_requests=1,
            remaining_browser_slots=2,
        )
        assert envelope.status == DecisionStatus.FALLBACK_TRUNCATED
        assert "tokenizer truncated" in (envelope.rationale or "")

    def test_probability_keys_criteria_mismatch_triggers_malformed(self) -> None:
        """Missing or extraneous probability keys triggers FALLBACK_MALFORMED."""
        req = make_request(
            eligible_choices=[
                DecisionAction.DISPATCH_BROWSER_ESCALATION,
                DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
            ]
        )
        # Missing PROCEED_DETERMINISTIC_EVALUATION, has extra UNKNOWN
        probs = {
            "DISPATCH_BROWSER_ESCALATION": 0.850,
            "UNKNOWN_ACTION": 0.150,
        }
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice="DISPATCH_BROWSER_ESCALATION",
            probabilities=probs,
            unhandled_browser_requests=1,
            remaining_browser_slots=2,
        )
        assert envelope.status == DecisionStatus.FALLBACK_MALFORMED
        assert "do not match offered criteria" in (envelope.rationale or "")

    def test_non_finite_probability_triggers_malformed(self) -> None:
        """NaN or Inf probability values trigger FALLBACK_MALFORMED."""
        req = make_request()
        probs = {
            "DISPATCH_BROWSER_ESCALATION": float("nan"),
            "PROCEED_DETERMINISTIC_EVALUATION": 0.500,
            "DISPATCH_EVIDENCE_SEARCH": 0.500,
        }
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice="DISPATCH_BROWSER_ESCALATION",
            probabilities=probs,
            unhandled_browser_requests=1,
            remaining_browser_slots=2,
        )
        assert envelope.status == DecisionStatus.FALLBACK_MALFORMED

    def test_non_normalized_probabilities_triggers_malformed(self) -> None:
        """Probabilities summing to 1.5 trigger FALLBACK_MALFORMED."""
        req = make_request()
        probs = {
            "DISPATCH_BROWSER_ESCALATION": 0.900,
            "PROCEED_DETERMINISTIC_EVALUATION": 0.400,
            "DISPATCH_EVIDENCE_SEARCH": 0.200,
        }
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice="DISPATCH_BROWSER_ESCALATION",
            probabilities=probs,
            unhandled_browser_requests=1,
            remaining_browser_slots=2,
        )
        assert envelope.status == DecisionStatus.FALLBACK_MALFORMED
        assert "do not normalize to 1.0" in (envelope.rationale or "")

    def test_argmax_inconsistency_triggers_malformed(self) -> None:
        """Candidate choice differing from argmax probability triggers FALLBACK_MALFORMED."""
        req = make_request()
        probs = {
            "DISPATCH_BROWSER_ESCALATION": 0.800,
            "PROCEED_DETERMINISTIC_EVALUATION": 0.150,
            "DISPATCH_EVIDENCE_SEARCH": 0.050,
        }
        # Model returned choice PROCEED_DETERMINISTIC_EVALUATION despite 0.150 < 0.800
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice="PROCEED_DETERMINISTIC_EVALUATION",
            probabilities=probs,
            unhandled_browser_requests=1,
            remaining_browser_slots=2,
        )
        assert envelope.status == DecisionStatus.FALLBACK_MALFORMED
        assert "does not match argmax" in (envelope.rationale or "")

    def test_ineligible_action_triggers_rejected_by_policy(self) -> None:
        """Model proposing an ineligible choice triggers REJECTED_BY_POLICY with prioritized fallback."""
        req = make_request(
            phase=DecisionPhase.BROWSER_ELIGIBILITY,
            eligible_choices=[
                DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
                DecisionAction.DISPATCH_EVIDENCE_SEARCH,
            ],
        )
        # Model proposes DISPATCH_BROWSER_ESCALATION which is not in eligible choices
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice=DecisionAction.DISPATCH_BROWSER_ESCALATION,
            probabilities={
                "PROCEED_DETERMINISTIC_EVALUATION": 0.500,
                "DISPATCH_EVIDENCE_SEARCH": 0.500,
            },
            required_evidence_satisfied=True,
        )
        assert envelope.status == DecisionStatus.REJECTED_BY_POLICY
        assert envelope.allowed_choice == DecisionAction.PROCEED_DETERMINISTIC_EVALUATION
        assert "is ineligible" in (envelope.rationale or "")


class TestCancellationAndDeadlineBarriers:
    """Tests for execution barriers in policy validator."""

    def test_cancelled_execution_raises_error(self) -> None:
        """Cancelled execution immediately raises ExecutionCancelledError."""
        req = make_request()
        with pytest.raises(ExecutionCancelledError, match="CANCELLED"):
            LayaPolicyValidator.validate_response(
                req,
                candidate_choice="DISPATCH_BROWSER_ESCALATION",
                probabilities=None,
                parent_status="CANCELLED",
            )

    def test_deadline_exceeded_raises_error(self) -> None:
        """Deadline exceeded immediately raises ExecutionDeadlineExceededError."""
        req = make_request()
        with pytest.raises(ExecutionDeadlineExceededError, match="deadline"):
            LayaPolicyValidator.validate_response(
                req,
                candidate_choice="DISPATCH_BROWSER_ESCALATION",
                probabilities=None,
                deadline_exceeded=True,
            )


class TestDeterministicFallbackTable:
    """Tests for specific fallback table mappings and evidence preservation."""

    def test_unconfigured_url_triggers_fallback_unconfigured(self) -> None:
        """Unconfigured URL triggers FALLBACK_UNCONFIGURED with phase default."""
        req = make_request(phase=DecisionPhase.SEARCH_COMPLETION)
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice=None,
            probabilities=None,
            fallback_status=DecisionStatus.FALLBACK_UNCONFIGURED,
            fallback_rationale="LAYA_ENDPOINT_URL is not configured",
        )
        assert envelope.status == DecisionStatus.FALLBACK_UNCONFIGURED
        assert envelope.allowed_choice == DecisionAction.DISPATCH_EVIDENCE_SEARCH

    def test_timeout_triggers_fallback_timeout(self) -> None:
        """Client timeout triggers FALLBACK_TIMEOUT with browser escalation if available."""
        req = make_request(phase=DecisionPhase.BROWSER_ELIGIBILITY)
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice=None,
            probabilities=None,
            fallback_status=DecisionStatus.FALLBACK_TIMEOUT,
            fallback_rationale="cumulative deadline exceeded",
            unhandled_browser_requests=1,
            remaining_browser_slots=2,
        )
        assert envelope.status == DecisionStatus.FALLBACK_TIMEOUT
        assert envelope.allowed_choice == DecisionAction.DISPATCH_BROWSER_ESCALATION

    def test_http_error_triggers_fallback_error(self) -> None:
        """HTTP error triggers FALLBACK_ERROR with evaluation fallback when evidence verified."""
        req = make_request(
            phase=DecisionPhase.BROWSER_ELIGIBILITY,
            eligible_choices=[DecisionAction.PROCEED_DETERMINISTIC_EVALUATION],
        )
        envelope = LayaPolicyValidator.validate_response(
            req,
            candidate_choice=None,
            probabilities=None,
            fallback_status=DecisionStatus.FALLBACK_ERROR,
            fallback_rationale="remote 503 Service Unavailable",
            unhandled_browser_requests=0,
            remaining_browser_slots=0,
            required_evidence_satisfied=True,
        )
        assert envelope.status == DecisionStatus.FALLBACK_ERROR
        assert envelope.allowed_choice == DecisionAction.PROCEED_DETERMINISTIC_EVALUATION

    def test_evidence_preservation_rule_in_fallback(self) -> None:
        """Fallback never skips to evaluation if required destination evidence is unverified and budget exhausted."""
        req = make_request(
            phase=DecisionPhase.BROWSER_ELIGIBILITY,
            eligible_choices=[DecisionAction.DISPATCH_EVIDENCE_SEARCH],
        )
        with pytest.raises(InsufficientRequiredEvidenceError, match="Insufficient required destination evidence"):
            LayaPolicyValidator.resolve_fallback(
                req,
                DecisionStatus.FALLBACK_ERROR,
                required_evidence_satisfied=False,
                unhandled_browser_requests=0,
                remaining_browser_slots=0,
                remaining_search_budget=0,
            )

    def test_request_digest_is_deterministic(self) -> None:
        """Request digest produces consistent 64 character SHA256 hex string."""
        req = make_request()
        digest1 = compute_request_digest(req)
        digest2 = compute_request_digest(req)
        assert digest1 == digest2
        assert len(digest1) == 64
