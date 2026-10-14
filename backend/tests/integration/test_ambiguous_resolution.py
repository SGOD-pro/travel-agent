"""Integration tests for PlaceResolutionService and ambiguous candidate resolution conforming to spec 0002 AC-7.

Covers:
- AC-7: Candidate place matching resolves incoming place observations against existing
  `canonical_places` records using spatial proximity within 100 meters and name similarity.
- AC-7: Ambiguous places (same name, different localities) are flagged for clarification
  without destructive entity merging.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from functions.orchestrator.places.repository import CanonicalPlacesRepository
from functions.orchestrator.places.schemas import (
    AccessStatus,
    CanonicalPlaceCreate,
    CoordinatePoint,
)
from functions.orchestrator.places.service import (
    CandidatePlaceInput,
    PlaceResolutionService,
    ResolutionStatus,
)


def _isolated_point(base_lng: float = 75.0, base_lat: float = 18.0) -> CoordinatePoint:
    """Generates an isolated coordinate point to prevent collisions with existing database rows."""
    offset = float(uuid.uuid4().int % 50000) / 10000.0  # 0.0000 to 5.0000 degrees
    return CoordinatePoint(lng=round(base_lng + offset, 5), lat=round(base_lat + offset, 5))


@pytest.mark.asyncio
class TestAmbiguousResolution:
    """Integration tests for AC-7 candidate place resolution and ambiguous place detection."""

    async def test_resolved_within_100_meters_and_high_name_similarity(
        self, session_factory: Any
    ) -> None:
        """AC-7: Resolves candidate matching existing place within 100m and name similarity >= 0.8."""
        test_suffix = uuid.uuid4().hex[:8]
        existing_id = uuid.uuid4()
        # Use an isolated coordinate location and unique test name to prevent collisions with other tests
        exact_coords = _isolated_point(70.0, 15.0)
        primary_name = f"Dakshineswar Kali Temple {test_suffix}"
        alt_name = f"Dakshineswar Temple {test_suffix}"

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=existing_id,
                    primary_name=primary_name,
                    locality="Kolkata",
                    point=exact_coords,
                    names=[alt_name, f"Dakshineswar Kali Mandir {test_suffix}"],
                    access_status=AccessStatus.PUBLIC,
                    is_candidate=False,
                )
            )
            await session.commit()

        # Candidate within ~39 meters (0.00035 degrees latitude)
        candidate_coords = CoordinatePoint(
            lng=exact_coords.lng,
            lat=round(exact_coords.lat + 0.00035, 5),
        )
        candidate = CandidatePlaceInput(
            primary_name=alt_name,
            locality="Kolkata",
            point=candidate_coords,
            names=[primary_name],
        )

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            service = PlaceResolutionService(repo)
            result = await service.resolve_candidate(candidate)

            assert result.status == ResolutionStatus.RESOLVED
            assert result.place is not None
            assert result.place.id == existing_id
            assert result.confidence is not None and result.confidence >= 0.8
            assert result.distance_meters is not None and result.distance_meters <= 100.0

    async def test_provider_id_match_resolves_definitively(
        self, session_factory: Any
    ) -> None:
        """AC-7: Matching authoritative provider ID resolves definitively."""
        osm_id = f"node/{uuid.uuid4().hex[:10]}"
        existing_id = uuid.uuid4()
        test_suffix = uuid.uuid4().hex[:8]

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=existing_id,
                    primary_name=f"Howrah Bridge {test_suffix}",
                    locality="Kolkata",
                    point=CoordinatePoint(lng=88.3468, lat=22.5851),
                    provider_ids={"osm_id": osm_id},
                    is_candidate=False,
                )
            )
            await session.commit()

        candidate = CandidatePlaceInput(
            primary_name=f"Rabindra Setu {test_suffix}",
            locality="Kolkata",
            point=CoordinatePoint(lng=88.3469, lat=22.5852),
            provider_ids={"osm_id": osm_id},
        )

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            service = PlaceResolutionService(repo)
            result = await service.resolve_candidate(candidate)

            assert result.status == ResolutionStatus.RESOLVED
            assert result.place is not None
            assert result.place.id == existing_id
            assert result.confidence == 1.0

    async def test_ambiguous_same_name_different_locality_flagged_without_destructive_merging(
        self, session_factory: Any
    ) -> None:
        """AC-7: Ambiguous places (same name, different localities) flagged for clarification without merging."""
        test_suffix = uuid.uuid4().hex[:8]
        place_salt_lake_id = uuid.uuid4()
        mall_name = f"City Centre Mall {test_suffix}"
        # City Centre in Salt Lake, Kolkata using isolated coordinates
        coords_salt_lake = _isolated_point(76.0, 17.0)

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=place_salt_lake_id,
                    primary_name=mall_name,
                    locality="Salt Lake, Kolkata",
                    point=coords_salt_lake,
                    names=[f"City Centre 1 {test_suffix}"],
                    is_candidate=False,
                )
            )
            await session.commit()

        # Incoming candidate: same mall name in New Town, Rajarhat (~7 km away, different locality)
        coords_new_town = CoordinatePoint(
            lng=round(coords_salt_lake.lng + 0.0600, 5),
            lat=round(coords_salt_lake.lat + 0.0400, 5),
        )
        candidate = CandidatePlaceInput(
            primary_name=mall_name,
            locality="New Town, Kolkata",
            point=coords_new_town,
            names=[f"City Centre 2 {test_suffix}"],
        )

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            service = PlaceResolutionService(repo)
            result = await service.resolve_candidate(candidate, auto_create=True)

            # Assert AC-7 behavior: Flagged as AMBIGUOUS for clarification
            assert result.status == ResolutionStatus.AMBIGUOUS
            assert len(result.candidates) >= 1
            # Confirms Salt Lake place is listed as candidate
            assert any(c.id == place_salt_lake_id for c in result.candidates)
            assert "different localities" in result.reason
            assert "without destructive entity merging" in result.reason

            # Assert non-destructive creation: a distinct candidate was created for New Town
            assert result.place is not None
            assert result.place.id != place_salt_lake_id
            assert result.place.locality == "New Town, Kolkata"
            assert result.place.is_candidate is True
            await session.commit()

        # Confirm database preserves BOTH records distinctly without overwriting
        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            salt_lake_place = await repo.get_by_id(place_salt_lake_id)
            new_town_place = await repo.get_by_id(result.place.id)

            assert salt_lake_place is not None
            assert new_town_place is not None
            assert salt_lake_place.locality == "Salt Lake, Kolkata"
            assert new_town_place.locality == "New Town, Kolkata"
            assert salt_lake_place.id != new_town_place.id

    async def test_ambiguous_multiple_matches_within_100_meters(
        self, session_factory: Any
    ) -> None:
        """AC-7: Multiple places within 100m matching name threshold are flagged as AMBIGUOUS."""
        test_suffix = uuid.uuid4().hex[:8]
        id_a = uuid.uuid4()
        id_b = uuid.uuid4()
        base_point = _isolated_point(79.0, 18.0)
        pt_a = CoordinatePoint(
            lng=round(base_point.lng + 0.00010, 5),
            lat=round(base_point.lat + 0.00010, 5),
        )  # ~15m away
        pt_b = CoordinatePoint(
            lng=round(base_point.lng + 0.00020, 5),
            lat=round(base_point.lat + 0.00020, 5),
        )  # ~30m away

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=id_a,
                    primary_name=f"St Pauls Hall {test_suffix}",
                    locality="Kolkata",
                    point=pt_a,
                    names=[f"St Pauls Complex {test_suffix}"],
                )
            )
            await repo.create_place(
                CanonicalPlaceCreate(
                    id=id_b,
                    primary_name=f"St Pauls Cathedral {test_suffix}",
                    locality="Kolkata",
                    point=pt_b,
                    names=[f"St Pauls Church {test_suffix}"],
                )
            )
            await session.commit()

        # Candidate close to both with generic name "St Pauls"
        candidate = CandidatePlaceInput(
            primary_name=f"St Pauls {test_suffix}",
            locality="Kolkata",
            point=base_point,
        )

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            service = PlaceResolutionService(repo)
            result = await service.resolve_candidate(candidate)

            assert result.status == ResolutionStatus.AMBIGUOUS
            assert len(result.candidates) >= 2
            cand_ids = {c.id for c in result.candidates}
            assert id_a in cand_ids
            assert id_b in cand_ids

    async def test_new_candidate_creation_when_no_match_exists(
        self, session_factory: Any
    ) -> None:
        """AC-7: Candidate with unique name and coordinates creates new candidate place."""
        unique_name = f"Unique Sanctuary {uuid.uuid4().hex[:8]}"
        candidate_point = _isolated_point(82.0, 19.0)
        candidate = CandidatePlaceInput(
            primary_name=unique_name,
            locality="Sundarbans",
            point=candidate_point,
            geocoder_provenance="osm",
        )

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            service = PlaceResolutionService(repo)
            result = await service.resolve_candidate(candidate, auto_create=True)

            assert result.status == ResolutionStatus.NEW_CANDIDATE
            assert result.place is not None
            assert result.place.primary_name == unique_name
            assert result.place.locality == "Sundarbans"
            assert result.place.is_candidate is True
            assert result.place.geocoder_provenance == "osm"
            await session.commit()

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            persisted = await repo.get_by_id(result.place.id)
            assert persisted is not None
            assert persisted.primary_name == unique_name
            assert persisted.is_candidate is True
