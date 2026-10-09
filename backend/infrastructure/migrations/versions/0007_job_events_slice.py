"""0007_job_events_slice

Revision ID: 0007_job_events_slice
Revises: 0006_completion_receipts_slice
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_job_events_slice"
down_revision: str | None = "0006_completion_receipts_slice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "job_events" not in existing_tables:
        op.create_table(
            "job_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "execution_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("executions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column(
                "safe_payload",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "timestamp",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint("execution_id", "sequence", name="uq_job_events_execution_sequence"),
        )
        op.create_index(
            "ix_job_events_execution_sequence",
            "job_events",
            ["execution_id", "sequence"],
        )
        op.create_index(
            "ix_job_events_cursor",
            "job_events",
            ["execution_id", "sequence", "timestamp"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "job_events" in existing_tables:
        op.drop_table("job_events")
