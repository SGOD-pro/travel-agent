"""Upstream wire schemas for LAYA SystemOne protocol.

Conforms to NandhaKishorM/laya release v0.4.9 POST /v1/systemone:
- Distinguishes external wire models from SWENA internal domain contracts.
- Handles strict mode responses where optional extended fields are omitted.
- Provides probability validation, argmax consistency verification, and
  deterministic missing field resolution.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LayaWireQuestion(BaseModel):
    """Question definition within upstream wire request payload."""

    model_config = ConfigDict(frozen=True)

    type: str = Field(default="choice", description="Question interaction type")
    instructions: str = Field(..., min_length=1, description="Guidance provided to the model")
    criteria: dict[str, str] = Field(
        ...,
        min_length=1,
        description="Allowed options mapped to option descriptions",
    )


class LayaWireRequest(BaseModel):
    """Wire request payload transmitted to POST /v1/systemone."""

    model_config = ConfigDict(frozen=True)

    state: dict[str, Any] = Field(..., description="Contextual execution state dictionary")
    questions: dict[str, LayaWireQuestion] = Field(..., min_length=1, description="Configured question schemas")
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0, description="Minimum confidence cutoff")
    model: str | None = Field(default=None, description="Requested checkpoint or routing key")


class LayaChoiceAnswer(BaseModel):
    """Answer payload for choice questions in upstream wire response."""

    model_config = ConfigDict(frozen=True)

    type: str = Field(default="choice", description="Answer type")
    choice: str = Field(..., min_length=1, description="Selected option key")
    probabilities: dict[str, float] = Field(default_factory=dict, description="Normalized probability distribution")
    answer_confidence: float | None = Field(
        default=None,
        description="Probability assigned to the top choice; derived from probabilities when omitted",
    )
    confidence: float | None = Field(
        default=None,
        description="Model distribution entropy confidence; preserved as null when omitted",
    )
    x_jev_confidence: float | None = Field(
        default=None,
        description="Calibrated multi choice confidence score from extended Jev",
    )
    abstention: str | None = Field(
        default=None,
        description="Model abstention outcome string, e.g. passed or abstained",
    )
    abstention_threshold: float | None = Field(
        default=None,
        description="Threshold applied by model server during inference",
    )
    low_confidence: bool | None = Field(
        default=None,
        description="Boolean flag indicating whether response fell below confidence cutoff",
    )


class LayaWireUsage(BaseModel):
    """Tokenization and inference resource consumption metadata."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    state_tokens: int | None = Field(default=None, ge=0)
    state_tokens_dropped: int | None = Field(default=None, ge=0)
    truncated: bool | None = Field(
        default=None,
        description="Tokenizer prompt truncation flag; null when omitted in strict mode",
    )


class LayaWireRouting(BaseModel):
    """Upstream model dispatch and routing metadata."""

    model_config = ConfigDict(frozen=True)

    model: str | None = Field(default=None, description="Actual model checkpoint executed")
    repo: str | None = Field(default=None, description="Source model repository")
    reason: str | None = Field(default=None, description="Routing rationale string")


class LayaWireResponse(BaseModel):
    """Wire response payload received from POST /v1/systemone."""

    model_config = ConfigDict(frozen=True)

    model: str | None = Field(default=None, description="Model identifier returned by server")
    answers: dict[str, LayaChoiceAnswer] = Field(default_factory=dict, description="Answers keyed by question ID")
    usage: LayaWireUsage | None = Field(default=None, description="Optional resource usage metrics")
    routing: LayaWireRouting | None = Field(default=None, description="Optional routing resolution details")


def validate_wire_probabilities(
    answer: LayaChoiceAnswer,
    allowed_choices: list[str] | set[str] | None = None,
) -> None:
    """Validates choice probabilities against spec 0004 mathematical and consistency invariants.

    Raises ValueError if:
    1. probabilities dictionary is empty.
    2. Any probability value is not a finite float between 0.0 and 1.0.
    3. Any key in probabilities is not in allowed_choices (when provided).
    4. Probabilities do not sum to 1.0 within 0.01 tolerance.
    5. The answer choice is missing from probabilities.
    6. The answer choice does not match the key with the maximum probability.
    7. answer_confidence is present and diverges from probabilities[choice] by more than 0.001.
    """
    if not answer.probabilities:
        raise ValueError("Probabilities dictionary is empty")

    if allowed_choices is not None:
        allowed_set = set(allowed_choices)
        invalid_keys = set(answer.probabilities.keys()) - allowed_set
        if invalid_keys:
            raise ValueError(f"Probabilities contain unoffered keys: {sorted(invalid_keys)}")

    if answer.choice not in answer.probabilities:
        raise ValueError(f"Selected choice '{answer.choice}' not present in probabilities")

    total_prob = 0.0
    for key, prob in answer.probabilities.items():
        if not math.isfinite(prob) or prob < 0.0 or prob > 1.0:
            raise ValueError(f"Probability for key '{key}' must be a finite float between 0.0 and 1.0; got {prob}")
        total_prob += prob

    if abs(total_prob - 1.0) > 0.01:
        raise ValueError(f"Probabilities must normalize to 1.0 within 0.01 tolerance; sum={total_prob:.4f}")

    max_key = max(answer.probabilities, key=lambda k: answer.probabilities[k])
    if answer.choice != max_key:
        raise ValueError(
            f"Selected choice '{answer.choice}' does not match argmax key '{max_key}' "
            f"with probability {answer.probabilities[max_key]}"
        )

    if answer.answer_confidence is not None:
        expected = answer.probabilities[answer.choice]
        if abs(answer.answer_confidence - expected) > 0.001:
            raise ValueError(
                f"answer_confidence {answer.answer_confidence} does not align with probabilities[{answer.choice}]={expected}"
            )


def resolve_answer_confidence(answer: LayaChoiceAnswer) -> float:
    """Deterministically resolves the top choice answer confidence.

    Uses answer.answer_confidence if present; otherwise derives it as probabilities.get(choice, 0.0).
    """
    if answer.answer_confidence is not None:
        return answer.answer_confidence
    return answer.probabilities.get(answer.choice, 0.0)


def resolve_model_version(response: LayaWireResponse) -> str:
    """Resolves model version identifier from response with fallback defaults."""
    if response.routing and response.routing.model:
        return response.routing.model
    if response.model:
        return response.model
    return "remote-laya-unknown"


def resolve_truncation_status(response: LayaWireResponse) -> str:
    """Returns 'truncated', 'not_truncated', or 'unknown' based on usage metadata."""
    if response.usage is not None and response.usage.truncated is not None:
        return "truncated" if response.usage.truncated else "not_truncated"
    return "unknown"
