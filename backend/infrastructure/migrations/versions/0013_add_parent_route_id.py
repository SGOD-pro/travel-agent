"""0013_add_parent_route_id

Adds parent_route_id foreign key and index to table routes conforming to spec 0006 Slice 1 (AC-6).

Revision ID: 0013_add_parent_route_id
Revises: 0012_add_route_optimization
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_add_parent_route_id"
down_revision: str | None = "0012_add_route_optimization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = [c["name"] for c in inspector.get_columns("routes")]

    if "parent_route_id" not in cols:
        op.add_column(
            "routes",
            sa.Column(
                "parent_route_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("routes.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )

    indexes = [ix["name"] for ix in inspector.get_indexes("routes")]
    if "ix_routes_parent_route_id" not in indexes:
        op.create_index("ix_routes_parent_route_id", "routes", ["parent_route_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = [ix["name"] for ix in inspector.get_indexes("routes")]
    if "ix_routes_parent_route_id" in indexes:
        op.drop_index("ix_routes_parent_route_id", table_name="routes")

    cols = [c["name"] for c in inspector.get_columns("routes")]
    if "parent_route_id" in cols:
        op.drop_column("routes", "parent_route_id")
