"""0014_enforce_single_child_replan

Enforces strict single-child replan concurrency policy for standalone and trip routes.
Adds unique partial index on routes(parent_route_id) where parent_route_id IS NOT NULL,
preventing competing child branches and enforcing linear version progression atomically.

Revision ID: 0014_enforce_single_child_replan
Revises: 0013_add_parent_route_id
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_enforce_single_child_replan"
down_revision: str | None = "0013_add_parent_route_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = [ix["name"] for ix in inspector.get_indexes("routes")]

    if "uq_routes_parent_route_id" not in indexes:
        op.create_index(
            "uq_routes_parent_route_id",
            "routes",
            ["parent_route_id"],
            unique=True,
            postgresql_where=sa.text("parent_route_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = [ix["name"] for ix in inspector.get_indexes("routes")]

    if "uq_routes_parent_route_id" in indexes:
        op.drop_index("uq_routes_parent_route_id", table_name="routes")
