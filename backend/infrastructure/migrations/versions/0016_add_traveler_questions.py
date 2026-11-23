"""0016_add_traveler_questions

Adds table traveler_questions for Human in the Loop (HITL) planning constraints and traveler answers.

Revision ID: 0016_add_traveler_questions
Revises: 0015_trip_route_itinerary
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_add_traveler_questions"
down_revision: str | None = "0015_trip_route_itinerary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "traveler_questions" not in tables:
        op.create_table(
            "traveler_questions",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "execution_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("executions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "trip_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("trips.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("context_version", sa.Integer(), nullable=False, default=1),
            sa.Column("question_key", sa.String(64), nullable=False),
            sa.Column("question_type", sa.String(32), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column(
                "validation_rules",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                default=dict,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("status", sa.String(32), nullable=False, default="PENDING"),
            sa.Column("answer_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        )

        op.create_index(
            "ix_traveler_questions_execution_id", "traveler_questions", ["execution_id"]
        )
        op.create_index("ix_traveler_questions_trip_id", "traveler_questions", ["trip_id"])
        op.create_index("ix_traveler_questions_owner_id", "traveler_questions", ["owner_id"])
        op.create_index("ix_traveler_questions_status", "traveler_questions", ["status"])
        op.create_index(
            "ix_traveler_questions_execution_status",
            "traveler_questions",
            ["execution_id", "status"],
        )
        op.create_index(
            "ix_traveler_questions_owner_status", "traveler_questions", ["owner_id", "status"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "traveler_questions" in tables:
        op.drop_table("traveler_questions")
