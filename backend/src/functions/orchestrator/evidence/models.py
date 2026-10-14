"""SQLAlchemy ORM models for claims and claim evidence junction conforming to spec 0002."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from config.base_model import Base
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.places.models import CanonicalPlaceModel


class ClaimModel(Base):
    """Extracted claim asserting real world fact about a place or subject.

    Note: updated_at is maintained by ORM onupdate and enforced at database level
    via before update trigger trg_claims_updated_at.
    """

    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    subject_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    place_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("canonical_places.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    typed_value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    verification: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    availability: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="UNKNOWN",
        server_default="UNKNOWN",
    )
    freshness: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="UNKNOWN",
        server_default="UNKNOWN",
        index=True,
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    geographic_scope: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    policy_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="1.0",
        server_default="1.0",
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    contradictions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    decision_history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    place: Mapped[CanonicalPlaceModel | None] = relationship(
        "CanonicalPlaceModel",
        lazy="selectin",
    )
    evidence_links: Mapped[list[ClaimEvidenceModel]] = relationship(
        "ClaimEvidenceModel",
        back_populates="claim",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    observations: Mapped[list[SourceObservationModel]] = relationship(
        "SourceObservationModel",
        secondary="claim_evidence",
        primaryjoin="ClaimModel.id == ClaimEvidenceModel.claim_id",
        secondaryjoin="SourceObservationModel.id == ClaimEvidenceModel.observation_id",
        viewonly=True,
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_claims_subject_category", "subject_id", "category"),
    )


class ClaimEvidenceModel(Base):
    """Junction table linking claims to immutable source observations."""

    __tablename__ = "claim_evidence"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="CASCADE"),
        primary_key=True,
    )
    observation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_observations.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    support_type: Mapped[str] = mapped_column(String(32), nullable=False)
    locator: Mapped[str] = mapped_column(String(256), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    claim: Mapped[ClaimModel] = relationship(
        "ClaimModel",
        back_populates="evidence_links",
    )
    observation: Mapped[SourceObservationModel] = relationship(
        "SourceObservationModel",
        lazy="selectin",
    )
