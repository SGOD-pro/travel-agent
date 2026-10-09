"""0008_source_observations_slice
Conforming to spec 0002 Slice 1.

Revision ID: 0008_source_observations_slice
Revises: 628f8a83fe95
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_source_observations_slice"
down_revision: str | None = "628f8a83fe95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "source_observations" not in existing_tables:
        op.create_table(
            "source_observations",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "execution_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("executions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column("canonical_url", sa.Text(), nullable=False),
            sa.Column("publisher", sa.String(length=128), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "artifact_ref",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            sa.Column("excerpt_text", sa.Text(), nullable=False),
            sa.Column("excerpt_locator", sa.String(length=256), nullable=False),
            sa.Column("extraction_method", sa.String(length=32), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "execution_id",
                "canonical_url",
                "content_hash",
                name="uq_source_observations_snapshot",
            ),
        )
        op.create_index(
            "ix_source_observations_execution_id",
            "source_observations",
            ["execution_id"],
        )
        op.create_index(
            "ix_source_observations_canonical_url",
            "source_observations",
            ["canonical_url"],
        )
        op.create_index(
            "ix_source_observations_content_hash",
            "source_observations",
            ["content_hash"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "source_observations" in existing_tables:
        op.drop_table("source_observations")
