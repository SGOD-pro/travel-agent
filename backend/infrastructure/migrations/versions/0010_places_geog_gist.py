"""0010_places_geog_gist

Adds functional GiST index on ((point)::geography) to support fast index scans
for ST_DWithin and ST_Distance queries in meters.

Revision ID: 0010_places_geog_gist
Revises: 0009_claims_and_places_slice
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_places_geog_gist"
down_revision: str | None = "0009_claims_and_places_slice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_canonical_places_point_geog "
            "ON canonical_places USING gist (((point)::geography));"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DROP INDEX IF EXISTS idx_canonical_places_point_geog;")
    )
