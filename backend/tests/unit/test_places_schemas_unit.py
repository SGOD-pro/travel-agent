"""Unit tests for canonical places schemas and coordinate types conforming to spec 0002.

Covers: AC-5 (Canonical places persistence, coordinate handling, and schema validation).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from geoalchemy2.elements import WKTElement
from pydantic import ValidationError
from shapely.geometry import Point

from functions.orchestrator.places.schemas import (
    AccessStatus,
    CanonicalPlaceCreate,
    CanonicalPlaceRead,
    CanonicalPlaceUpdate,
    CoordinatePoint,
)

# ============================================================================
# CoordinatePoint Unit Tests (AC-5)
# ============================================================================


def test_coordinate_point_initialization_with_valid_coordinates() -> None:
    """Covers: AC-5 - CoordinatePoint creates successfully with valid decimal degrees."""
    # Arrange & Act
    point = CoordinatePoint(lng=88.3639, lat=22.5726)

    # Assert
    assert point.lng == 88.3639
    assert point.lat == 22.5726


@pytest.mark.parametrize(
    ("lng", "lat"),
    [
        (-180.0, -90.0),
        (180.0, 90.0),
        (0.0, 0.0),
        (-0.0, 0.0),
        (-122.4194, 37.7749),
    ],
)
def test_coordinate_point_boundary_coordinates(lng: float, lat: float) -> None:
    """Covers: AC-5 - CoordinatePoint accepts extreme valid boundary coordinates."""
    # Arrange & Act
    point = CoordinatePoint(lng=lng, lat=lat)

    # Assert
    assert point.lng == lng
    assert point.lat == lat


@pytest.mark.parametrize(
    ("lng", "lat"),
    [
        (-180.1, 20.0),
        (180.1, 20.0),
        (88.0, -90.1),
        (88.0, 90.1),
        (999.0, 999.0),
    ],
)
def test_coordinate_point_rejects_out_of_range_coordinates(lng: float, lat: float) -> None:
    """Covers: AC-5 - CoordinatePoint raises ValidationError on out-of-bounds latitude or longitude."""
    # Arrange, Act & Assert
    with pytest.raises(ValidationError):
        CoordinatePoint(lng=lng, lat=lat)


def test_coordinate_point_to_wkt_formats() -> None:
    """Covers: AC-5 - CoordinatePoint formats plain WKT and EWKT with custom SRID."""
    # Arrange
    point = CoordinatePoint(lng=88.3576, lat=22.6534)

    # Act & Assert
    assert point.to_plain_wkt() == "POINT(88.3576 22.6534)"
    assert point.to_ewkt(srid=4326) == "SRID=4326;POINT(88.3576 22.6534)"
    assert point.to_ewkt(srid=3857) == "SRID=3857;POINT(88.3576 22.6534)"
    # Backwards compatible aliases
    assert point.to_point_wkt() == point.to_plain_wkt()
    assert point.to_wkt() == point.to_ewkt()


def test_coordinate_point_from_shapely_shape() -> None:
    """Covers: AC-5 - CoordinatePoint constructs accurately from a Shapely Point object."""
    # Arrange
    shapely_point = Point(77.2090, 28.6139)

    # Act
    point = CoordinatePoint.from_shape(shapely_point)

    # Assert
    assert point.lng == 77.2090
    assert point.lat == 28.6139


@pytest.mark.parametrize(
    "wkt_str",
    [
        "POINT(88.3576 22.6534)",
        "POINT (88.3576 22.6534)",
        "point(88.3576 22.6534)",
        "SRID=4326;POINT(88.3576 22.6534)",
        "  POINT (  88.3576   22.6534  ) ",
    ],
)
def test_coordinate_point_from_wkt_parsing(wkt_str: str) -> None:
    """Covers: AC-5 - CoordinatePoint parses standard and whitespace-padded WKT/EWKT strings."""
    # Arrange & Act
    point = CoordinatePoint.from_wkt(wkt_str)

    # Assert
    assert abs(point.lng - 88.3576) < 1e-5
    assert abs(point.lat - 22.6534) < 1e-5


def test_coordinate_point_from_wkt_rejects_malformed_string() -> None:
    """Covers: AC-5 - CoordinatePoint.from_wkt raises ValueError on non-point or malformed WKT."""
    # Arrange, Act & Assert
    with pytest.raises(ValueError, match="Invalid WKT point format"):
        CoordinatePoint.from_wkt("POLYGON((0 0, 1 1, 1 0, 0 0))")

    with pytest.raises(ValueError, match="Invalid WKT point format"):
        CoordinatePoint.from_wkt("NOT_A_GEOMETRY")


def test_coordinate_point_from_geo_coercion_types() -> None:
    """Covers: AC-5 - CoordinatePoint.from_geo coerces across all supported input formats."""
    # Arrange
    orig = CoordinatePoint(lng=88.35, lat=22.65)
    wkt_elem = WKTElement("POINT(88.35 22.65)", srid=4326)
    shapely_pt = Point(88.35, 22.65)
    dict_pt = {"lng": 88.35, "lat": 22.65}
    str_pt = "POINT(88.35 22.65)"

    # Act & Assert
    assert CoordinatePoint.from_geo(orig) == orig
    assert CoordinatePoint.from_geo(wkt_elem) == orig
    assert CoordinatePoint.from_geo(shapely_pt) == orig
    assert CoordinatePoint.from_geo(dict_pt) == orig
    assert CoordinatePoint.from_geo(str_pt) == orig


def test_coordinate_point_from_geo_rejects_unsupported_type() -> None:
    """Covers: AC-5 - CoordinatePoint.from_geo raises ValueError on unsupported objects."""
    # Arrange, Act & Assert
    with pytest.raises(ValueError, match="Cannot coerce"):
        CoordinatePoint.from_geo(12345)


# ============================================================================
# CanonicalPlace Schema Unit Tests (AC-5)
# ============================================================================


def test_canonical_place_create_with_coerced_point_dict() -> None:
    """Covers: AC-5 - CanonicalPlaceCreate coerces dictionary point to CoordinatePoint."""
    # Arrange
    payload = {
        "primary_name": "Victoria Memorial",
        "locality": "Kolkata",
        "point": {"lng": 88.3426, "lat": 22.5448},
        "names": ["Victoria Memorial Hall", "ভিক্টোরিয়া মেমোরিয়াল"],
        "access_status": "PUBLIC",
    }

    # Act
    place = CanonicalPlaceCreate(**payload)

    # Assert
    assert place.primary_name == "Victoria Memorial"
    assert place.locality == "Kolkata"
    assert isinstance(place.point, CoordinatePoint)
    assert place.point.lng == 88.3426
    assert place.point.lat == 22.5448
    assert place.access_status == AccessStatus.PUBLIC
    assert len(place.names) == 2


def test_canonical_place_create_with_wkt_string() -> None:
    """Covers: AC-5 - CanonicalPlaceCreate coerces raw WKT string point."""
    # Arrange
    payload = {
        "primary_name": "Howrah Bridge",
        "locality": "Howrah",
        "point": "POINT(88.3476 22.5851)",
    }

    # Act
    place = CanonicalPlaceCreate(**payload)

    # Assert
    assert isinstance(place.point, CoordinatePoint)
    assert abs(place.point.lng - 88.3476) < 1e-4
    assert place.is_candidate is True
    assert place.access_status == AccessStatus.UNKNOWN


def test_canonical_place_validation_rejects_empty_strings() -> None:
    """Covers: AC-5 - CanonicalPlaceCreate rejects empty primary_name and locality."""
    # Arrange, Act & Assert
    with pytest.raises(ValidationError):
        CanonicalPlaceCreate(
            primary_name="",
            locality="Kolkata",
            point={"lng": 88.34, "lat": 22.54},
        )

    with pytest.raises(ValidationError):
        CanonicalPlaceCreate(
            primary_name="Valid Name",
            locality="",
            point={"lng": 88.34, "lat": 22.54},
        )


def test_canonical_place_update_optional_point_handling() -> None:
    """Covers: AC-5 - CanonicalPlaceUpdate handles optional point field gracefully."""
    # Arrange & Act
    update_without_point = CanonicalPlaceUpdate(primary_name="New Name")
    update_with_point = CanonicalPlaceUpdate(point={"lng": 88.1, "lat": 22.1})

    # Assert
    assert update_without_point.point is None
    assert update_with_point.point is not None
    assert update_with_point.point.lng == 88.1


def test_canonical_place_read_safe_orm_coercion() -> None:
    """Covers: AC-5 - CanonicalPlaceRead safely converts mock ORM attributes without errors."""

    # Arrange: Mock ORM object without depending on internal __dict__ state
    class MockOrmPlace:
        def __init__(self) -> None:
            self.id = uuid.uuid4()
            self.primary_name = "Belur Math"
            self.locality = "Howrah"
            self.point = WKTElement("POINT(88.3562 22.6322)", srid=4326)
            self.names = ["Belur Math Temple"]
            self.access_points: list[dict[str, Any]] = []
            self.provider_ids = {"osm_id": "node/1234"}
            self.geocoder_provenance = "osm"
            self.identity_claim_refs: list[str] = []
            self.access_status = AccessStatus.PUBLIC
            self.is_candidate = False
            self.created_at = datetime.now(UTC)
            self.updated_at = datetime.now(UTC)

    mock_obj = MockOrmPlace()

    # Act
    read_place = CanonicalPlaceRead.model_validate(mock_obj)

    # Assert
    assert read_place.primary_name == "Belur Math"
    assert read_place.point.lng == 88.3562
    assert read_place.access_status == AccessStatus.PUBLIC
