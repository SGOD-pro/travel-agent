"""Spatial corridor validation and geodesic distance utilities for candidate places.

Conforms to spec 0007 AC-3:
- Validates candidate coordinate bounds and rejects malformed values.
- Evaluates minimum distance to travel corridor segments connecting origin and destinations.
- Discards candidate places farther than 100 kilometers from the corridor line.
- Invariant: A coordinate or corridor distance match alone proves neither existence nor operating hours.
"""

from __future__ import annotations

import math
from typing import Any

from contracts.candidate import CandidateCorridorResult, CorridorValidationStatus
from contracts.llm import PlaceProposal
from functions.orchestrator.places.schemas import CoordinatePoint

EARTH_RADIUS_KM: float = 6371.0
DEFAULT_MAX_CORRIDOR_DISTANCE_KM: float = 100.0


def validate_coordinates(lat: Any, lng: Any) -> tuple[bool, str | None]:
    """Validates that latitude and longitude are finite numbers within valid geographic ranges."""
    if lat is None or lng is None:
        return False, "Coordinates cannot be None"
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return (
            False,
            f"Coordinates must be numeric, got lat={type(lat).__name__}, lng={type(lng).__name__}",
        )
    if not math.isfinite(lat) or not math.isfinite(lng):
        return False, f"Coordinates must be finite numbers, got lat={lat}, lng={lng}"
    if lat < -90.0 or lat > 90.0:
        return False, f"Latitude {lat} out of bounds [-90.0, 90.0]"
    if lng < -180.0 or lng > 180.0:
        return False, f"Longitude {lng} out of bounds [-180.0, 180.0]"
    return True, None


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes great circle distance between two points on Earth in kilometers using Haversine formula."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    # Guard against float inaccuracies causing a > 1.0
    c = 2.0 * math.atan2(math.sqrt(min(1.0, max(0.0, a))), math.sqrt(max(0.0, 1.0 - a)))
    return EARTH_RADIUS_KM * c


def point_to_segment_distance_km(
    p_lat: float,
    p_lng: float,
    a_lat: float,
    a_lng: float,
    b_lat: float,
    b_lng: float,
    steps: int = 20,
) -> float:
    """Computes the minimum great circle distance from a point to a geodesic segment AB.

    Samples points along the great circle segment to provide reliable, bounded distance on spherical geometry.
    """
    dist_pa = haversine_distance_km(p_lat, p_lng, a_lat, a_lng)
    dist_pb = haversine_distance_km(p_lat, p_lng, b_lat, b_lng)
    min_dist = min(dist_pa, dist_pb)

    seg_len = haversine_distance_km(a_lat, a_lng, b_lat, b_lng)
    if seg_len < 1e-4:
        return min_dist

    # Sample intermediate points along the segment
    for i in range(1, steps):
        fraction = i / float(steps)
        # Linear interpolation of lat/lon for intermediate sampling
        interp_lat = a_lat + fraction * (b_lat - a_lat)
        interp_lng = a_lng + fraction * (b_lng - a_lng)
        d = haversine_distance_km(p_lat, p_lng, interp_lat, interp_lng)
        if d < min_dist:
            min_dist = d

    return min_dist


def compute_corridor_distance_km(
    point: CoordinatePoint,
    corridor_points: list[CoordinatePoint],
) -> float:
    """Computes the minimum distance from a point to a multi point travel corridor in kilometers."""
    if not corridor_points:
        raise ValueError("corridor_points cannot be empty")

    if len(corridor_points) == 1:
        return haversine_distance_km(
            point.lat, point.lng, corridor_points[0].lat, corridor_points[0].lng
        )

    min_distance = float("inf")
    for i in range(len(corridor_points) - 1):
        pt_a = corridor_points[i]
        pt_b = corridor_points[i + 1]
        dist = point_to_segment_distance_km(
            point.lat,
            point.lng,
            pt_a.lat,
            pt_a.lng,
            pt_b.lat,
            pt_b.lng,
        )
        if dist < min_distance:
            min_distance = dist

    return min_distance


def validate_candidate_corridor(
    proposal: PlaceProposal,
    corridor_points: list[CoordinatePoint],
    max_distance_km: float = DEFAULT_MAX_CORRIDOR_DISTANCE_KM,
) -> CandidateCorridorResult:
    """Validates candidate proposal coordinates and verifies distance to travel corridor."""
    is_valid_coords, coord_error = validate_coordinates(
        proposal.estimated_latitude, proposal.estimated_longitude
    )
    if not is_valid_coords:
        return CandidateCorridorResult(
            proposal=proposal,
            status=CorridorValidationStatus.MALFORMED_COORDINATES,
            distance_to_corridor_km=None,
            diagnostic=f"Malformed candidate coordinates: {coord_error}",
        )

    cand_point = CoordinatePoint(
        lat=proposal.estimated_latitude,
        lng=proposal.estimated_longitude,
    )

    try:
        dist_km = compute_corridor_distance_km(cand_point, corridor_points)
    except Exception as exc:
        return CandidateCorridorResult(
            proposal=proposal,
            status=CorridorValidationStatus.MALFORMED_COORDINATES,
            distance_to_corridor_km=None,
            diagnostic=f"Failed calculating corridor distance: {str(exc)}",
        )

    if dist_km <= max_distance_km:
        return CandidateCorridorResult(
            proposal=proposal,
            status=CorridorValidationStatus.WITHIN_CORRIDOR,
            distance_to_corridor_km=round(dist_km, 2),
            diagnostic=(
                f"Candidate within corridor bounds ({dist_km:.1f}km <= {max_distance_km:.1f}km). "
                "Coordinate match alone does not prove existence."
            ),
        )

    return CandidateCorridorResult(
        proposal=proposal,
        status=CorridorValidationStatus.OUTSIDE_CORRIDOR,
        distance_to_corridor_km=round(dist_km, 2),
        diagnostic=(
            f"Candidate distance ({dist_km:.1f}km) exceeds maximum corridor boundary "
            f"({max_distance_km:.1f}km). Discarded."
        ),
    )
