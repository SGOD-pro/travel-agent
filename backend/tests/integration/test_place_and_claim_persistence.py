"""Integration tests for claim and place persistence models conforming to spec 0002 Slice 2.

Satisfies:
- AC-5: Canonical places persistence in canonical_places with reviewed names, locality,
  PostGIS geometry(Point, 4326), access points, geocoder provenance, provider identifier
  mappings, and GiST indexing.
- AC-8: Extracted claims persistence in claims linked to observations via claim_evidence
  junction table with typed values, category, validity interval, and geographic scope.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from geoalchemy2 import Geography
from geoalchemy2.elements import WKTElement
from geoalchemy2.functions import ST_DWithin
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from functions.api.models import TripModel
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.models import ClaimEvidenceModel, ClaimModel
from functions.orchestrator.evidence.schemas import (
    AvailabilityStatus,
    ClaimCategory,
    ClaimCreate,
    ClaimEvidenceRead,
    ClaimRead,
    ClaimUpdate,
    EvidenceSupportType,
    FreshnessStatus,
    VerificationStatus,
)
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.places.models import CanonicalPlaceModel
from functions.orchestrator.places.schemas import (
    AccessStatus,
    CanonicalPlaceRead,
    CoordinatePoint,
)


async def _create_test_execution(session: Any) -> uuid.UUID:
    """Helper creating a parent execution row."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    now = datetime.now(UTC)

    trip = TripModel(
        id=trip_id,
        owner_id=owner_id,
        current_version=1,
        state="draft",
        created_at=now,
        updated_at=now,
    )
    session.add(trip)
    await session.flush()

    execution = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow="PLANNING",
        status="RUNNING",
        context_version=1,
        deadline_at=now + timedelta(hours=2),
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    await session.flush()
    return execution_id


async def _create_test_observation(session: Any, execution_id: uuid.UUID) -> uuid.UUID:
    """Helper creating a parent source observation row."""
    observation_id = uuid.uuid4()
    now = datetime.now(UTC)
    obs = SourceObservationModel(
        id=observation_id,
        execution_id=execution_id,
        source_url="https://wbtourism.gov.in/attractions/dakshineswar",
        canonical_url="https://wbtourism.gov.in/attractions/dakshineswar",
        publisher="wbtourism.gov.in",
        content_hash=f"hash_{uuid.uuid4().hex}",
        retrieved_at=now,
        published_at=now - timedelta(days=10),
        excerpt_text="Dakshineswar Kali Temple remains open daily from 06:00 to 12:30 and 15:00 to 20:30.",
        excerpt_locator="main article p",
        extraction_method="SCRAPLING_HTTP",
        created_at=now,
    )
    session.add(obs)
    await session.flush()
    return observation_id


@pytest.mark.asyncio
async def test_canonical_places_crud_and_coordinates(session_factory: Any) -> None:
    """AC-5: Canonical place persists with coordinates, reviewed names, and provider IDs."""
    async with session_factory() as session:
        place_id = uuid.uuid4()
        coords = CoordinatePoint(lng=88.3576, lat=22.6534)
        assert coords.to_plain_wkt() == "POINT(88.3576 22.6534)"
        assert coords.to_ewkt() == "SRID=4326;POINT(88.3576 22.6534)"
        point_geom = WKTElement(coords.to_plain_wkt(), srid=4326)

        place = CanonicalPlaceModel(
            id=place_id,
            names=["Dakshineswar Temple", "Dakshineswar Kali Mandir"],
            primary_name="Dakshineswar Kali Temple",
            locality="Kolkata",
            point=point_geom,
            access_points=[
                {"label": "Main Gate", "coords": [88.3575, 22.6533]},
                {"label": "Parking", "coords": [88.3580, 22.6530]},
            ],
            provider_ids={
                "google_place_id": f"ChIJ_{uuid.uuid4().hex[:12]}",
                "osm_id": f"node/{uuid.uuid4().hex[:8]}",
            },
            geocoder_provenance="google_places",
            identity_claim_refs=[],
            access_status="PUBLIC",
            is_candidate=False,
        )
        session.add(place)
        await session.commit()

    # Query back and verify attributes
    async with session_factory() as session:
        res = await session.execute(
            select(CanonicalPlaceModel).where(CanonicalPlaceModel.id == place_id)
        )
        loaded = res.scalar_one_or_none()
        assert loaded is not None
        assert loaded.primary_name == "Dakshineswar Kali Temple"
        assert loaded.locality == "Kolkata"
        assert loaded.names == ["Dakshineswar Temple", "Dakshineswar Kali Mandir"]
        assert loaded.access_status == "PUBLIC"
        assert loaded.is_candidate is False
        assert loaded.geocoder_provenance == "google_places"
        assert "google_place_id" in loaded.provider_ids

        # Verify Pydantic schema validation from ORM
        read_schema = CanonicalPlaceRead.model_validate(loaded)
        assert read_schema.id == place_id
        assert abs(read_schema.point.lng - 88.3576) < 1e-4
        assert abs(read_schema.point.lat - 22.6534) < 1e-4
        assert read_schema.access_status == AccessStatus.PUBLIC


