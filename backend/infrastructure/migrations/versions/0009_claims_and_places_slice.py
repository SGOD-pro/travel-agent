"""0009_claims_and_places_slice
Conforming to spec 0002 Slice 2.

Note: Table existence guards (if table not in existing_tables) are retained for local
development flexibility, while relying on the linear Alembic revision chain for ordering.

Revision ID: 0009_claims_and_places_slice
Revises: 0008_source_observations_slice
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_claims_and_places_slice"
down_revision: str | None = "0008_source_observations_slice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1. canonical_places table
    if "canonical_places" not in existing_tables:
        op.create_table(
            "canonical_places",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "names",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("primary_name", sa.String(length=256), nullable=False),
            sa.Column("locality", sa.String(length=128), nullable=False),
            sa.Column(
                "point",
                geoalchemy2.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
                nullable=False,
            ),
            sa.Column(
                "access_points",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "provider_ids",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("geocoder_provenance", sa.String(length=64), nullable=True),
            sa.Column(
                "identity_claim_refs",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "access_status",
                sa.String(length=32),
                nullable=False,
                server_default="UNKNOWN",
            ),
            sa.Column(
                "is_candidate",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
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
        )
        op.create_index(
            "idx_canonical_places_point",
            "canonical_places",
            ["point"],
            postgresql_using="gist",
        )
        op.create_index(
            "ix_canonical_places_locality",
            "canonical_places",
            ["locality"],
        )
        op.create_index(
            "ix_canonical_places_primary_name",
            "canonical_places",
            ["primary_name"],
        )
        op.create_index(
            "ix_canonical_places_provider_ids",
            "canonical_places",
            ["provider_ids"],
            postgresql_using="gin",
        )
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_canonical_places_osm_id
            ON canonical_places ((provider_ids->>'osm_id'))
            WHERE (provider_ids->>'osm_id') IS NOT NULL;
            """
        )
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_canonical_places_google_place_id
            ON canonical_places ((provider_ids->>'google_place_id'))
            WHERE (provider_ids->>'google_place_id') IS NOT NULL;
            """
        )

    # 2. claims table
    if "claims" not in existing_tables:
        op.create_table(
            "claims",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "execution_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("executions.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("subject_id", sa.String(length=256), nullable=False),
            sa.Column(
                "place_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("canonical_places.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("category", sa.String(length=64), nullable=False),
            sa.Column(
                "typed_value",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
            ),
            sa.Column("verification", sa.String(length=32), nullable=False),
            sa.Column(
                "availability",
                sa.String(length=32),
                nullable=False,
                server_default="UNKNOWN",
            ),
            sa.Column(
                "freshness",
                sa.String(length=32),
                nullable=False,
                server_default="UNKNOWN",
            ),
            sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
            sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "geographic_scope",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            sa.Column(
                "policy_version",
                sa.String(length=32),
                nullable=False,
                server_default="1.0",
            ),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column(
                "contradictions",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "decision_history",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
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
        )
        op.create_index("ix_claims_execution_id", "claims", ["execution_id"])
        op.create_index("ix_claims_subject_id", "claims", ["subject_id"])
        op.create_index("ix_claims_category", "claims", ["category"])
        op.create_index("ix_claims_place_id", "claims", ["place_id"])
        op.create_index("ix_claims_verification", "claims", ["verification"])
        op.create_index("ix_claims_freshness", "claims", ["freshness"])
        op.create_index("ix_claims_subject_category", "claims", ["subject_id", "category"])

    # 3. claim_evidence table
    if "claim_evidence" not in existing_tables:
        op.create_table(
            "claim_evidence",
            sa.Column(
                "claim_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("claims.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "observation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("source_observations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("support_type", sa.String(length=32), nullable=False),
            sa.Column("locator", sa.String(length=256), nullable=False, server_default=""),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("claim_id", "observation_id", name="pk_claim_evidence"),
        )
        op.create_index(
            "ix_claim_evidence_observation_id",
            "claim_evidence",
            ["observation_id"],
        )

    # 4. Triggers maintaining updated_at timestamp on direct SQL updates
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at_timestamp()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_canonical_places_updated_at ON canonical_places;")
    op.execute(
        """
        CREATE TRIGGER trg_canonical_places_updated_at
        BEFORE UPDATE ON canonical_places
        FOR EACH ROW
        EXECUTE FUNCTION set_updated_at_timestamp();
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_claims_updated_at ON claims;")
    op.execute(
        """
        CREATE TRIGGER trg_claims_updated_at
        BEFORE UPDATE ON claims
        FOR EACH ROW
        EXECUTE FUNCTION set_updated_at_timestamp();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_claims_updated_at ON claims;")
    op.execute("DROP TRIGGER IF EXISTS trg_canonical_places_updated_at ON canonical_places;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at_timestamp();")

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "claim_evidence" in existing_tables:
        op.drop_table("claim_evidence")

    if "claims" in existing_tables:
        op.drop_table("claims")

    if "canonical_places" in existing_tables:
        op.drop_table("canonical_places")
