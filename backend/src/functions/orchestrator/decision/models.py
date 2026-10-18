"""SQLAlchemy models for canonical LAYA decision records.

Conforms to spec 0004 Slice 4:
1. Table laya_decisions with execution foreign key, context_version, idempotency_key.
2. Distinct decimal columns for confidence and answer_confidence (Numeric(4, 3)).
3. Unique constraint on (execution_id, idempotency_key).
4. Conversion method to domain DecisionEnvelope.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from config.base_model import Base
from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionPhase,
    DecisionStatus,
    DecisionType,
)


class LayaDecisionModel(Base):
    """Canonical database model for persisted LAYA decision records."""

    __tablename__ = "laya_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    context_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=sa.text("1")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_choices: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    chosen_action: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    answer_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), nullable=True
    )
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rationale: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "execution_id", "idempotency_key", name="uq_laya_execution_idempotency"
        ),
        Index("ix_laya_decisions_execution_id", "execution_id"),
        Index("ix_laya_decisions_decision_id", "decision_id"),
    )

    def extract_phase(self) -> DecisionPhase:
        """Derive workflow phase from the idempotency key format."""
        parts = self.idempotency_key.split(":")
        for part in parts:
            try:
                return DecisionPhase(part)
            except ValueError:
                continue
        return DecisionPhase.BROWSER_ELIGIBILITY

    @property
    def phase(self) -> DecisionPhase:
        """Derive workflow phase as a model property."""
        return self.extract_phase()

    def to_envelope(self) -> DecisionEnvelope:
        """Converts database record to immutable domain DecisionEnvelope."""
        return DecisionEnvelope(
            decision_id=self.decision_id,
            decision_type=DecisionType(self.decision_type),
            phase=self.phase,
            execution_id=self.execution_id,
            trip_id=self.trip_id,
            idempotency_key=self.idempotency_key,
            allowed_choice=DecisionAction(self.chosen_action),
            status=DecisionStatus(self.status),
            confidence=float(self.confidence) if self.confidence is not None else None,
            answer_confidence=float(self.answer_confidence)
            if self.answer_confidence is not None
            else None,
            model_version=self.model_version,
            policy_version=self.policy_version,
            rationale=self.rationale,
            latency_ms=self.latency_ms,
            request_digest=self.request_digest,
            created_at=self.created_at.isoformat()
            if isinstance(self.created_at, datetime)
            else str(self.created_at),
        )
