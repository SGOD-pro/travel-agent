"""0012_add_route_optimization

Adds canonical routes, route_stops, route_legs, and route_restrictions tables
with PostGIS spatial indexes and foreign keys conforming to spec 0005 Slice 2.

Revision ID: 0012_add_route_optimization
Revises: 0011_add_laya_decisions
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_add_route_optimization"
down_revision: str | None = "0011_add_laya_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1. routes table
    if "routes" not in existing_tables:
        op.create_table(
            "routes",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "execution_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("executions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "trip_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("trips.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("route_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("solver_status", sa.String(length=32), nullable=False),
            sa.Column("objective", sa.String(length=32), nullable=False),
            sa.Column("departure_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("return_deadline", sa.DateTime(timezone=True), nullable=True),
            sa.Column("actual_return_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("total_distance_m", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("total_travel_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("total_dwell_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("total_wait_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("total_elapsed_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("driving_distance_m", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("walking_distance_m", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("fuel_amount", sa.Numeric(precision=20, scale=6), nullable=True),
            sa.Column("fuel_currency", sa.String(length=3), nullable=False, server_default=sa.text("'INR'")),
            sa.Column("fuel_basis", sa.String(length=256), nullable=True),
            sa.Column(
                "warnings",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "assumptions",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("previous_route_version", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

        op.create_index("ix_routes_owner_id", "routes", ["owner_id"], unique=False)
        op.create_index("ix_routes_execution_id", "routes", ["execution_id"], unique=False)
        op.create_index("ix_routes_trip_id", "routes", ["trip_id"], unique=False)
        op.create_index(
            "uq_routes_trip_version",
            "routes",
            ["trip_id", "route_version"],
            unique=True,
            postgresql_where=sa.text("trip_id IS NOT NULL"),
        )

    # 2. route_stops table
    if "route_stops" not in existing_tables:
        op.create_table(
            "route_stops",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "route_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("routes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "place_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("canonical_places.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("stop_order", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=256), nullable=False),
            sa.Column(
                "location",
                geoalchemy2.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
                nullable=False,
            ),
            sa.Column("stop_type", sa.String(length=32), nullable=False),
            sa.Column("is_must_visit", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("is_completed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_skipped", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("skip_reason", sa.String(length=64), nullable=True),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dwell_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("planned_arrival_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("planned_departure_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("wait_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("route_id", "stop_order", name="uq_route_stops_route_order"),
        )

        op.create_index("ix_route_stops_route_id", "route_stops", ["route_id"], unique=False)
        op.create_index("ix_route_stops_place_id", "route_stops", ["place_id"], unique=False)
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS idx_route_stops_location "
                "ON route_stops USING gist (location);"
            )
        )
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS idx_route_stops_location_geog "
                "ON route_stops USING gist (((location)::geography));"
            )
        )

    # 3. route_legs table
    if "route_legs" not in existing_tables:
        op.create_table(
            "route_legs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "route_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("routes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("leg_order", sa.Integer(), nullable=False),
            sa.Column(
                "from_stop_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("route_stops.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "to_stop_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("route_stops.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("transport_mode", sa.String(length=32), nullable=False),
            sa.Column("departure_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("arrival_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("distance_m", sa.Integer(), nullable=False),
            sa.Column("travel_seconds", sa.Integer(), nullable=False),
            sa.Column("geometry", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column(
                "parking_hub_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("route_stops.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("traffic_basis", sa.String(length=32), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False, server_default=sa.text("'valhalla'")),
            sa.Column("provider_version", sa.String(length=64), nullable=True),
            sa.Column("data_version", sa.String(length=64), nullable=True),
            sa.Column(
                "turn_instructions",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("route_id", "leg_order", name="uq_route_legs_route_order"),
        )

        op.create_index("ix_route_legs_route_id", "route_legs", ["route_id"], unique=False)
        op.create_index("ix_route_legs_from_stop_id", "route_legs", ["from_stop_id"], unique=False)
        op.create_index("ix_route_legs_to_stop_id", "route_legs", ["to_stop_id"], unique=False)
        op.create_index("ix_route_legs_parking_hub_id", "route_legs", ["parking_hub_id"], unique=False)

    # 4. route_restrictions table
    if "route_restrictions" not in existing_tables:
        op.create_table(
            "route_restrictions",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "route_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("routes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("restriction_type", sa.String(length=32), nullable=False),
            sa.Column(
                "geometry",
                geoalchemy2.Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
                nullable=False,
            ),
            sa.Column(
                "allowed_modes",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("effective_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("effective_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "claim_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("claims.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("was_applied", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("unresolved_reason", sa.String(length=256), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

        op.create_index("ix_route_restrictions_route_id", "route_restrictions", ["route_id"], unique=False)
        op.create_index("ix_route_restrictions_claim_id", "route_restrictions", ["claim_id"], unique=False)
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS idx_route_restrictions_geometry "
                "ON route_restrictions USING gist (geometry);"
            )
        )


def downgrade() -> None:
    # 1. Drop route_restrictions
    op.execute(sa.text("DROP INDEX IF EXISTS idx_route_restrictions_geometry;"))
    op.drop_index("ix_route_restrictions_claim_id", table_name="route_restrictions")
    op.drop_index("ix_route_restrictions_route_id", table_name="route_restrictions")
    op.drop_table("route_restrictions")

    # 2. Drop route_legs
    op.drop_index("ix_route_legs_parking_hub_id", table_name="route_legs")
    op.drop_index("ix_route_legs_to_stop_id", table_name="route_legs")
    op.drop_index("ix_route_legs_from_stop_id", table_name="route_legs")
    op.drop_index("ix_route_legs_route_id", table_name="route_legs")
    op.drop_table("route_legs")

    # 3. Drop route_stops
    op.execute(sa.text("DROP INDEX IF EXISTS idx_route_stops_location_geog;"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_route_stops_location;"))
    op.drop_index("ix_route_stops_place_id", table_name="route_stops")
    op.drop_index("ix_route_stops_route_id", table_name="route_stops")
    op.drop_table("route_stops")

    # 4. Drop routes
    op.drop_index("uq_routes_trip_version", table_name="routes")
    op.drop_index("ix_routes_trip_id", table_name="routes")
    op.drop_index("ix_routes_execution_id", table_name="routes")
    op.drop_index("ix_routes_owner_id", table_name="routes")
    op.drop_table("routes")
