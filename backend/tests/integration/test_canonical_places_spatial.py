"""Integration tests for CanonicalPlacesRepository and PostGIS spatial queries conforming to spec 0002.

Covers:
- AC-5: Canonical places persistence with reviewed names, locality, PostGIS point, geocoder provenance.
- AC-6: Spatial queries for nearby places supporting configurable annular bands
  (2000 to 3000 meters and 5000 to 7000 meters) using PostGIS ST_DWithin on geography coordinates.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from functions.orchestrator.places.repository import CanonicalPlacesRepository
from functions.orchestrator.places.schemas import (
    AccessStatus,
    CanonicalPlaceCreate,
    CanonicalPlaceUpdate,
    CoordinatePoint,
)


@pytest.mark.asyncio
class TestCanonicalPlacesSpatialRepository:
    """Integration tests for CanonicalPlacesRepository spatial operations."""

    async def test_create_and_get_canonical_place_with_geocoder_provenance(
        self, session_factory: Any
    ) -> None:
        """AC-5: Canonical place persists with coordinates, access points, provider IDs, and geocoder provenance."""
        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            place_id = uuid.uuid4()
            coords = CoordinatePoint(lng=88.3576, lat=22.6534)

            create_data = CanonicalPlaceCreate(
                id=place_id,
                primary_name="Dakshineswar Kali Temple",
                locality="Kolkata",
                point=coords,
                names=["Dakshineswar Temple", "Dakshineswar Kali Mandir"],
                access_points=[
                    {"label": "Main Gate", "coords": [88.3575, 22.6533]},
                    {"label": "Parking Lot", "coords": [88.3580, 22.6530]},
                ],
                provider_ids={
                    "google_place_id": f"ChIJ_{uuid.uuid4().hex[:12]}",
                    "osm_id": f"node/{uuid.uuid4().hex[:8]}",
                },
                geocoder_provenance="google_places",
                identity_claim_refs=[],
                access_status=AccessStatus.PUBLIC,
                is_candidate=False,
            )

            await repo.create_place(create_data)
            await session.commit()

        # Query back
        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            loaded = await repo.get_by_id(place_id)
            assert loaded is not None
            assert loaded.id == place_id
            assert loaded.primary_name == "Dakshineswar Kali Temple"
            assert loaded.locality == "Kolkata"
            assert loaded.names == ["Dakshineswar Temple", "Dakshineswar Kali Mandir"]
            assert loaded.geocoder_provenance == "google_places"
            assert loaded.access_status == "PUBLIC"
            assert loaded.is_candidate is False
            assert len(loaded.access_points) == 2

    async def test_find_by_provider_id(self, session_factory: Any) -> None:
        """Finds place by external provider ID in JSONB."""
        osm_node_id = f"node/{uuid.uuid4().hex[:10]}"
        place_id = uuid.uuid4()

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=place_id,
                    primary_name="Belur Math",
                    locality="Howrah",
                    point=CoordinatePoint(lng=88.3558, lat=22.6288),
                    provider_ids={"osm_id": osm_node_id},
                )
            )
            await session.commit()

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            found = await repo.find_by_provider_id("osm_id", osm_node_id)
            assert found is not None
            assert found.id == place_id
            assert found.primary_name == "Belur Math"

            # Non-existent returns None
            not_found = await repo.find_by_provider_id("osm_id", "node/non_existent_9999")
            assert not_found is None

    async def test_annular_band_queries_2000_to_3000_meters(
        self, session_factory: Any
    ) -> None:
        """AC-6: PostGIS spatial queries correctly filter places in a 2000m to 3000m annular band."""
        # Origin: Center of Kolkata (Maidan / Park Street area)
        origin = CoordinatePoint(lng=88.3500, lat=22.5500)

        # Place A: ~1000m away (too close, should be excluded from 2000-3000m band)
        # 1 degree latitude ~ 111 km -> 1000m ~ 0.009 degrees lat
        place_too_close_id = uuid.uuid4()
        pt_close = CoordinatePoint(lng=88.3500, lat=22.5590)  # ~1000m North

        # Place B: ~2500m away (inside 2000m-3000m band, should be included)
        place_in_band_id = uuid.uuid4()
        pt_band = CoordinatePoint(lng=88.3500, lat=22.5725)  # ~2500m North

        # Place C: ~4000m away (too far, should be excluded from 2000-3000m band)
        place_too_far_id = uuid.uuid4()
        pt_far = CoordinatePoint(lng=88.3500, lat=22.5860)  # ~4000m North

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=place_too_close_id,
                    primary_name=f"Close Place {uuid.uuid4().hex[:6]}",
                    locality="Kolkata",
                    point=pt_close,
                )
            )
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=place_in_band_id,
                    primary_name=f"Band 2k-3k Place {uuid.uuid4().hex[:6]}",
                    locality="Kolkata",
                    point=pt_band,
                )
            )
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=place_too_far_id,
                    primary_name=f"Far Place {uuid.uuid4().hex[:6]}",
                    locality="Kolkata",
                    point=pt_far,
                )
            )
            await session.commit()

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            results = await repo.find_in_annular_band(
                point=origin,
                min_meters=2000.0,
                max_meters=3000.0,
                limit=100,
            )

            result_ids = {p.id for p, _ in results}
            assert place_in_band_id in result_ids, "Expected 2500m place inside 2000-3000m band"
            assert place_too_close_id not in result_ids, "1000m place should be excluded from 2000-3000m band"
            assert place_too_far_id not in result_ids, "4000m place should be excluded from 2000-3000m band"

            # Check distance calculation
            for place, dist in results:
                if place.id == place_in_band_id:
                    assert 2000.0 <= dist <= 3000.0, f"Distance {dist} should be in [2000, 3000]"

    async def test_annular_band_queries_5000_to_7000_meters(
        self, session_factory: Any
    ) -> None:
        """AC-6: PostGIS spatial queries correctly filter places in a 5000m to 7000m annular band."""
        origin = CoordinatePoint(lng=88.3500, lat=22.5500)

        # Place A: ~2000m away (inside 2-3km, but outside 5-7km)
        p_2k_id = uuid.uuid4()
        pt_2k = CoordinatePoint(lng=88.3500, lat=22.5680)

        # Place B: ~6000m away (inside 5000m-7000m band, ~0.054 deg north)
        p_6k_id = uuid.uuid4()
        pt_6k = CoordinatePoint(lng=88.3500, lat=22.6040)

        # Place C: ~10000m away (outside 7000m)
        p_10k_id = uuid.uuid4()
        pt_10k = CoordinatePoint(lng=88.3500, lat=22.6400)

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=p_2k_id,
                    primary_name=f"2k Place {uuid.uuid4().hex[:6]}",
                    locality="Kolkata",
                    point=pt_2k,
                )
            )
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=p_6k_id,
                    primary_name=f"6k Place {uuid.uuid4().hex[:6]}",
                    locality="Kolkata",
                    point=pt_6k,
                )
            )
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=p_10k_id,
                    primary_name=f"10k Place {uuid.uuid4().hex[:6]}",
                    locality="Kolkata",
                    point=pt_10k,
                )
            )
            await session.commit()

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            results = await repo.find_in_annular_band(
                point=origin,
                min_meters=5000.0,
                max_meters=7000.0,
                limit=100,
            )

            result_ids = {p.id for p, _ in results}
            assert p_6k_id in result_ids, "Expected 6000m place inside 5000-7000m band"
            assert p_2k_id not in result_ids, "2000m place should be excluded from 5000-7000m band"
            assert p_10k_id not in result_ids, "10000m place should be excluded from 5000-7000m band"

            for place, dist in results:
                if place.id == p_6k_id:
                    assert 5000.0 <= dist <= 7000.0, f"Distance {dist} should be in [5000, 7000]"

    async def test_update_place_and_link_identity_claims(
        self, session_factory: Any
    ) -> None:
        """Verifies place updates and identity claim linking."""
        place_id = uuid.uuid4()
        claim_id_1 = uuid.uuid4()
        claim_id_2 = uuid.uuid4()

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=place_id,
                    primary_name="Indian Museum",
                    locality="Kolkata",
                    point=CoordinatePoint(lng=88.3512, lat=22.5579),
                    is_candidate=True,
                )
            )
            await session.commit()

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            # Update candidate status to false and add alias
            updated = await repo.update_place(
                place_id,
                CanonicalPlaceUpdate(
                    is_candidate=False,
                    names=["The Imperial Museum at Calcutta"],
                    access_status=AccessStatus.PUBLIC,
                ),
            )
            assert updated is not None
            assert updated.is_candidate is False
            assert updated.names == ["The Imperial Museum at Calcutta"]
            assert updated.access_status == "PUBLIC"

            # Link identity claims
            await repo.link_identity_claim(place_id, claim_id_1)
            await repo.link_identity_claim(place_id, claim_id_2)
            await session.commit()

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            place = await repo.get_by_id(place_id)
            assert place is not None
            assert str(claim_id_1) in place.identity_claim_refs
            assert str(claim_id_2) in place.identity_claim_refs
