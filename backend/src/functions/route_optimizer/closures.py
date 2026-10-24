"""Hard closure avoidance and post-routing spatial restriction revalidation.

Conforms strictly to specification 0005 AC-3:
- Ingests verified road closure claims from specification 0002.
- Enforces strict prohibition over penalties via Valhalla exclude_polygons.
- Revalidates final leg geometries against active restriction intervals.
- Enforces temporal applicability (overlap of leg traversal and restriction active window).
- Treats unsupported restrictions as explicit and fails closed safely with INFEASIBLE.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import shape

from contracts.routing import (
    OptimizedRoute,
    RestrictionType,
    RouteLegOutput,
    RouteRestrictionOutput,
    RouteStatus,
    SolverStatus,
)

logger = logging.getLogger(__name__)

_UNSUPPORTED_PREFIX = "UNSUPPORTED_RESTRICTION"


class UnsupportedRestrictionError(Exception):
    """Raised when a road restriction cannot be converted to a topological spatial exclusion."""


class RoadClosureIntersectionError(Exception):
    """Raised when a route leg intersects an active verified road closure."""


class RestrictionClaim(BaseModel):
    """Verified road closure or spatial hazard claim representation."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    claim_id: UUID | None = None
    restriction_type: RestrictionType = RestrictionType.ROAD_CLOSURE
    geometry: dict[str, Any]
    allowed_modes: list[str] = Field(default_factory=list)
    effective_start: datetime | None = None
    effective_end: datetime | None = None
    is_active: bool = True
    is_verified: bool = True
    description: str | None = None


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensures datetime is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def check_temporal_overlap(
    traversal_start: datetime,
    traversal_end: datetime,
    restriction_start: datetime | None,
    restriction_end: datetime | None,
) -> bool:
    """Checks whether the planned traversal window overlaps the restriction active period.

    If start or end is None, the restriction is treated as unbounded on that boundary.
    """
    t_start = _ensure_utc(traversal_start)
    t_end = _ensure_utc(traversal_end)
    r_start = _ensure_utc(restriction_start)
    r_end = _ensure_utc(restriction_end)

    if t_start is None or t_end is None:
        return True

    # Check non-overlap conditions:
    # 1. Traversal ends before restriction starts
    if r_start is not None and t_end < r_start:
        return False
    # 2. Traversal starts after restriction ends
    if r_end is not None and t_start > r_end:
        return False

    return True


