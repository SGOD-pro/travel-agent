"""Pydantic schemas for canonical places and coordinate points."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from geoalchemy2.elements import WKBElement, WKTElement
from geoalchemy2.shape import to_shape
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from shapely.geometry import Point


class AccessStatus(StrEnum):
    PUBLIC = "PUBLIC"
    RESTRICTED = "RESTRICTED"
    UNKNOWN = "UNKNOWN"


class CoordinatePoint(BaseModel):
    """WGS84 coordinate point (EPSG:4326) with longitude and latitude."""

    model_config = ConfigDict(frozen=True)

    lng: float = Field(..., ge=-180.0, le=180.0, description="Longitude in decimal degrees")
    lat: float = Field(..., ge=-90.0, le=90.0, description="Latitude in decimal degrees")

    def to_ewkt(self, srid: int = 4326) -> str:
        """Return EWKT representation with SRID."""
        return f"SRID={srid};POINT({self.lng} {self.lat})"

    def to_plain_wkt(self) -> str:
        """Return plain WKT POINT representation without SRID."""
        return f"POINT({self.lng} {self.lat})"

    def to_wkt(self, srid: int = 4326) -> str:
        """Return EWKT representation with SRID (alias for to_ewkt)."""
        return self.to_ewkt(srid=srid)

    def to_point_wkt(self) -> str:
        """Return plain WKT POINT representation (alias for to_plain_wkt)."""
        return self.to_plain_wkt()

    @classmethod
    def from_shape(cls, shape: Point) -> CoordinatePoint:
        """Create CoordinatePoint from Shapely Point."""
        return cls(lng=float(shape.x), lat=float(shape.y))

    @classmethod
    def from_wkt(cls, wkt_str: str) -> CoordinatePoint:
        """Parse WKT or EWKT string into CoordinatePoint."""
        match = re.search(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)", wkt_str, re.IGNORECASE)
        if not match:
            raise ValueError(f"Invalid WKT point format: {wkt_str}")
        return cls(lng=float(match.group(1)), lat=float(match.group(2)))

    @classmethod
    def from_geo(cls, obj: Any) -> CoordinatePoint:
        """Coerce GeoAlchemy2 element, Shapely Point, dict, or WKT string into CoordinatePoint."""
        if isinstance(obj, CoordinatePoint):
            return obj
        if isinstance(obj, WKBElement | WKTElement):
            shape = to_shape(obj)
            if isinstance(shape, Point):
                return cls.from_shape(shape)
            raise ValueError(f"Unsupported geometry type: {type(shape)}")
        if isinstance(obj, Point):
            return cls.from_shape(obj)
        if isinstance(obj, str):
            return cls.from_wkt(obj)
        if isinstance(obj, dict) and "lng" in obj and "lat" in obj:
            return cls(lng=float(obj["lng"]), lat=float(obj["lat"]))
        raise ValueError(f"Cannot coerce {type(obj)} to CoordinatePoint")


class CanonicalPlaceBase(BaseModel):
    """Base schema for canonical places."""

    primary_name: str = Field(..., min_length=1, max_length=256)
    locality: str = Field(..., min_length=1, max_length=128)
    point: CoordinatePoint
    names: list[str] = Field(default_factory=list)
    access_points: list[dict[str, Any]] = Field(default_factory=list)
    provider_ids: dict[str, Any] = Field(default_factory=dict)
    geocoder_provenance: str | None = Field(default=None, max_length=64)
    identity_claim_refs: list[str] = Field(default_factory=list)
    access_status: AccessStatus = Field(default=AccessStatus.UNKNOWN)
    is_candidate: bool = Field(default=True)

    @field_validator("point", mode="before")
    @classmethod
    def parse_point(cls, v: Any) -> CoordinatePoint:
        return CoordinatePoint.from_geo(v)


class CanonicalPlaceCreate(CanonicalPlaceBase):
    """Payload for creating a new canonical place."""

    id: uuid.UUID | None = None


class CanonicalPlaceUpdate(BaseModel):
    """Payload for updating an existing canonical place."""

    primary_name: str | None = Field(default=None, min_length=1, max_length=256)
    locality: str | None = Field(default=None, min_length=1, max_length=128)
    point: CoordinatePoint | None = None
    names: list[str] | None = None
    access_points: list[dict[str, Any]] | None = None
    provider_ids: dict[str, Any] | None = None
    geocoder_provenance: str | None = None
    identity_claim_refs: list[str] | None = None
    access_status: AccessStatus | None = None
    is_candidate: bool | None = None

    @field_validator("point", mode="before")
    @classmethod
    def parse_point(cls, v: Any) -> CoordinatePoint | None:
        if v is None:
            return None
        return CoordinatePoint.from_geo(v)


class CanonicalPlaceRead(CanonicalPlaceBase):
    """Read representation of a persisted canonical place."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def coerce_orm_attributes(cls, data: Any) -> Any:
        """Handle ORM object attributes before standard validation."""
        if isinstance(data, dict):
            return data
        if hasattr(data, "point"):
            point_val = getattr(data, "point", None)
            extracted: dict[str, Any] = {}
            for field_name in cls.model_fields:
                val = getattr(data, field_name, None)
                if val is not None:
                    extracted[field_name] = val
            if point_val is not None and not isinstance(point_val, CoordinatePoint):
                extracted["point"] = CoordinatePoint.from_geo(point_val)
            return extracted
        return data
