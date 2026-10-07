"""0002_add_artifacts

Revision ID: 0002_add_artifacts
Revises: 0001_initial_schema
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_add_artifacts"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trip_version", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column(
            "content_type", sa.String(length=100), nullable=False, server_default="application/pdf"
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("download_url", sa.String(length=1000), nullable=True),
        sa.Column(
            "meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key", name="uq_artifacts_object_key"),
    )
    op.create_index("ix_artifacts_trip_id", "artifacts", ["trip_id"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_trip_id", table_name="artifacts")
    op.drop_table("artifacts")
