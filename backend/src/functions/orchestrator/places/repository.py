"""Canonical places repository for PostGIS spatial queries and persistence.

Conforms to spec 0002 AC-5 and AC-6.
Supports annular distance band queries and candidate proximity lookups.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from geoalchemy2 import Geography
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from functions.orchestrator.places.models import CanonicalPlaceModel
from functions.orchestrator.places.schemas import (
    CanonicalPlaceCreate,
    CanonicalPlaceUpdate,
    CoordinatePoint,
)


class CanonicalPlacesRepository:
    """Repository managing canonical places persistence and PostGIS spatial queries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_place(self, data: CanonicalPlaceCreate) -> CanonicalPlaceModel:
        """Creates a new canonical place with PostGIS geometry coordinates."""
        place_id = data.id or uuid.uuid4()
        point_geom = WKTElement(data.point.to_plain_wkt(), srid=4326)

        place = CanonicalPlaceModel(
            id=place_id,
            primary_name=data.primary_name,
            locality=data.locality,
            point=point_geom,
            names=data.names,
            access_points=data.access_points,
            provider_ids=data.provider_ids,
            geocoder_provenance=data.geocoder_provenance,
            identity_claim_refs=data.identity_claim_refs,
            access_status=data.access_status.value,
            is_candidate=data.is_candidate,
        )
        self._session.add(place)
        await self._session.flush()
        return place

    async def get_by_id(self, place_id: uuid.UUID) -> CanonicalPlaceModel | None:
        """Retrieves a canonical place by primary key UUID."""
        stmt = select(CanonicalPlaceModel).where(CanonicalPlaceModel.id == place_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_by_provider_id(
        self,
        provider_key: str,
        provider_id: str,
    ) -> CanonicalPlaceModel | None:
        """Finds a canonical place matching an external provider identifier."""
        stmt = select(CanonicalPlaceModel).where(
            CanonicalPlaceModel.provider_ids.op("->>")(provider_key) == provider_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_nearby(
        self,
        point: CoordinatePoint,
        max_distance_meters: float,
        *,
        limit: int = 50,
    ) -> list[tuple[CanonicalPlaceModel, float]]:
        """Queries places within max_distance_meters using PostGIS geography ST_DWithin."""
        if max_distance_meters < 0:
            raise ValueError("max_distance_meters cannot be negative")

        target_pt = func.ST_SetSRID(func.ST_MakePoint(point.lng, point.lat), 4326)
        geog_col = sa.cast(CanonicalPlaceModel.point, Geography)
        target_geog = sa.cast(target_pt, Geography)
        dist_expr = func.ST_Distance(geog_col, target_geog).label("distance_meters")

        stmt = (
            select(CanonicalPlaceModel, dist_expr)
            .where(func.ST_DWithin(geog_col, target_geog, max_distance_meters))
            .order_by(dist_expr.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [(row[0], float(row[1])) for row in result.all()]

    async def find_in_annular_band(
        self,
        point: CoordinatePoint,
        min_meters: float,
        max_meters: float,
        *,
        limit: int = 50,
    ) -> list[tuple[CanonicalPlaceModel, float]]:
        """Queries places within an annular distance band (min_meters to max_meters) (AC-6).

        Uses PostGIS ST_DWithin on geography coordinates to enable spatial index usage.
        """
        if min_meters < 0 or max_meters < 0:
            raise ValueError("Band distance bounds cannot be negative")
        if min_meters > max_meters:
            raise ValueError(f"min_meters ({min_meters}) cannot exceed max_meters ({max_meters})")

        target_pt = func.ST_SetSRID(func.ST_MakePoint(point.lng, point.lat), 4326)
        geog_col = sa.cast(CanonicalPlaceModel.point, Geography)
        target_geog = sa.cast(target_pt, Geography)
        dist_expr = func.ST_Distance(geog_col, target_geog).label("distance_meters")

        stmt = (
            select(CanonicalPlaceModel, dist_expr)
            .where(
                sa.and_(
                    func.ST_DWithin(geog_col, target_geog, max_meters),
                    sa.not_(func.ST_DWithin(geog_col, target_geog, min_meters)),
                )
            )
            .order_by(dist_expr.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [(row[0], float(row[1])) for row in result.all()]

    async def find_candidates_by_proximity(
        self,
        point: CoordinatePoint,
        max_distance_meters: float = 100.0,
        *,
        limit: int = 20,
    ) -> list[tuple[CanonicalPlaceModel, float]]:
        """Finds existing places within candidate proximity threshold (default 100m, AC-7)."""
        return await self.find_nearby(point, max_distance_meters, limit=limit)

    async def search_by_name(
        self,
        name: str,
        *,
        locality: str | None = None,
        limit: int = 20,
    ) -> list[CanonicalPlaceModel]:
        """Finds canonical places matching name substring on primary_name or names array."""
        clean = name.strip()
        conditions = [
            sa.or_(
                CanonicalPlaceModel.primary_name.ilike(f"%{clean}%"),
                CanonicalPlaceModel.names.cast(sa.String).ilike(f"%{clean}%"),
            )
        ]
        if locality:
            conditions.append(CanonicalPlaceModel.locality.ilike(f"%{locality.strip()}%"))

        stmt = select(CanonicalPlaceModel).where(sa.and_(*conditions)).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_place(
        self,
        place_id: uuid.UUID,
        data: CanonicalPlaceUpdate,
    ) -> CanonicalPlaceModel | None:
        """Updates attributes of an existing canonical place."""
        place = await self.get_by_id(place_id)
        if not place:
            return None

        if data.primary_name is not None:
            place.primary_name = data.primary_name
        if data.locality is not None:
            place.locality = data.locality
        if data.point is not None:
            place.point = WKTElement(data.point.to_plain_wkt(), srid=4326)
        if data.names is not None:
            place.names = data.names
        if data.access_points is not None:
            place.access_points = data.access_points
        if data.provider_ids is not None:
            place.provider_ids = data.provider_ids
        if data.geocoder_provenance is not None:
            place.geocoder_provenance = data.geocoder_provenance
        if data.identity_claim_refs is not None:
            place.identity_claim_refs = data.identity_claim_refs
        if data.access_status is not None:
            place.access_status = data.access_status.value
        if data.is_candidate is not None:
            place.is_candidate = data.is_candidate

        await self._session.flush()
        return place

    async def link_identity_claim(
        self,
        place_id: uuid.UUID,
        claim_id: uuid.UUID,
    ) -> CanonicalPlaceModel | None:
        """Appends a claim reference to identity_claim_refs if not already present."""
        place = await self.get_by_id(place_id)
        if not place:
            return None

        claim_str = str(claim_id)
        current_refs = list(place.identity_claim_refs or [])
        if claim_str not in current_refs:
            place.identity_claim_refs = current_refs + [claim_str]
            await self._session.flush()

        return place
