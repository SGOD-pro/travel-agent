"""add_event_type_to_completion_receipts

Revision ID: 628f8a83fe95
Revises: 0007_job_events_slice
Create Date: 2026-10-09 17:16:20.673174

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '628f8a83fe95'
down_revision: Union[str, Sequence[str], None] = '0007_job_events_slice'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('completion_receipts', sa.Column('event_type', sa.String(), nullable=False, server_default='TASK_COMPLETED'))

def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('completion_receipts', 'event_type')
    op.add_column('completion_receipts', sa.Column('event_type', sa.String(), nullable=False))
    op.create_index(op.f('ix_executions_owner_id'), 'executions', ['owner_id'], unique=False)
    op.create_index(op.f('ix_job_events_execution_id'), 'job_events', ['execution_id'], unique=False)
    op.alter_column('outbox_events', 'execution_id',
               existing_type=sa.UUID(),
               nullable=False)
    op.alter_column('outbox_events', 'kind',
               existing_type=sa.VARCHAR(length=64),
               nullable=False)
    op.alter_column('outbox_events', 'payload_ref',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               nullable=False)
    op.drop_index(op.f('ix_outbox_aggregate_id'), table_name='outbox_events')
    op.drop_index(op.f('ix_outbox_pending'), table_name='outbox_events', postgresql_where='(delivered_at IS NULL)')
    op.create_index('ix_outbox_events_due_pending', 'outbox_events', ['due_at'], unique=False, postgresql_where=sa.text('delivered_at IS NULL'))
    op.create_index(op.f('ix_tasks_execution_id'), 'tasks', ['execution_id'], unique=False)
    op.create_index(op.f('ix_worker_results_execution_id'), 'worker_results', ['execution_id'], unique=False)
    op.create_index(op.f('ix_worker_results_task_id'), 'worker_results', ['task_id'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('completion_receipts', 'event_type')
