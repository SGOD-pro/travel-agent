"""Domain models for Human in the Loop (HITL) traveler questions and answers.

Enforces:
- Stable question identity, expected context version, and typed validation rules.
- Question lifecycle states (PENDING, ANSWERED, EXPIRED, CANCELLED, SUPERSEDED).
- Caller ownership and replay-safe answer submission.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class QuestionStatus(StrEnum):
    PENDING = "PENDING"
    ANSWERED = "ANSWERED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class QuestionType(StrEnum):
    DATE_RANGE = "DATE_RANGE"
    CHOICE = "CHOICE"
    TEXT = "TEXT"


class TravelerQuestion(BaseModel):
    """Canonical question presented to traveler when planning constraint is missing."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    execution_id: uuid.UUID
    trip_id: uuid.UUID | None = None
    owner_id: uuid.UUID
    context_version: int = Field(default=1, ge=1)
    question_key: str
    question_type: QuestionType
    prompt: str
    validation_rules: dict[str, Any] = Field(default_factory=dict)
    status: QuestionStatus = Field(default=QuestionStatus.PENDING)
    answer_json: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    answered_at: datetime | None = None


class QuestionAnswerRequest(BaseModel):
    """Payload submitted to answer a pending question."""

    model_config = ConfigDict(frozen=True)

    question_id: uuid.UUID
    answer: dict[str, Any]
    expected_context_version: int | None = None


class AnswerSubmissionResult(BaseModel):
    """Result of processing an answer submission."""

    model_config = ConfigDict(frozen=True)

    question_id: uuid.UUID
    execution_id: uuid.UUID
    status: str
    new_context_version: int
    context_version: int | None = None
    is_duplicate: bool = False
    message: str | None = None
