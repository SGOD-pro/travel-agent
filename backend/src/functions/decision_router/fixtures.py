"""Offline fixture adapter for LAYA decision routing.

Conforms to spec 0004 Slice 1:
- Implements LayaDecisionPort for offline testing with zero external network dependencies.
- Translates between internal domain models and upstream wire schemas.
- Supports canned extended and strict wire responses, deterministic singleton bypass,
  and configurable failure scenarios.
- Strictly test only: must never be injected as a production HTTP client.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionStatus,
    LayaDecisionPort,
    LayaDecisionRequest,
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
)

SAMPLE_QUESTION_KEY = "evidence_routing"

SAMPLE_EXTENDED_PROBABILITIES: dict[str, float] = {
    DecisionAction.DISPATCH_BROWSER_ESCALATION: 0.820,
    DecisionAction.PROCEED_DETERMINISTIC_EVALUATION: 0.110,
    DecisionAction.DISPATCH_EVIDENCE_SEARCH: 0.070,
}


def compute_request_digest(
    execution_id: str,
    phase: str,
    context_version: int,
    policy_version: str,
    question_version: str,
    eligible_choices: list[str],
) -> str:
    """Computes a deterministic SHA256 canonical request digest."""
    payload = {
        "execution_id": execution_id,
        "phase": phase,
        "context_version": context_version,
        "policy_version": policy_version,
        "question_version": question_version,
        "eligible_choices": sorted(eligible_choices),
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FixtureLayaDecisionAdapter(LayaDecisionPort):
    """Offline test fixture adapter implementing LayaDecisionPort."""

    def __init__(
        self,
        canned_envelope: DecisionEnvelope | None = None,
        canned_wire_response: LayaWireResponse | None = None,
        default_status: DecisionStatus = DecisionStatus.VALIDATED,
        health_status: bool = True,
    ) -> None:
        self.is_test_fixture = True
        self._canned_envelope = canned_envelope
        self._canned_wire_response = canned_wire_response
        self._default_status = default_status
        self._health_status = health_status
        self.call_count = 0
        self.recorded_requests: list[LayaDecisionRequest] = []

    @classmethod
    def make_sample_extended_wire_response(
        cls,
        choice: str = DecisionAction.DISPATCH_BROWSER_ESCALATION,
        probabilities: dict[str, float] | None = None,
        answer_confidence: float = 0.820,
        x_jev_confidence: float | None = 0.730,
        confidence: float | None = None,
        model: str = "laya-rl-agent",
        routing_model: str = "typed-decisions",
        truncated: bool = False,
    ) -> LayaWireResponse:
        """Constructs an illustrative extended Jev wire response payload."""
        probs = probabilities if probabilities is not None else dict(SAMPLE_EXTENDED_PROBABILITIES)
        return LayaWireResponse(
            model=model,
            answers={
                SAMPLE_QUESTION_KEY: LayaChoiceAnswer(
                    type="choice",
                    choice=choice,
                    probabilities=probs,
                    answer_confidence=answer_confidence,
                    confidence=confidence,
                    x_jev_confidence=x_jev_confidence,
                    abstention="passed",
                    abstention_threshold=0.700,
                    low_confidence=False,
                )
            },
            usage=LayaWireUsage(
                input_tokens=128,
                output_tokens=0,
                state_tokens=42,
                state_tokens_dropped=0,
                truncated=truncated,
            ),
            routing=LayaWireRouting(
                model=routing_model,
                repo="convaiinnovations/laya-typed-decisions",
                reason="Explicit model selection",
            ),
        )

    @classmethod
    def make_sample_strict_wire_response(
        cls,
        choice: str = DecisionAction.DISPATCH_BROWSER_ESCALATION,
        probabilities: dict[str, float] | None = None,
        model: str = "typed-decisions",
    ) -> LayaWireResponse:
        """Constructs a strict mode wire response omitting optional fields."""
        probs = probabilities if probabilities is not None else dict(SAMPLE_EXTENDED_PROBABILITIES)
        return LayaWireResponse(
            model=model,
            answers={
                SAMPLE_QUESTION_KEY: LayaChoiceAnswer(
                    type="choice",
                    choice=choice,
                    probabilities=probs,
                    answer_confidence=None,
                    confidence=None,
                    x_jev_confidence=None,
                    abstention=None,
                    abstention_threshold=None,
                    low_confidence=None,
                )
            },
            usage=None,
            routing=None,
        )

    @classmethod
    def make_sample_wire_request(
        cls,
        request: LayaDecisionRequest,
        min_confidence: float = 0.700,
        model: str = "typed-decisions",
    ) -> LayaWireRequest:
        """Constructs an upstream wire request payload from domain request."""
        criteria: dict[str, str] = {}
        action_descriptions = {
            DecisionAction.DISPATCH_BROWSER_ESCALATION: "Escalate dynamic lodging page to browser rendering worker",
            DecisionAction.PROCEED_DETERMINISTIC_EVALUATION: "Bypass browser escalation and proceed directly to schedule solving with existing evidence",
            DecisionAction.DISPATCH_EVIDENCE_SEARCH: "Perform secondary web search for regional transit advisories",
        }
        for act in request.policy_derived_eligible_choices:
            criteria[act.value] = action_descriptions.get(act, f"Action {act.value}")

        return LayaWireRequest(
            state=dict(request.context),
            questions={
                SAMPLE_QUESTION_KEY: LayaWireQuestion(
                    type="choice",
                    instructions="Select next routing step based on evidence completeness and budget",
                    criteria=criteria,
                )
            },
            min_confidence=min_confidence,
            model=model,
        )

    @classmethod
    def create_singleton_envelope(cls, request: LayaDecisionRequest) -> DecisionEnvelope:
        """Creates a deterministic singleton bypass envelope."""
        single_choice = request.policy_derived_eligible_choices[0]
        digest = compute_request_digest(
            execution_id=str(request.execution_id),
            phase=request.phase.value,
            context_version=request.context_version,
            policy_version=request.policy_version,
            question_version=request.question_version,
            eligible_choices=[single_choice.value],
        )
        return DecisionEnvelope(
            decision_id=request.decision_id or uuid4(),
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
            policy_version=request.policy_version,
            rationale="single eligible choice: deterministic bypass",
            latency_ms=0,
            request_digest=digest,
            created_at=datetime.now(UTC).isoformat(),
        )

    @classmethod
    def create_envelope(
        cls,
        *,
        request: LayaDecisionRequest,
        allowed_choice: DecisionAction,
        status: DecisionStatus = DecisionStatus.VALIDATED,
        confidence: float | None = None,
        answer_confidence: float | None = 0.820,
        model_version: str = "typed-decisions",
        rationale: str = "policy validated proposal",
        latency_ms: int = 42,
    ) -> DecisionEnvelope:
        """Creates a domain decision envelope with explicit fields."""
        digest = compute_request_digest(
            execution_id=str(request.execution_id),
            phase=request.phase.value,
            context_version=request.context_version,
            policy_version=request.policy_version,
            question_version=request.question_version,
            eligible_choices=[c.value for c in request.policy_derived_eligible_choices],
        )
        return DecisionEnvelope(
            decision_id=request.decision_id or uuid4(),
            decision_type=request.decision_type,
            phase=request.phase,
            execution_id=request.execution_id,
            trip_id=request.trip_id,
            idempotency_key=request.idempotency_key,
            allowed_choice=allowed_choice,
            status=status,
            confidence=confidence,
            answer_confidence=answer_confidence,
            model_version=model_version,
            policy_version=request.policy_version,
            rationale=rationale,
            latency_ms=latency_ms,
            request_digest=digest,
            created_at=datetime.now(UTC).isoformat(),
        )

    async def query_decision(self, request: LayaDecisionRequest) -> DecisionEnvelope:
        """Executes mock decision routing against configured fixtures."""
        self.call_count += 1
        self.recorded_requests.append(request)

        if self._canned_envelope is not None:
            return self._canned_envelope

        # If exactly one eligible choice and not overridden, perform singleton bypass
        if len(request.policy_derived_eligible_choices) == 1:
            return self.create_singleton_envelope(request)

        # If canned wire response is supplied, parse and construct envelope
        if self._canned_wire_response is not None:
            answer = self._canned_wire_response.answers.get(SAMPLE_QUESTION_KEY)
            if answer is None:
                raise ValueError(f"Canned wire response missing answer for '{SAMPLE_QUESTION_KEY}'")
            choice_action = DecisionAction(answer.choice)
            ans_conf = resolve_answer_confidence(answer)
            model_ver = resolve_model_version(self._canned_wire_response)
            return self.create_envelope(
                request=request,
                allowed_choice=choice_action,
                status=self._default_status,
                confidence=answer.confidence,
                answer_confidence=ans_conf,
                model_version=model_ver,
                rationale="Canned wire response evaluated by fixture adapter",
                latency_ms=35,
            )

        # Default fallback / success behavior: pick first eligible choice
        allowed = request.policy_derived_eligible_choices[0]
        return self.create_envelope(
            request=request,
            allowed_choice=allowed,
            status=self._default_status,
            confidence=None,
            answer_confidence=0.820,
            model_version="typed-decisions",
            rationale="Default fixture decision",
            latency_ms=10,
        )

    async def check_health(self) -> bool:
        """Returns mock health status."""
        return self._health_status