@pytest.mark.asyncio
async def test_canonical_places_spatial_gist_query(session_factory: Any) -> None:
    """AC-5 & AC-6: PostGIS spatial lookup using ST_DWithin and GiST index."""
    async with session_factory() as session:
        # Place 1: Dakshineswar (North Kolkata)
        dakshineswar_id = uuid.uuid4()
        dakshineswar = CanonicalPlaceModel(
            id=dakshineswar_id,
            primary_name="Dakshineswar Kali Temple",
            locality="Kolkata",
            point=WKTElement("POINT(88.3576 22.6534)", srid=4326),
            names=["Dakshineswar"],
            provider_ids={"osm_id": f"node/{uuid.uuid4().hex[:8]}"},
            access_status="PUBLIC",
        )

        # Place 2: Victoria Memorial (Central South Kolkata, ~10 km from Dakshineswar)
        victoria_id = uuid.uuid4()
        victoria = CanonicalPlaceModel(
            id=victoria_id,
            primary_name="Victoria Memorial",
            locality="Kolkata",
            point=WKTElement("POINT(88.3426 22.5448)", srid=4326),
            names=["Victoria Memorial Hall"],
            provider_ids={"osm_id": f"node/{uuid.uuid4().hex[:8]}"},
            access_status="PUBLIC",
        )

        # Place 3: Taj Mahal (Agra, ~1150 km from Kolkata)
        taj_id = uuid.uuid4()
        taj = CanonicalPlaceModel(
            id=taj_id,
            primary_name="Taj Mahal",
            locality="Agra",
            point=WKTElement("POINT(78.0421 27.1751)", srid=4326),
            names=["Taj"],
            provider_ids={"osm_id": f"node/{uuid.uuid4().hex[:8]}"},
            access_status="PUBLIC",
        )

        session.add_all([dakshineswar, victoria, taj])
        await session.commit()

        # Verify planner selects GiST index idx_canonical_places_point for spatial bounding queries
        await session.execute(sa.text("SET enable_seqscan = off;"))
        explain_res = await session.execute(
            sa.text(
                """
                EXPLAIN (FORMAT TEXT)
                SELECT id FROM canonical_places
                WHERE ST_DWithin(point, ST_SetSRID(ST_MakePoint(88.3426, 22.5851), 4326), 0.1);
                """
            )
        )
        explain_lines = [row[0] for row in explain_res.fetchall()]
        plan_text = " ".join(explain_lines)
        assert "idx_canonical_places_point" in plan_text
        await session.execute(sa.text("SET enable_seqscan = on;"))

        # Query origin: Howrah Railway Station (88.3426, 22.5851)
        origin_wkt = "SRID=4326;POINT(88.3426 22.5851)"

        # ST_DWithin geography query within 15 km (15000 meters)
        stmt = select(CanonicalPlaceModel.id, CanonicalPlaceModel.primary_name).where(
            CanonicalPlaceModel.id.in_([dakshineswar_id, victoria_id, taj_id]),
            ST_DWithin(
                sa.cast(CanonicalPlaceModel.point, Geography),
                sa.cast(origin_wkt, Geography),
                15000,
            ),
        )
        res = await session.execute(stmt)
        nearby_rows = res.fetchall()
        nearby_ids = {row[0] for row in nearby_rows}

        assert dakshineswar_id in nearby_ids
        assert victoria_id in nearby_ids
        assert taj_id not in nearby_ids


