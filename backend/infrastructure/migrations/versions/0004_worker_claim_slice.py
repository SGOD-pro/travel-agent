"""0004_worker_claim_slice

Revision ID: 0004_worker_claim_slice
Revises: 0003_job_acceptance_slice
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_worker_claim_slice"
down_revision: str | None = "0003_job_acceptance_slice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1. tasks table
    if "tasks" not in existing_tables:
        op.create_table(
            "tasks",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "execution_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("executions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "parent_task_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("worker", sa.String(length=32), nullable=False),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("payload_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("context_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
            sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("lease_expiry", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=True),
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
            sa.UniqueConstraint(
                "execution_id", "idempotency_key", name="uq_tasks_execution_idempotency"
            ),
        )
        op.create_index("ix_tasks_execution_status", "tasks", ["execution_id", "status"])
        op.create_index(
            "ix_tasks_active_lease_expiry",
            "tasks",
            ["status", "lease_expiry"],
            postgresql_where=sa.text("status IN ('PENDING', 'CLAIMED', 'RUNNING')"),
        )

    # 2. worker_results table
    if "worker_results" not in existing_tables:
        op.create_table(
            "worker_results",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
            sa.Column("context_version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("output_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column(
                "warnings",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default="[]",
            ),
            sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column(
                "usage",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default="{}",
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("task_id", "attempt", name="uq_worker_results_task_attempt"),
        )
        op.create_index(
            "ix_worker_results_execution_task", "worker_results", ["execution_id", "task_id"]
        )

        # 3. Add foreign key from tasks.result_id to worker_results.id
        op.create_foreign_key(
            "fk_tasks_result_id_worker_results",
            "tasks",
            "worker_results",
            ["result_id"],
            ["id"],
            ondelete="SET NULL",
            use_alter=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "tasks" in existing_tables and "worker_results" in existing_tables:
        try:
            op.drop_constraint("fk_tasks_result_id_worker_results", "tasks", type_="foreignkey")
        except Exception:
            pass

    if "worker_results" in existing_tables:
        op.drop_table("worker_results")

    if "tasks" in existing_tables:
        op.drop_table("tasks")
