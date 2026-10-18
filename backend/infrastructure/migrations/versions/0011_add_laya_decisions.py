"""0011_add_laya_decisions

Adds canonical laya_decisions table for persisting and revalidating LAYA routing decisions.
Conforms to spec 0004 Slice 4.

Revision ID: 0011_add_laya_decisions
Revises: 0010_places_geog_gist
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_add_laya_decisions"
down_revision: str | None = "0010_places_geog_gist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "laya_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("context_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("decision_type", sa.String(length=64), nullable=False),
        sa.Column(
            "allowed_choices",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("chosen_action", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("answer_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("rationale", sa.String(length=1000), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("execution_id", "idempotency_key", name="uq_laya_execution_idempotency"),
    )

    op.create_index("ix_laya_decisions_execution_id", "laya_decisions", ["execution_id"], unique=False)
    op.create_index("ix_laya_decisions_decision_id", "laya_decisions", ["decision_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_laya_decisions_decision_id", table_name="laya_decisions")
    op.drop_index("ix_laya_decisions_execution_id", table_name="laya_decisions")
    op.drop_table("laya_decisions")
