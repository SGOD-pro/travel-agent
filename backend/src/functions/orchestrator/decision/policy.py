"""Deterministic policy validator and action eligibility engine for LAYA decision routing.

Conforms to spec 0004 Slice 3:
1. Action eligibility evaluation across three explicit workflow phases:
   - SEARCH_COMPLETION: singleton bypass to DISPATCH_EVIDENCE_SEARCH.
   - BROWSER_ELIGIBILITY: multi-choice routing when static search has completed
     and optional enrichment requests/budgets exist.
   - EVALUATION: singleton bypass to PROCEED_DETERMINISTIC_EVALUATION only when
     required destination evidence is verified.
2. Deterministic singleton bypass assigning confidence=null and answer_confidence=null
   with status DETERMINISTIC_SINGLETON.
3. Policy gating using answer_confidence consistently against threshold (default 0.700),
   validating probability keys against offered criteria, finite float values,
   unity normalization, argmax consistency, rejecting abstentions and tokenizer truncations.
4. Deterministic fallback table mapping all failure conditions to safe default actions,
   strictly preserving required destination evidence without premature evaluation.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime

from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionPhase,
    DecisionStatus,
    LayaDecisionRequest,
)


class InsufficientRequiredEvidenceError(RuntimeError):
    """Raised when required destination evidence is missing and cannot be gathered."""


class ExecutionCancelledError(RuntimeError):
    """Raised when parent execution is in cancelled state."""


class ExecutionDeadlineExceededError(RuntimeError):
    """Raised when parent execution deadline has expired."""


def compute_request_digest(request: LayaDecisionRequest) -> str:
    """Compute canonical SHA256 digest covering execution, phase, versions, and eligible choices."""
    sorted_choices = ",".join(sorted(c.value for c in request.policy_derived_eligible_choices))
    canonical_str = (
        f"{request.execution_id}:"
        f"{request.phase.value}:"
        f"{request.context_version}:"
        f"{request.policy_version}:"
        f"{request.question_version}:"
        f"{sorted_choices}"
    )
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


class LayaPolicyValidator:
    """Deterministic policy authority governing LAYA proposal acceptance and fallback routing."""

    POLICY_VERSION: str = "laya-policy-v1"
    DEFAULT_CONFIDENCE_THRESHOLD: float = 0.700

    @classmethod
    def compute_eligible_choices(
        cls,
        phase: DecisionPhase,
        *,
        parent_status: str = "RUNNING",
        cancelled: bool = False,
        deadline_exceeded: bool = False,
        static_search_completed: bool = False,
        required_evidence_satisfied: bool = False,
        unhandled_browser_requests: int = 0,
        remaining_browser_slots: int = 0,
        remaining_search_budget: int = 0,
    ) -> list[DecisionAction]:
        """Compute prerequisite filtered eligible decision actions for a workflow phase.

        Rules:
        - Parent cancellation or deadline expiration halts all actions.
        - SEARCH_COMPLETION: Only DISPATCH_EVIDENCE_SEARCH is eligible prior to search.
          PROCEED_DETERMINISTIC_EVALUATION and DISPATCH_BROWSER_ESCALATION are strictly ineligible.
        - BROWSER_ELIGIBILITY:
          * DISPATCH_BROWSER_ESCALATION: eligible only if static search completed, unhandled
            browser requests exist, and remaining browser slots > 0.
          * DISPATCH_EVIDENCE_SEARCH: eligible for secondary search if remaining search budget > 0.
          * PROCEED_DETERMINISTIC_EVALUATION: eligible if and only if required destination evidence
            is satisfied.
        - EVALUATION: Only PROCEED_DETERMINISTIC_EVALUATION is eligible, provided required evidence
          is satisfied.
        """
        is_running = parent_status.upper() in ("RUNNING", "IN_PROGRESS")
        if cancelled or parent_status.upper() == "CANCELLED" or deadline_exceeded:
            return []

        if phase == DecisionPhase.SEARCH_COMPLETION:
            if not static_search_completed and is_running:
                return [DecisionAction.DISPATCH_EVIDENCE_SEARCH]
            return []

        if phase == DecisionPhase.BROWSER_ELIGIBILITY:
            eligible: list[DecisionAction] = []
            if (
                static_search_completed
                and unhandled_browser_requests > 0
                and remaining_browser_slots > 0
                and is_running
            ):
                eligible.append(DecisionAction.DISPATCH_BROWSER_ESCALATION)

            if required_evidence_satisfied and is_running:
                eligible.append(DecisionAction.PROCEED_DETERMINISTIC_EVALUATION)

            if remaining_search_budget > 0 and is_running:
                eligible.append(DecisionAction.DISPATCH_EVIDENCE_SEARCH)

            return eligible

        if phase == DecisionPhase.EVALUATION:
            if required_evidence_satisfied and is_running:
                return [DecisionAction.PROCEED_DETERMINISTIC_EVALUATION]
            return []

        return []

    @classmethod
    def is_singleton(cls, eligible_choices: list[DecisionAction]) -> bool:
        """Check whether exactly one choice is eligible for deterministic bypass."""
        return len(eligible_choices) == 1

    @classmethod
    def create_singleton_decision(
        cls,
        request: LayaDecisionRequest,
        single_choice: DecisionAction,
    ) -> DecisionEnvelope:
        """Create a deterministic singleton bypass decision envelope."""
        now_iso = datetime.now(UTC).isoformat()
        digest = compute_request_digest(request)
        return DecisionEnvelope(
            decision_id=request.decision_id,
            decision_type=request.decision_type,
            phase=request.phase,
            execution_id=request.execution_id,
            trip_id=request.trip_id,
            idempotency_key=request.idempotency_key,
            allowed_choice=single_choice,
            status=DecisionStatus.DETERMINISTIC_SINGLETON,
            confidence=None,
            answer_confidence=None,
            model_version="deterministic_singleton",
            policy_version=cls.POLICY_VERSION,
            rationale="single eligible choice: deterministic bypass",
            latency_ms=0,
            request_digest=digest,
            created_at=now_iso,
        )

    @classmethod
    def handle_zero_choices(
        cls,
        request: LayaDecisionRequest,
        *,
        required_evidence_satisfied: bool = False,
    ) -> DecisionEnvelope:
        """Handle scenario where zero choices satisfy eligibility rules.

        If required evidence is verified, defaults to PROCEED_DETERMINISTIC_EVALUATION.
        If required evidence is missing, raises InsufficientRequiredEvidenceError.
        """
        if required_evidence_satisfied:
            now_iso = datetime.now(UTC).isoformat()
            digest = compute_request_digest(request)
            return DecisionEnvelope(
                decision_id=request.decision_id,
                decision_type=request.decision_type,
                phase=request.phase,
                execution_id=request.execution_id,
                trip_id=request.trip_id,
                idempotency_key=request.idempotency_key,
                allowed_choice=DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
                status=DecisionStatus.DETERMINISTIC_SINGLETON,
                confidence=None,
                answer_confidence=None,
                model_version="deterministic_zero_choices_satisfied",
                policy_version=cls.POLICY_VERSION,
                rationale="zero choices remaining; required destination evidence verified, advancing to evaluation",
                latency_ms=0,
                request_digest=digest,
                created_at=now_iso,
            )

        raise InsufficientRequiredEvidenceError(
            "Insufficient required destination evidence to evaluate trip; candidate existence or budget exhaustion never substitutes for verified coordinates"
        )

    @classmethod
    def resolve_fallback(
        cls,
        request: LayaDecisionRequest,
        fallback_status: DecisionStatus,
        *,
        rationale: str | None = None,
        latency_ms: int = 0,
        required_evidence_satisfied: bool = False,
        unhandled_browser_requests: int = 0,
        remaining_browser_slots: int = 0,
        remaining_search_budget: int = 0,
        low_confidence_score: float | None = None,
    ) -> DecisionEnvelope:
        """Resolve a fallback action per the deterministic fallback table."""
        now_iso = datetime.now(UTC).isoformat()
        digest = compute_request_digest(request)
        eligible = request.policy_derived_eligible_choices

        fallback_action: DecisionAction

        if fallback_status == DecisionStatus.REJECTED_BY_POLICY:
            # Pick highest priority remaining eligible choice
            if request.phase == DecisionPhase.SEARCH_COMPLETION:
                fallback_action = DecisionAction.DISPATCH_EVIDENCE_SEARCH
            elif request.phase == DecisionPhase.BROWSER_ELIGIBILITY:
                if (
                    DecisionAction.DISPATCH_BROWSER_ESCALATION in eligible
                    and unhandled_browser_requests > 0
                    and remaining_browser_slots > 0
                ):
                    fallback_action = DecisionAction.DISPATCH_BROWSER_ESCALATION
                elif DecisionAction.PROCEED_DETERMINISTIC_EVALUATION in eligible and required_evidence_satisfied:
                    fallback_action = DecisionAction.PROCEED_DETERMINISTIC_EVALUATION
                elif DecisionAction.DISPATCH_EVIDENCE_SEARCH in eligible:
                    fallback_action = DecisionAction.DISPATCH_EVIDENCE_SEARCH
                elif eligible:
                    fallback_action = eligible[0]
                elif required_evidence_satisfied:
                    fallback_action = DecisionAction.PROCEED_DETERMINISTIC_EVALUATION
                else:
                    raise InsufficientRequiredEvidenceError(
                        "Cannot fallback from rejected policy without required evidence"
                    )
            elif request.phase == DecisionPhase.EVALUATION:
                if required_evidence_satisfied:
                    fallback_action = DecisionAction.PROCEED_DETERMINISTIC_EVALUATION
                else:
                    raise InsufficientRequiredEvidenceError(
                        "Cannot fallback to evaluation without required destination evidence"
                    )
            else:
                fallback_action = eligible[0] if eligible else DecisionAction.DISPATCH_EVIDENCE_SEARCH

        else:
            # Phase-derived defaults
            if request.phase == DecisionPhase.SEARCH_COMPLETION:
                fallback_action = DecisionAction.DISPATCH_EVIDENCE_SEARCH
            elif request.phase == DecisionPhase.BROWSER_ELIGIBILITY:
                if (
                    DecisionAction.DISPATCH_BROWSER_ESCALATION in eligible
                    and unhandled_browser_requests > 0
                    and remaining_browser_slots > 0
                ):
                    fallback_action = DecisionAction.DISPATCH_BROWSER_ESCALATION
                elif (
                    DecisionAction.PROCEED_DETERMINISTIC_EVALUATION in eligible
                    and required_evidence_satisfied
                ):
                    fallback_action = DecisionAction.PROCEED_DETERMINISTIC_EVALUATION
                elif (
                    DecisionAction.DISPATCH_EVIDENCE_SEARCH in eligible
                    and remaining_search_budget > 0
                ):
                    fallback_action = DecisionAction.DISPATCH_EVIDENCE_SEARCH
                elif required_evidence_satisfied:
                    fallback_action = DecisionAction.PROCEED_DETERMINISTIC_EVALUATION
                else:
                    raise InsufficientRequiredEvidenceError(
                        "Insufficient required destination evidence to evaluate trip; search budget exhausted"
                    )
            elif request.phase == DecisionPhase.EVALUATION:
                if required_evidence_satisfied:
                    fallback_action = DecisionAction.PROCEED_DETERMINISTIC_EVALUATION
                else:
                    raise InsufficientRequiredEvidenceError(
                        "Insufficient required destination evidence to evaluate trip"
                    )
            else:
                fallback_action = eligible[0] if eligible else DecisionAction.DISPATCH_EVIDENCE_SEARCH

        default_rationale = f"deterministic fallback triggered: {fallback_status.value} -> {fallback_action.value}"
        final_rationale = rationale or default_rationale

        return DecisionEnvelope(
            decision_id=request.decision_id,
            decision_type=request.decision_type,
            phase=request.phase,
            execution_id=request.execution_id,
            trip_id=request.trip_id,
            idempotency_key=request.idempotency_key,
            allowed_choice=fallback_action,
            status=fallback_status,
            confidence=None,
            answer_confidence=low_confidence_score,
            model_version="deterministic_fallback",
            policy_version=cls.POLICY_VERSION,
            rationale=final_rationale[:1000],
            latency_ms=latency_ms,
            request_digest=digest,
            created_at=now_iso,
        )

    @classmethod
    def validate_response(
        cls,
        request: LayaDecisionRequest,
        *,
        candidate_choice: str | DecisionAction | None,
        probabilities: dict[str, float] | None,
        confidence: float | None = None,
        answer_confidence: float | None = None,
        abstention: str | None = None,
        low_confidence: bool | None = None,
        usage_truncated: bool | None = None,
        latency_ms: int = 0,
        model_version: str | None = None,
        threshold: float | None = None,
        parent_status: str = "RUNNING",
        cancelled: bool = False,
        deadline_exceeded: bool = False,
        required_evidence_satisfied: bool = False,
        unhandled_browser_requests: int = 0,
        remaining_browser_slots: int = 0,
        remaining_search_budget: int = 0,
        fallback_status: DecisionStatus | None = None,
        fallback_rationale: str | None = None,
    ) -> DecisionEnvelope:
        """Validate an upstream LAYA model proposal before task dispatch or database persistence.

        Enforces:
        - Cancellation and deadline barriers.
        - Fallback status triggers (timeouts, 5xx, unconfigured URL).
        - Tokenizer truncation rejection.
        - Probability keys matching question criteria.
        - Finite float values and unity normalization.
        - Choice matching argmax probability.
        - Gating against answer_confidence threshold (default 0.700).
        - Option membership in policy_derived_eligible_choices.
        """
        if cancelled or parent_status == "CANCELLED":
            raise ExecutionCancelledError("Execution is in CANCELLED state; halting decision dispatch")

        if deadline_exceeded:
            raise ExecutionDeadlineExceededError("Execution deadline exceeded; halting decision dispatch")

        # Handle explicit fallback trigger (e.g. from client timeouts or HTTP errors)
        if fallback_status is not None:
            return cls.resolve_fallback(
                request,
                fallback_status,
                rationale=fallback_rationale,
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
            )

        # 1. Tokenizer truncation check
        if usage_truncated is True:
            return cls.resolve_fallback(
                request,
                DecisionStatus.FALLBACK_TRUNCATED,
                rationale="tokenizer truncated input context or criteria",
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
            )

        # 2. Explicit abstention check
        if abstention == "abstained":
            return cls.resolve_fallback(
                request,
                DecisionStatus.FALLBACK_ABSTAINED,
                rationale="model explicitly abstained from categorical decision",
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
            )

        offered_criteria = {c.value for c in request.policy_derived_eligible_choices}

        # 3. Check candidate choice presence and option eligibility
        if candidate_choice is None:
            return cls.resolve_fallback(
                request,
                DecisionStatus.FALLBACK_MALFORMED,
                rationale="candidate choice is missing from response answers",
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
            )

        choice_str = (
            candidate_choice.value
            if isinstance(candidate_choice, DecisionAction)
            else str(candidate_choice)
        )

        if choice_str not in offered_criteria:
            return cls.resolve_fallback(
                request,
                DecisionStatus.REJECTED_BY_POLICY,
                rationale=f"proposed action '{choice_str}' is ineligible in phase {request.phase.value}",
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
            )

        # 4. Probability structure and criteria membership checks
        if probabilities is None or not isinstance(probabilities, dict) or not probabilities:
            return cls.resolve_fallback(
                request,
                DecisionStatus.FALLBACK_MALFORMED,
                rationale="response is missing valid probabilities dictionary",
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
            )

        prob_keys = set(probabilities.keys())

        if prob_keys != offered_criteria:
            return cls.resolve_fallback(
                request,
                DecisionStatus.FALLBACK_MALFORMED,
                rationale=f"probability keys {sorted(prob_keys)} do not match offered criteria {sorted(offered_criteria)}",
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
            )

        # 5. Finite values and normalization checks
        for key, val in probabilities.items():
            if not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0.0:
                return cls.resolve_fallback(
                    request,
                    DecisionStatus.FALLBACK_MALFORMED,
                    rationale=f"probability for '{key}' is not a non negative finite float: {val}",
                    latency_ms=latency_ms,
                    required_evidence_satisfied=required_evidence_satisfied,
                    unhandled_browser_requests=unhandled_browser_requests,
                    remaining_browser_slots=remaining_browser_slots,
                    remaining_search_budget=remaining_search_budget,
                )

        prob_sum = sum(probabilities.values())
        if abs(prob_sum - 1.0) > 0.02:
            return cls.resolve_fallback(
                request,
                DecisionStatus.FALLBACK_MALFORMED,
                rationale=f"probabilities do not normalize to 1.0: sum={prob_sum:.4f}",
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
            )

        # 6. Argmax consistency check
        max_key, max_prob = max(probabilities.items(), key=lambda item: item[1])
        choice_prob = probabilities.get(choice_str, -1.0)

        if choice_prob < 0.0 or abs(choice_prob - max_prob) > 1e-6:
            return cls.resolve_fallback(
                request,
                DecisionStatus.FALLBACK_MALFORMED,
                rationale=f"choice '{choice_str}' (prob {choice_prob:.3f}) does not match argmax option '{max_key}' (prob {max_prob:.3f})",
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
            )

        # 6. Option eligibility verification
        if choice_str not in offered_criteria:
            return cls.resolve_fallback(
                request,
                DecisionStatus.REJECTED_BY_POLICY,
                rationale=f"proposed action '{choice_str}' is ineligible in phase {request.phase.value}",
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
            )

        # 7. Answer confidence threshold gating
        resolved_threshold = (
            threshold if threshold is not None else cls.DEFAULT_CONFIDENCE_THRESHOLD
        )
        effective_answer_conf = (
            answer_confidence if answer_confidence is not None else choice_prob
        )

        if low_confidence is True or effective_answer_conf < resolved_threshold:
            return cls.resolve_fallback(
                request,
                DecisionStatus.FALLBACK_LOW_CONFIDENCE,
                rationale=f"answer confidence {effective_answer_conf:.3f} below threshold {resolved_threshold:.3f}",
                latency_ms=latency_ms,
                required_evidence_satisfied=required_evidence_satisfied,
                unhandled_browser_requests=unhandled_browser_requests,
                remaining_browser_slots=remaining_browser_slots,
                remaining_search_budget=remaining_search_budget,
                low_confidence_score=effective_answer_conf,
            )

        # 8. All policy validations pass: return VALIDATED envelope
        now_iso = datetime.now(UTC).isoformat()
        digest = compute_request_digest(request)
        action = DecisionAction(choice_str)

        return DecisionEnvelope(
            decision_id=request.decision_id,
            decision_type=request.decision_type,
            phase=request.phase,
            execution_id=request.execution_id,
            trip_id=request.trip_id,
            idempotency_key=request.idempotency_key,
            allowed_choice=action,
            status=DecisionStatus.VALIDATED,
            confidence=confidence,  # Preserved as entropy confidence if supplied, or None
            answer_confidence=effective_answer_conf,
            model_version=model_version or "laya-remote",
            policy_version=cls.POLICY_VERSION,
            rationale=f"policy accepted {action.value} with answer confidence {effective_answer_conf:.3f} >= {resolved_threshold:.3f}",
            latency_ms=latency_ms,
            request_digest=digest,
            created_at=now_iso,
        )
