"""SQLAlchemy ORM models for canonical places conforming to spec 0002."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from config.base_model import Base


class CanonicalPlaceModel(Base):
    """Canonical place with PostGIS geometry coordinates and provider identity.

    Note: updated_at is maintained by ORM onupdate and enforced at database level
    via before update trigger trg_canonical_places_updated_at.
    """

    __tablename__ = "canonical_places"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    names: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    primary_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    locality: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # GiST index is created explicitly in __table_args__ below to manage naming and parameters
    point: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    access_points: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    provider_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
    )
    geocoder_provenance: Mapped[str | None] = mapped_column(String(64), nullable=True)
    identity_claim_refs: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    access_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="UNKNOWN",
        server_default="UNKNOWN",
    )
    is_candidate: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa.text("true"),
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

    __table_args__ = (
        Index("idx_canonical_places_point", "point", postgresql_using="gist"),
        Index(
            "idx_canonical_places_point_geog",
            sa.text("((point)::geography)"),
            postgresql_using="gist",
        ),
        Index("ix_canonical_places_provider_ids", "provider_ids", postgresql_using="gin"),
        Index(
            "uq_canonical_places_osm_id",
            sa.text("(provider_ids->>'osm_id')"),
            unique=True,
            postgresql_where=sa.text("(provider_ids->>'osm_id') IS NOT NULL"),
        ),
        Index(
            "uq_canonical_places_google_place_id",
            sa.text("(provider_ids->>'google_place_id')"),
            unique=True,
            postgresql_where=sa.text("(provider_ids->>'google_place_id') IS NOT NULL"),
        ),
    )
