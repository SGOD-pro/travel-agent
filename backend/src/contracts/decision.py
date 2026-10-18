"""Domain contracts for remote LAYA decision routing.

Conforms to spec 0004 Slice 1:
1. Strict separation of domain models from external upstream wire schemas.
2. Distinct and nullable confidence metrics:
   answer_confidence reflects top choice probability.
   confidence reflects model distribution entropy confidence (null when omitted).
3. Deterministic singleton decisions enforce confidence=null and answer_confidence=null;
   DETERMINISTIC_SINGLETON identifies their source.
4. Abstract port LayaDecisionPort defines async query_decision and check_health methods.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DecisionAction(StrEnum):
    """Allowed orchestration decision routing actions."""

    DISPATCH_EVIDENCE_SEARCH = "DISPATCH_EVIDENCE_SEARCH"
    DISPATCH_BROWSER_ESCALATION = "DISPATCH_BROWSER_ESCALATION"
    PROCEED_DETERMINISTIC_EVALUATION = "PROCEED_DETERMINISTIC_EVALUATION"


class DecisionType(StrEnum):
    """Domain categorization for decision requests."""

    EVIDENCE_ROUTING = "EVIDENCE_ROUTING"


class DecisionPhase(StrEnum):
    """Explicit workflow phases for routing decisions."""

    SEARCH_COMPLETION = "SEARCH_COMPLETION"
    BROWSER_ELIGIBILITY = "BROWSER_ELIGIBILITY"
    EVALUATION = "EVALUATION"


class DecisionStatus(StrEnum):
    """Normalized outcomes for decision routing execution."""

    VALIDATED = "VALIDATED"
    DETERMINISTIC_SINGLETON = "DETERMINISTIC_SINGLETON"
    REJECTED_BY_POLICY = "REJECTED_BY_POLICY"
    FALLBACK_TIMEOUT = "FALLBACK_TIMEOUT"
    FALLBACK_ERROR = "FALLBACK_ERROR"
    FALLBACK_LOW_CONFIDENCE = "FALLBACK_LOW_CONFIDENCE"
    FALLBACK_ABSTAINED = "FALLBACK_ABSTAINED"
    FALLBACK_TRUNCATED = "FALLBACK_TRUNCATED"
    FALLBACK_MALFORMED = "FALLBACK_MALFORMED"
    FALLBACK_UNCONFIGURED = "FALLBACK_UNCONFIGURED"


class LayaDecisionRequest(BaseModel):
    """Input payload dispatched to the LAYA decision router."""

    model_config = ConfigDict(frozen=True)

    decision_id: UUID = Field(default_factory=uuid4, description="Unique decision request identifier")
    decision_type: DecisionType = Field(
        default=DecisionType.EVIDENCE_ROUTING,
        description="Type of routing decision requested",
    )
    phase: DecisionPhase = Field(
        default=DecisionPhase.BROWSER_ELIGIBILITY,
        description="Current workflow execution phase",
    )
    execution_id: UUID = Field(..., description="Active trip execution identifier")
    trip_id: UUID | None = Field(default=None, description="Optional trip identifier")
    context_version: int = Field(default=1, ge=1, description="Optimistic concurrency context version")
    policy_version: str = Field(default="laya-policy-v1", description="Active policy version identifier")
    question_version: str = Field(default="evidence_routing:v1", description="Active question template version")
    policy_derived_eligible_choices: list[DecisionAction] = Field(
        ...,
        min_length=1,
        description="Prerequisite filtered choices eligible for selection",
    )
    budget_remaining: int = Field(ge=0, description="Remaining execution task budget")
    context: dict[str, Any] = Field(..., description="Sanitized contextual state for decision evaluation")
    idempotency_key: str = Field(..., min_length=1, description="Deterministic idempotency key for replay safety")


class DecisionEnvelope(BaseModel):
    """Normalized decision envelope output produced by decision routing."""

    model_config = ConfigDict(frozen=True)

    decision_id: UUID = Field(..., description="Unique decision identifier")
    decision_type: DecisionType = Field(..., description="Domain decision type")
    phase: DecisionPhase = Field(..., description="Workflow phase of the decision")
    execution_id: UUID = Field(..., description="Active execution identifier")
    trip_id: UUID | None = Field(default=None, description="Optional trip identifier")
    idempotency_key: str = Field(..., min_length=1, description="Replay prevention idempotency key")
    allowed_choice: DecisionAction = Field(..., description="Approved decision routing action")
    status: DecisionStatus = Field(..., description="Outcome classification status")
    confidence: float | None = Field(
        default=None,
        description="Model distribution entropy confidence; null when omitted or for deterministic singletons",
    )
    answer_confidence: float | None = Field(
        default=None,
        description="Probability assigned to the top choice; null for deterministic singletons",
    )
    model_version: str | None = Field(default=None, description="Model identifier or deterministic source")
    policy_version: str = Field(..., min_length=1, description="Evaluator policy version")
    rationale: str | None = Field(default=None, max_length=1000, description="Structured policy rationale")
    latency_ms: int = Field(..., ge=0, description="Elapsed routing latency in milliseconds")
    request_digest: str = Field(..., min_length=1, description="Canonical SHA256 digest of request inputs")
    created_at: str = Field(..., min_length=1, description="ISO 8601 creation timestamp string")

    @model_validator(mode="after")
    def validate_confidence_semantics(self) -> DecisionEnvelope:
        if self.status == DecisionStatus.DETERMINISTIC_SINGLETON:
            if self.confidence is not None or self.answer_confidence is not None:
                raise ValueError(
                    "Deterministic singleton decisions must have confidence=null and answer_confidence=null"
                )
        if self.confidence is not None:
            if not math.isfinite(self.confidence) or not (0.0 <= self.confidence <= 1.0):
                raise ValueError("confidence must be a finite float between 0.0 and 1.0")
        if self.answer_confidence is not None:
            if not math.isfinite(self.answer_confidence) or not (0.0 <= self.answer_confidence <= 1.0):
                raise ValueError("answer_confidence must be a finite float between 0.0 and 1.0")
        return self


class LayaDecisionPort(ABC):
    """Abstract port for remote LAYA decision routing and inference."""

    @abstractmethod
    async def query_decision(self, request: LayaDecisionRequest) -> DecisionEnvelope:
        """Translates domain request, executes inference or fallback, and returns normalized decision envelope."""
        ...

    @abstractmethod
    async def check_health(self) -> bool:
        """Performs health check against LAYA remote endpoint."""
        ...