@pytest.mark.asyncio
async def test_canonical_places_provider_id_uniqueness(session_factory: Any) -> None:
    """AC-5: Unique constraint on specific provider IDs (e.g. osm_id)."""
    shared_osm_id = f"osm_node_{uuid.uuid4().hex[:8]}"

    async with session_factory() as session:
        place1 = CanonicalPlaceModel(
            id=uuid.uuid4(),
            primary_name="Original Place",
            locality="Kolkata",
            point=WKTElement("POINT(88.3639 22.5726)", srid=4326),
            provider_ids={"osm_id": shared_osm_id},
        )
        session.add(place1)
        await session.commit()

    async with session_factory() as session:
        place2 = CanonicalPlaceModel(
            id=uuid.uuid4(),
            primary_name="Duplicate Place with identical OSM ID",
            locality="Kolkata",
            point=WKTElement("POINT(88.3640 22.5727)", srid=4326),
            provider_ids={"osm_id": shared_osm_id},
        )
        session.add(place2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_claims_crud_and_validation(session_factory: Any) -> None:
    """AC-8: Extracted claims persist with typed values, validity, and scope."""
    now = datetime.now(UTC)
    claim_id = uuid.uuid4()
    subject_id = "dakshineswar-temple"

    async with session_factory() as session:
        execution_id = await _create_test_execution(session)

        place = CanonicalPlaceModel(
            id=uuid.uuid4(),
            primary_name="Dakshineswar Temple",
            locality="Kolkata",
            point=WKTElement("POINT(88.3576 22.6534)", srid=4326),
        )
        session.add(place)
        await session.flush()

        claim = ClaimModel(
            id=claim_id,
            execution_id=execution_id,
            subject_id=subject_id,
            place_id=place.id,
            category=ClaimCategory.HOURS,
            typed_value={"open": "06:00", "close": "12:30", "evening_open": "15:00"},
            verification=VerificationStatus.VERIFIED,
            availability=AvailabilityStatus.AVAILABLE,
            freshness=FreshnessStatus.FRESH,
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=90),
            geographic_scope={"locality": "Kolkata", "state": "WB"},
            policy_version="1.0",
            confidence=0.95,
            contradictions=[],
            decision_history=[
                {"transition": "VERIFIED", "reason": "Authoritative portal confirmation"}
            ],
        )
        session.add(claim)
        await session.commit()

    async with session_factory() as session:
        res = await session.execute(select(ClaimModel).where(ClaimModel.id == claim_id))
        loaded = res.scalar_one_or_none()
        assert loaded is not None
        assert loaded.subject_id == subject_id
        assert loaded.category == ClaimCategory.HOURS
        assert loaded.verification == VerificationStatus.VERIFIED
        assert loaded.typed_value["open"] == "06:00"
        assert loaded.confidence == 0.95

        # Validate with Pydantic schema
        read_claim = ClaimRead.model_validate(loaded)
        assert read_claim.id == claim_id
        assert read_claim.category == ClaimCategory.HOURS
        assert read_claim.confidence == 0.95


def test_claims_pydantic_validation_invariants() -> None:
    """AC-8: Invariant checks on claim validity interval and confidence."""
    now = datetime.now(UTC)

    # valid_until earlier than valid_from must raise validation error
    with pytest.raises(ValidationError):
        ClaimCreate(
            subject_id="test-place",
            category=ClaimCategory.FEES,
            typed_value={"entry_fee_inr": 50},
            valid_from=now,
            valid_until=now - timedelta(hours=1),
        )

    # confidence out of 0.0 to 1.0 range must raise validation error
    with pytest.raises(ValidationError):
        ClaimCreate(
            subject_id="test-place",
            category=ClaimCategory.FEES,
            typed_value={"entry_fee_inr": 50},
            confidence=1.5,
        )

    # Partial update invariant check with existing bounds
    update_inverted = ClaimUpdate(valid_until=now - timedelta(hours=1))
    with pytest.raises(ValueError, match="Updated valid_until cannot be earlier than valid_from"):
        update_inverted.validate_with_existing(
            existing_valid_from=now,
            existing_valid_until=now + timedelta(days=1),
        )

    # Valid partial update passes without error
    update_valid = ClaimUpdate(valid_until=now + timedelta(days=2))
    update_valid.validate_with_existing(
        existing_valid_from=now,
        existing_valid_until=now + timedelta(days=1),
    )


