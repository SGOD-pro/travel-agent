"""0006_completion_receipts_slice

Revision ID: 0006_completion_receipts_slice
Revises: 0005_outbox_relay_slice
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_completion_receipts_slice"
down_revision: str | None = "0005_outbox_relay_slice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "completion_receipts" not in existing_tables:
        op.create_table(
            "completion_receipts",
            sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "task_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("tasks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "execution_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("executions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column(
                "result_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("worker_results.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "applied_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_completion_receipts_task_result",
            "completion_receipts",
            ["task_id", "result_id"],
        )
        op.create_index(
            "ix_completion_receipts_execution_id",
            "completion_receipts",
            ["execution_id"],
        )
        op.create_index(
            "ix_completion_receipts_unapplied",
            "completion_receipts",
            ["received_at"],
            postgresql_where=sa.text("applied_at IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "completion_receipts" in existing_tables:
        op.drop_table("completion_receipts")
