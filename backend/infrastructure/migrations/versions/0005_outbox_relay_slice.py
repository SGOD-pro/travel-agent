"""0005_outbox_relay_slice

Revision ID: 0005_outbox_relay_slice
Revises: 0004_worker_claim_slice
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_outbox_relay_slice"
down_revision: str | None = "0004_worker_claim_slice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = [c["name"] for c in inspector.get_columns("outbox_events")]

    if "failed_at" not in existing_columns:
        op.add_column(
            "outbox_events",
            sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "last_error" not in existing_columns:
        op.add_column(
            "outbox_events",
            sa.Column("last_error", sa.Text(), nullable=True),
        )

    existing_indexes = [idx["name"] for idx in inspector.get_indexes("outbox_events")]
    if "ix_outbox_events_relay_pending" not in existing_indexes:
        op.create_index(
            "ix_outbox_events_relay_pending",
            "outbox_events",
            ["due_at", "lease_expiry"],
            postgresql_where=sa.text("delivered_at IS NULL AND failed_at IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_indexes = [idx["name"] for idx in inspector.get_indexes("outbox_events")]
    if "ix_outbox_events_relay_pending" in existing_indexes:
        op.drop_index("ix_outbox_events_relay_pending", table_name="outbox_events")

    existing_columns = [c["name"] for c in inspector.get_columns("outbox_events")]
    if "last_error" in existing_columns:
        op.drop_column("outbox_events", "last_error")
    if "failed_at" in existing_columns:
        op.drop_column("outbox_events", "failed_at")
