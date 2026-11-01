"""0015_trip_route_itinerary

Adds current_route_id and itinerary_json columns to table trips conforming to spec 0007 Slice 5.

Revision ID: 0015_trip_route_itinerary
Revises: 0014_enforce_single_child_replan
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_trip_route_itinerary"
down_revision: str | None = "0014_enforce_single_child_replan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = [c["name"] for c in inspector.get_columns("trips")]

    if "current_route_id" not in cols:
        op.add_column(
            "trips",
            sa.Column(
                "current_route_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("routes.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )

    indexes = [ix["name"] for ix in inspector.get_indexes("trips")]
    if "ix_trips_current_route_id" not in indexes:
        op.create_index("ix_trips_current_route_id", "trips", ["current_route_id"], unique=False)

    if "itinerary_json" not in cols:
        op.add_column(
            "trips",
            sa.Column(
                "itinerary_json",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = [ix["name"] for ix in inspector.get_indexes("trips")]
    if "ix_trips_current_route_id" in indexes:
        op.drop_index("ix_trips_current_route_id", table_name="trips")

    cols = [c["name"] for c in inspector.get_columns("trips")]
    if "current_route_id" in cols:
        op.drop_column("trips", "current_route_id")

    if "itinerary_json" in cols:
        op.drop_column("trips", "itinerary_json")
