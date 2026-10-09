"""0003_job_acceptance_slice

Revision ID: 0003_job_acceptance_slice
Revises: 0002_add_artifacts
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_job_acceptance_slice"
down_revision: str | None = "0002_add_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1. api_idempotency table
    if "api_idempotency" not in existing_tables:
        op.create_table(
            "api_idempotency",
            sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("operation", sa.String(length=64), nullable=False),
            sa.Column("key", sa.String(length=128), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("owner_id", "operation", "key", name="pk_api_idempotency"),
        )
        op.create_index("ix_api_idempotency_expires_at", "api_idempotency", ["expires_at"])

    # 2. executions table
    if "executions" not in existing_tables:
        # Check if trips table exists for foreign key
        fk_constraints = []
        if "trips" in existing_tables:
            fk_constraints.append(
                sa.ForeignKeyConstraint(["trip_id"], ["trips.id"], ondelete="SET NULL")
            )

        op.create_table(
            "executions",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("workflow", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="CREATED"),
            sa.Column("context_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("checkpoint_id", sa.String(length=128), nullable=True),
            sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("budget", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("lease_expiry", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            *fk_constraints,
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_executions_owner_created", "executions", ["owner_id", "created_at"])
        op.create_index(
            "ix_executions_active_deadline",
            "executions",
            ["status", "deadline_at"],
            postgresql_where=sa.text("status IN ('CREATED', 'RUNNING', 'WAITING_TASKS')"),
        )

    # 3. outbox_events table
    if "outbox_events" not in existing_tables:
        op.create_table(
            "outbox_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("payload_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column(
                "due_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("lease_expiry", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_outbox_events_execution_id", "outbox_events", ["execution_id"])
        op.create_index(
            "ix_outbox_events_due_pending",
            "outbox_events",
            ["due_at"],
            postgresql_where=sa.text("delivered_at IS NULL"),
        )
    else:
        existing_columns = [c["name"] for c in inspector.get_columns("outbox_events")]
        if "aggregate_id" in existing_columns:
            op.alter_column("outbox_events", "aggregate_id", nullable=True)
        if "event_type" in existing_columns:
            op.alter_column("outbox_events", "event_type", nullable=True)
        if "payload" in existing_columns:
            op.alter_column("outbox_events", "payload", nullable=True)
        if "execution_id" not in existing_columns:
            op.add_column(
                "outbox_events",
                sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
            )
            op.create_index("ix_outbox_events_execution_id", "outbox_events", ["execution_id"])
        if "task_id" not in existing_columns:
            op.add_column(
                "outbox_events",
                sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
            )
        if "kind" not in existing_columns:
            op.add_column(
                "outbox_events",
                sa.Column("kind", sa.String(length=64), nullable=True),
            )
        if "payload_ref" not in existing_columns:
            op.add_column(
                "outbox_events",
                sa.Column(
                    "payload_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True
                ),
            )
        if "due_at" not in existing_columns:
            op.add_column(
                "outbox_events",
                sa.Column(
                    "due_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.text("now()"),
                ),
            )
        if "lease_token" not in existing_columns:
            op.add_column(
                "outbox_events",
                sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
            )
        if "lease_expiry" not in existing_columns:
            op.add_column(
                "outbox_events",
                sa.Column("lease_expiry", sa.DateTime(timezone=True), nullable=True),
            )


def downgrade() -> None:
    op.drop_table("api_idempotency")
    op.drop_table("executions")