class ClosureAvoidanceService:
    """Manages Valhalla exclude_polygons extraction and post-routing spatial revalidation."""

    @staticmethod
    def extract_valhalla_exclude_polygons(
        restrictions: list[RestrictionClaim],
    ) -> tuple[list[list[list[float]]], list[RouteRestrictionOutput], list[str]]:
        """Converts active verified road closure geometries into Valhalla exclude_polygons.

        Returns:
            (valhalla_exclude_polygons, route_restrictions_records, diagnostics)
        """
        exclude_polygons: list[list[list[float]]] = []
        applied_records: list[RouteRestrictionOutput] = []
        diagnostics: list[str] = []

        for restr in restrictions:
            if not restr.is_active or not restr.is_verified:
                continue

            if restr.restriction_type != RestrictionType.ROAD_CLOSURE:
                # Non-closure restrictions (e.g. general advisory warnings)
                applied_records.append(
                    RouteRestrictionOutput(
                        id=restr.id,
                        claim_id=restr.claim_id,
                        restriction_type=restr.restriction_type,
                        geometry=restr.geometry,
                        allowed_modes=restr.allowed_modes,
                        effective_start=restr.effective_start,
                        effective_end=restr.effective_end,
                        was_applied=False,
                        unresolved_reason="Non-blocking restriction type does not require topological exclusion",
                    )
                )
                continue

            # Extract polygon ring coordinates for Valhalla
            geom = restr.geometry
            geom_type = geom.get("type", "")

            try:
                if geom_type == "Polygon":
                    coords = geom.get("coordinates", [])
                    if not coords or len(coords[0]) < 3:
                        raise ValueError("Polygon has fewer than 3 coordinates")
                    # Valhalla outer ring [[lon, lat], [lon, lat], ...]
                    outer_ring = [[float(p[0]), float(p[1])] for p in coords[0]]
                    exclude_polygons.append(outer_ring)
                    applied_records.append(
                        RouteRestrictionOutput(
                            id=restr.id,
                            claim_id=restr.claim_id,
                            restriction_type=restr.restriction_type,
                            geometry=restr.geometry,
                            allowed_modes=restr.allowed_modes,
                            effective_start=restr.effective_start,
                            effective_end=restr.effective_end,
                            was_applied=True,
                        )
                    )

                elif geom_type == "MultiPolygon":
                    coords = geom.get("coordinates", [])
                    for poly_coords in coords:
                        if poly_coords and len(poly_coords[0]) >= 3:
                            outer_ring = [[float(p[0]), float(p[1])] for p in poly_coords[0]]
                            exclude_polygons.append(outer_ring)
                    applied_records.append(
                        RouteRestrictionOutput(
                            id=restr.id,
                            claim_id=restr.claim_id,
                            restriction_type=restr.restriction_type,
                            geometry=restr.geometry,
                            allowed_modes=restr.allowed_modes,
                            effective_start=restr.effective_start,
                            effective_end=restr.effective_end,
                            was_applied=True,
                        )
                    )

                elif geom_type == "LineString":
                    # Buffer line string by ~30 meters (0.0003 degrees) to create an exclusion polygon
                    ls = shape(geom)
                    poly = ls.buffer(0.0003)
                    buffered_coords = [[float(c[0]), float(c[1])] for c in poly.exterior.coords]
                    exclude_polygons.append(buffered_coords)
                    applied_records.append(
                        RouteRestrictionOutput(
                            id=restr.id,
                            claim_id=restr.claim_id,
                            restriction_type=restr.restriction_type,
                            geometry=restr.geometry,
                            allowed_modes=restr.allowed_modes,
                            effective_start=restr.effective_start,
                            effective_end=restr.effective_end,
                            was_applied=True,
                        )
                    )

                else:
                    # Unsupported geometry (e.g. Point or non-standard collection)
                    reason = f"{_UNSUPPORTED_PREFIX}: Geometry type '{geom_type}' cannot be converted to an exclude polygon"
                    logger.warning(reason)
                    diagnostics.append(reason)
                    applied_records.append(
                        RouteRestrictionOutput(
                            id=restr.id,
                            claim_id=restr.claim_id,
                            restriction_type=restr.restriction_type,
                            geometry=restr.geometry,
                            allowed_modes=restr.allowed_modes,
                            effective_start=restr.effective_start,
                            effective_end=restr.effective_end,
                            was_applied=False,
                            unresolved_reason=reason,
                        )
                    )

            except Exception as exc:
                reason = f"{_UNSUPPORTED_PREFIX}: Failed to process restriction {restr.id}: {exc}"
                logger.error(reason)
                diagnostics.append(reason)
                applied_records.append(
                    RouteRestrictionOutput(
                        id=restr.id,
                        claim_id=restr.claim_id,
                        restriction_type=restr.restriction_type,
                        geometry=restr.geometry,
                        allowed_modes=restr.allowed_modes,
                        effective_start=restr.effective_start,
                        effective_end=restr.effective_end,
                        was_applied=False,
                        unresolved_reason=reason,
                    )
                )

        return exclude_polygons, applied_records, diagnostics

    @staticmethod
    def revalidate_leg_geometries(
        legs: list[RouteLegOutput],
        restrictions: list[RestrictionClaim],
    ) -> list[str]:
        """Performs post-routing spatial intersection check between legs and active closures.

        Conforms strictly to AC-3:
        Checks spatial intersection only if planned traversal interval overlaps active restriction period.
        """
        violations: list[str] = []

        for leg in legs:
            leg_coords = leg.geometry.get("coordinates", [])
            if not leg_coords or len(leg_coords) < 2:
                continue

            try:
                leg_shape = shape(leg.geometry)
            except Exception as exc:
                logger.error("Failed to parse leg geometry for leg %d: %s", leg.leg_order, exc)
                continue

            for restr in restrictions:
                if not restr.is_active or not restr.is_verified:
                    continue
                if restr.restriction_type != RestrictionType.ROAD_CLOSURE:
                    continue

                # 1. Temporal applicability check
                overlaps = check_temporal_overlap(
                    leg.departure_at,
                    leg.arrival_at,
                    restr.effective_start,
                    restr.effective_end,
                )
                if not overlaps:
                    continue

                # 2. Spatial intersection check
                try:
                    restr_shape = shape(restr.geometry)
                    if leg_shape.intersects(restr_shape):
                        msg = (
                            f"ROAD_CLOSURE_INTERSECTION: Route leg {leg.leg_order} "
                            f"('{leg.from_stop_name}' -> '{leg.to_stop_name}') intersects "
                            f"active verified road closure {restr.claim_id or restr.id} "
                            f"during active interval [{restr.effective_start} to {restr.effective_end}]"
                        )
                        logger.error(msg)
                        violations.append(msg)
                except Exception as exc:
                    logger.error("Error evaluating intersection with restriction %s: %s", restr.id, exc)

        return violations

    @classmethod
    def apply_and_revalidate(
        cls,
        route: OptimizedRoute,
        restrictions: list[RestrictionClaim],
    ) -> OptimizedRoute:
        """Applies restriction records and fails closed safely if unsupported restrictions or intersections occur."""
        _, restriction_records, unapplied_diagnostics = cls.extract_valhalla_exclude_polygons(
            restrictions
        )

        # 1. Check for unsupported restrictions (fail closed)
        if unapplied_diagnostics:
            return route.model_copy(
                update={
                    "status": RouteStatus.INFEASIBLE,
                    "solver_status": SolverStatus.INFEASIBLE,
                    "restrictions": restriction_records,
                    "diagnostics": route.diagnostics + unapplied_diagnostics,
                    "warnings": route.warnings + ["Route failed closed due to unsupported restriction formats"],
                }
            )

        # 2. Check for spatial intersections with active road closures
        intersections = cls.revalidate_leg_geometries(route.legs, restrictions)
        if intersections:
            return route.model_copy(
                update={
                    "status": RouteStatus.INFEASIBLE,
                    "solver_status": SolverStatus.INFEASIBLE,
                    "restrictions": restriction_records,
                    "diagnostics": route.diagnostics + intersections,
                    "warnings": route.warnings + ["Route intersects active verified road closure"],
                }
            )

        # Route passed revalidation cleanly
        return route.model_copy(update={"restrictions": restriction_records})