@pytest.mark.asyncio
async def test_claim_evidence_junction_crud_and_composite_pk(session_factory: Any) -> None:
    """AC-8: Claim linked to observations via claim_evidence with composite PK."""
    async with session_factory() as session:
        execution_id = await _create_test_execution(session)
        obs_id = await _create_test_observation(session, execution_id)

        claim_id = uuid.uuid4()
        claim = ClaimModel(
            id=claim_id,
            execution_id=execution_id,
            subject_id="dakshineswar-temple",
            category=ClaimCategory.HOURS,
            typed_value={"open": "06:00", "close": "12:30"},
            verification=VerificationStatus.VERIFIED,
        )
        session.add(claim)
        await session.flush()

        evidence_link = ClaimEvidenceModel(
            claim_id=claim_id,
            observation_id=obs_id,
            support_type=EvidenceSupportType.SUPPORTS,
            locator="main article p",
        )
        session.add(evidence_link)
        await session.commit()

    # Query back through relationships
    async with session_factory() as session:
        res = await session.execute(
            select(ClaimModel).where(ClaimModel.id == claim_id)
        )
        loaded_claim = res.scalar_one_or_none()
        assert loaded_claim is not None
        assert len(loaded_claim.evidence_links) == 1
        assert loaded_claim.evidence_links[0].observation_id == obs_id
        assert loaded_claim.evidence_links[0].support_type == EvidenceSupportType.SUPPORTS
        assert len(loaded_claim.observations) == 1
        assert loaded_claim.observations[0].id == obs_id

        # Schema serialization
        schema = ClaimEvidenceRead.model_validate(loaded_claim.evidence_links[0])
        assert schema.claim_id == claim_id
        assert schema.observation_id == obs_id
        assert schema.support_type == EvidenceSupportType.SUPPORTS

    # Verify duplicate composite PK (claim_id, observation_id) is rejected
    async with session_factory() as session:
        duplicate_link = ClaimEvidenceModel(
            claim_id=claim_id,
            observation_id=obs_id,
            support_type=EvidenceSupportType.REFUTES,
            locator="second paragraph",
        )
        session.add(duplicate_link)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_cascade_and_set_null_foreign_key_behaviors(session_factory: Any) -> None:
    """AC-5 & AC-8: Deleting place sets place_id null; deleting claim cascades evidence."""
    async with session_factory() as session:
        execution_id = await _create_test_execution(session)
        obs_id = await _create_test_observation(session, execution_id)

        place_id = uuid.uuid4()
        place = CanonicalPlaceModel(
            id=place_id,
            primary_name="Temporary Place",
            locality="Kolkata",
            point=WKTElement("POINT(88.3639 22.5726)", srid=4326),
        )
        session.add(place)
        await session.flush()

        claim_id = uuid.uuid4()
        claim = ClaimModel(
            id=claim_id,
            execution_id=execution_id,
            subject_id="temp-subject",
            place_id=place_id,
            category=ClaimCategory.RESTRICTION,
            typed_value={"restriction": "NO_HEAVY_VEHICLES"},
            verification=VerificationStatus.UNVERIFIED,
        )
        session.add(claim)
        await session.flush()

        evidence_link = ClaimEvidenceModel(
            claim_id=claim_id,
            observation_id=obs_id,
            support_type=EvidenceSupportType.SUPPORTS,
            locator="selector",
        )
        session.add(evidence_link)
        await session.commit()

    # 1. Delete canonical place -> claim.place_id becomes NULL (SET NULL)
    async with session_factory() as session:
        res = await session.execute(
            select(CanonicalPlaceModel).where(CanonicalPlaceModel.id == place_id)
        )
        place_to_delete = res.scalar_one_or_none()
        assert place_to_delete is not None
        await session.delete(place_to_delete)
        await session.commit()

    async with session_factory() as session:
        res = await session.execute(select(ClaimModel).where(ClaimModel.id == claim_id))
        surviving_claim = res.scalar_one_or_none()
        assert surviving_claim is not None
        assert surviving_claim.place_id is None  # SET NULL verified

    # 2. Delete claim -> claim_evidence junction row cascades, but observation survives
    async with session_factory() as session:
        res = await session.execute(select(ClaimModel).where(ClaimModel.id == claim_id))
        claim_to_delete = res.scalar_one_or_none()
        assert claim_to_delete is not None
        await session.delete(claim_to_delete)
        await session.commit()

    async with session_factory() as session:
        # claim_evidence deleted
        res = await session.execute(
            select(ClaimEvidenceModel).where(ClaimEvidenceModel.claim_id == claim_id)
        )
        assert res.scalar_one_or_none() is None

        # observation still intact
        res = await session.execute(
            select(SourceObservationModel).where(SourceObservationModel.id == obs_id)
        )
        assert res.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_contradictory_claims_preservation(session_factory: Any) -> None:
    """AC-11 & AC-8: Conflicting claims are preserved side by side as CONFLICTED."""
    async with session_factory() as session:
        execution_id = await _create_test_execution(session)

        claim1_id = uuid.uuid4()
        claim2_id = uuid.uuid4()
        subject_id = f"kolkata-central-avenue-closure-{uuid.uuid4().hex[:8]}"

        # Source A claims road is open
        claim1 = ClaimModel(
            id=claim1_id,
            execution_id=execution_id,
            subject_id=subject_id,
            category=ClaimCategory.RESTRICTION,
            typed_value={"road_open": True, "source": "travel-blog-2025"},
            verification=VerificationStatus.CONFLICTED,
            contradictions=[
                {"conflicting_claim_id": str(claim2_id), "reason": "Official advisory asserts closure"}
            ],
        )

        # Source B claims road is closed
        claim2 = ClaimModel(
            id=claim2_id,
            execution_id=execution_id,
            subject_id=subject_id,
            category=ClaimCategory.RESTRICTION,
            typed_value={"road_open": False, "source": "kolkata-police-traffic-advisory-2026"},
            verification=VerificationStatus.CONFLICTED,
            contradictions=[
                {"conflicting_claim_id": str(claim1_id), "reason": "Secondary blog asserts road is open"}
            ],
        )

        session.add_all([claim1, claim2])
        await session.commit()

    # Verify both rows coexist with distinct IDs and conflict annotations
    async with session_factory() as session:
        res = await session.execute(
            select(ClaimModel).where(ClaimModel.subject_id == subject_id).order_by(ClaimModel.created_at)
        )
        conflicted_claims = res.scalars().all()
        assert len(conflicted_claims) == 2
        assert {c.id for c in conflicted_claims} == {claim1_id, claim2_id}
        assert all(c.verification == VerificationStatus.CONFLICTED for c in conflicted_claims)
        assert conflicted_claims[0].typed_value["road_open"] is True
        assert conflicted_claims[1].typed_value["road_open"] is False


