"""GeoJSON assembler service conforming to spec 0006 AC-5.

Assembles standards-compliant RFC 7946 GeoJSON FeatureCollections from:
1. PostGIS stop Points (route_stops.location)
2. JSONB leg geometries (route_legs.geometry)
3. Applied road closure restrictions (route_restrictions.geometry)
with simplestyle presentation styling.
"""

from __future__ import annotations

import logging
from typing import Any

from contracts.routing import (
    GeoJsonFeature,
    RouteMapGeoJsonResponse,
    RouteStatus,
    SolverStatus,
)
from functions.orchestrator.routing.models import RouteModel

logger = logging.getLogger(__name__)


def assemble_route_map_geojson(route: RouteModel) -> RouteMapGeoJsonResponse:
    """Assembles a standards-compliant GeoJSON FeatureCollection from a persistent RouteModel entity."""
    features: list[GeoJsonFeature] = []

    # Determine detour provenance
    has_applied_closure = any(r.was_applied for r in (route.restrictions or []))

    all_lons: list[float] = []
    all_lats: list[float] = []

    # 1. Stop Point Features
    for stop in route.stops or []:
        pt = stop.extract_point()
        lon = float(pt.longitude)
        lat = float(pt.latitude)
        all_lons.append(lon)
        all_lats.append(lat)

        is_infeasible_or_skipped = (
            stop.is_skipped
            or (route.status == RouteStatus.INFEASIBLE.value and not stop.is_completed)
        )

        if is_infeasible_or_skipped:
            marker_color = "#dc2626"  # red for skipped or infeasible
        elif stop.stop_type == "PARKING_HUB":
            marker_color = "#d97706"  # amber for parking hub
        elif stop.stop_type in ("ORIGIN", "RETURN_ORIGIN"):
            marker_color = "#2563eb"  # blue for origin/return
        else:
            marker_color = "#059669"  # green for visited venue

        marker_symbol = "P" if stop.stop_type == "PARKING_HUB" else str(stop.stop_order)

        fallback_infeasible_reason = (
            ((route.assumptions or [None])[0] or "MANDATORY_CONSTRAINT_INFEASIBLE")
            if (route.status == RouteStatus.INFEASIBLE.value and not stop.is_completed)
            else None
        )
        effective_skip_reason = (
            (stop.skip_reason or fallback_infeasible_reason)
            if is_infeasible_or_skipped
            else None
        )

        stop_props: dict[str, Any] = {
            "feature_type": "stop",
            "id": str(stop.id),
            "stop_order": stop.stop_order,
            "name": stop.name,
            "title": stop.name,
            "stop_name": stop.name,
            "stop_type": stop.stop_type,
            "planned_arrival_at": (
                stop.planned_arrival_at.isoformat() if stop.planned_arrival_at else None
            ),
            "planned_departure_at": (
                stop.planned_departure_at.isoformat() if stop.planned_departure_at else None
            ),
            "dwell_seconds": stop.dwell_seconds,
            "wait_seconds": stop.wait_seconds,
            "is_completed": bool(stop.is_completed),
            "is_skipped": is_infeasible_or_skipped,
            "skip_reason": effective_skip_reason,
            "marker-color": marker_color,
            "marker-symbol": marker_symbol,
        }

        stop_geom: dict[str, Any] = {
            "type": "Point",
            "coordinates": [lon, lat],
        }

        features.append(
            GeoJsonFeature(
                type="Feature",
                geometry=stop_geom,
                properties=stop_props,
            )
        )

    # 2. Leg LineString Features
    for leg in route.legs or []:
        mode_str = str(leg.transport_mode).upper()
        is_walking = mode_str in ("WALK", "WALKING")

        if is_walking:
            stroke = "#10b981"  # emerald for walking
        else:
            stroke = "#3b82f6"  # blue for driving

        stroke_width = 3 if is_walking else 4
        stroke_dasharray = "5, 5" if is_walking else "none"

        leg_props: dict[str, Any] = {
            "feature_type": "leg",
            "leg_order": leg.leg_order,
            "from_stop_id": str(leg.from_stop_id),
            "to_stop_id": str(leg.to_stop_id),
            "transport_mode": leg.transport_mode,
            "mode": leg.transport_mode,
            "distance_m": leg.distance_m,
            "travel_seconds": leg.travel_seconds,
            "traffic_basis": leg.traffic_basis,
            "stroke": stroke,
            "stroke-width": stroke_width,
            "stroke-dasharray": stroke_dasharray,
        }

        if isinstance(leg.geometry, dict):
            leg_geom = leg.geometry
        else:
            logger.debug(
                "Non-dictionary leg geometry on leg %s from %s to %s: %r. Falling back to empty LineString.",
                leg.leg_order,
                leg.from_stop_id,
                leg.to_stop_id,
                leg.geometry,
            )
            leg_geom = {"type": "LineString", "coordinates": []}

        coords = leg_geom.get("coordinates") if isinstance(leg_geom, dict) else None
        if isinstance(coords, list):
            for c in coords:
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    all_lons.append(float(c[0]))
                    all_lats.append(float(c[1]))

        features.append(
            GeoJsonFeature(
                type="Feature",
                geometry=leg_geom,
                properties=leg_props,
            )
        )

    # 3. Restriction Polygon Features
    for restr in route.restrictions or []:
        if restr.was_applied:
            restr_geom = restr.extract_geometry_dict()
            restr_props: dict[str, Any] = {
                "feature_type": "restriction",
                "restriction_type": restr.restriction_type,
                "fill": "#ef4444",
                "fill-opacity": 0.35,
                "stroke": "#b91c1c",
                "description": restr.unresolved_reason or "Applied Road Closure Restriction",
            }
            features.append(
                GeoJsonFeature(
                    type="Feature",
                    geometry=restr_geom,
                    properties=restr_props,
                )
            )

    bbox = None
    if all_lons and all_lats:
        bbox = [min(all_lons), min(all_lats), max(all_lons), max(all_lats)]

    diagnostics_list = list(route.assumptions or [])
    for s in (route.stops or []):
        if getattr(s, "is_skipped", False):
            reason_text = f": {s.skip_reason}" if getattr(s, "skip_reason", None) else ""
            dropped_msg = f"Optional stop '{s.name}' dropped{reason_text}"
            if dropped_msg not in diagnostics_list:
                diagnostics_list.append(dropped_msg)
    if route.warnings:
        for w in route.warnings:
            if w not in diagnostics_list:
                diagnostics_list.append(w)


    top_level_props: dict[str, Any] = {
        "route_id": str(route.id),
        "route_version": route.route_version,
        "parent_route_id": str(route.parent_route_id) if route.parent_route_id else None,
        "status": route.status,
        "solver_status": route.solver_status,
        "total_distance_m": route.total_distance_m,
        "total_travel_seconds": route.total_travel_seconds,
        "fuel_cost": str(route.fuel_amount) if route.fuel_amount is not None else None,
        "warnings": list(route.warnings or []),
        "diagnostics": diagnostics_list,
    }

    return RouteMapGeoJsonResponse(
        type="FeatureCollection",
        bbox=bbox,
        features=features,
        properties=top_level_props,
        route_id=route.id,
        route_version=route.route_version,
        parent_route_id=route.parent_route_id,
        status=RouteStatus(route.status),
        solver_status=SolverStatus(route.solver_status),
        total_distance_m=route.total_distance_m,
        total_travel_seconds=route.total_travel_seconds,
        fuel_cost=str(route.fuel_amount) if route.fuel_amount is not None else None,
        warnings=list(route.warnings or []),
        diagnostics=diagnostics_list,
    )
