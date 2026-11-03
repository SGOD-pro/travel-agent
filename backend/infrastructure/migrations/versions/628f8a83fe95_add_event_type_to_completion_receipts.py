"""add_event_type_to_completion_receipts

Revision ID: 628f8a83fe95
Revises: 0007_job_events_slice
Create Date: 2026-10-09 17:16:20.673174

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '628f8a83fe95'
down_revision: str | Sequence[str] | None = '0007_job_events_slice'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('completion_receipts', sa.Column('event_type', sa.String(), nullable=False, server_default='TASK_COMPLETED'))

def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('completion_receipts', 'event_type')