@pytest.mark.asyncio
async def test_raw_sql_update_triggers_updated_at(session_factory: Any) -> None:
    """AC-5 & AC-8: Direct raw SQL update triggers BEFORE UPDATE trigger advancing updated_at."""
    async with session_factory() as session:
        place_id = uuid.uuid4()
        place = CanonicalPlaceModel(
            id=place_id,
            primary_name="Trigger Test Place",
            locality="Kolkata",
            point=WKTElement("POINT(88.3639 22.5726)", srid=4326),
        )
        session.add(place)
        await session.commit()

    async with session_factory() as session:
        res = await session.execute(
            select(CanonicalPlaceModel).where(CanonicalPlaceModel.id == place_id)
        )
        loaded = res.scalar_one()
        initial_updated_at = loaded.updated_at

        # Execute direct raw SQL update without ORM onupdate involvement
        await session.execute(
            sa.text(
                "UPDATE canonical_places SET primary_name = 'Trigger Updated Place' WHERE id = :id"
            ),
            {"id": place_id},
        )
        await session.commit()

    async with session_factory() as session:
        res = await session.execute(
            select(CanonicalPlaceModel).where(CanonicalPlaceModel.id == place_id)
        )
        loaded = res.scalar_one()
        assert loaded.primary_name == "Trigger Updated Place"
        assert loaded.updated_at >= initial_updated_at
