"""Integration tests for claim ingestion service conforming to spec 0007 AC-13 and AC-14.

Verifies:
1. Genuine supporting evidence with independent coordinates promotes to VERIFIED existence.
2. Name in URL only does not match or promote candidate existence.
3. Unrelated nearby coordinates with different place name do not match.
4. Same-name places located far apart (> 1.0 km) do not match.
5. Casual passing mentions without coordinates or structured metadata do not match.
6. Observations without independent coordinates do not use candidate's own coordinates as proof,
   keeping the candidate UNVERIFIED in editorial recommendations.
7. Unavailable, blocked, or error observations are skipped.
8. Accommodation extractions strictly preserve INDICATIVE_SEARCH classification.
9. Claim ingestion is idempotent across repeated invocations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from contracts.candidate import (
    CandidateExistenceStatus,
    CoordinatePoint,
    RegisteredCandidate,
)
from contracts.evidence import EvidenceClass
from functions.api.models import TripModel
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.claim_ingestion import (
    ingest_claims_from_observations,
)
from functions.orchestrator.evidence.eligibility import (
    DeterministicEvidenceEligibilityGate,
)
from functions.orchestrator.evidence.models import ClaimEvidenceModel, ClaimModel
from functions.orchestrator.evidence.schemas import (
    ClaimCategory,
    EvidenceSupportType,
    VerificationStatus,
)
from functions.orchestrator.execution.models import ExecutionModel


async def _create_test_execution(session: Any) -> tuple[uuid.UUID, uuid.UUID]:
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
        workflow="TRAVEL",
        status="RUNNING",
        context_version=1,
        deadline_at=now + timedelta(hours=2),
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    await session.flush()
    return execution_id, owner_id


@pytest.mark.asyncio
class TestClaimIngestionIntegration:
    """Tests that claims are correctly derived from collected observations and verified by the eligibility gate."""

    async def test_ingest_claims_from_matching_observations(self, session_factory: Any) -> None:
        """Genuine supporting observation with independent coordinates promotes to VERIFIED existence."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Insert a genuine collected source observation with independent coordinates
            obs_id = uuid.uuid4()
            obs = SourceObservationModel(
                id=obs_id,
                execution_id=exec_id,
                source_url="https://wbtourism.gov.in/belur-math",
                canonical_url="https://wbtourism.gov.in/belur-math",
                publisher="West Bengal Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now - timedelta(days=10),
                excerpt_text="Belur Math is a historic temple and monastery in Kolkata open daily from 10:00 to 17:00.",
                excerpt_locator="p.details",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={
                    "category": "attraction",
                    "status": "AVAILABLE",
                    "title": "Belur Math",
                    "latitude": 22.6322,
                    "longitude": 88.3564,
                },
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            candidate = RegisteredCandidate(
                primary_name="Belur Math",
                category="attraction",
                point=CoordinatePoint(lat=22.6322, lng=88.3564),
            )

            # Ingest claims from observation
            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            assert len(claims) == 1
            claim = claims[0]
            assert claim.subject_id == "belur math"
            assert claim.category == ClaimCategory.ATTRACTION.value
            assert claim.verification == VerificationStatus.VERIFIED
            # Verified coordinates derived from observation, not candidate
            assert claim.geographic_scope == {"lat": 22.6322, "lng": 88.3564}

            # Assert claim-evidence link
            link_stmt = select(ClaimEvidenceModel).where(ClaimEvidenceModel.claim_id == claim.id)
            links = list((await session.execute(link_stmt)).scalars().all())
            assert len(links) == 1
            assert links[0].observation_id == obs_id
            assert links[0].support_type == EvidenceSupportType.SUPPORTS

            # Verify through DeterministicEvidenceEligibilityGate
            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )

            assert len(report.eligible_stops) == 1
            assert report.eligible_stops[0].primary_name == "Belur Math"
            assert report.eligible_stops[0].existence_status == CandidateExistenceStatus.VERIFIED
            assert len(report.editorial_recommendations) == 0

    async def test_same_name_places_far_apart_stays_unverified(self, session_factory: Any) -> None:
        """Observation with matching name but coordinates far apart (> 1.0 km) must not verify candidate."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Observation for a Kali Temple located in Chennai (13.0827, 80.2707)
            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://tamilnadutourism.tn.gov.in/kali-temple",
                canonical_url="https://tamilnadutourism.tn.gov.in/kali-temple",
                publisher="Tamil Nadu Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Kali Temple is a peaceful place of worship in Chennai.",
                excerpt_locator="div.content",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={
                    "category": "attraction",
                    "status": "AVAILABLE",
                    "title": "Kali Temple",
                    "latitude": 13.0827,
                    "longitude": 80.2707,
                },
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            # Candidate is in Kolkata (22.5000, 88.3000), over 1300 km away
            candidate = RegisteredCandidate(
                primary_name="Kali Temple",
                category="attraction",
                point=CoordinatePoint(lat=22.5000, lng=88.3000),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Must NOT create a claim or support link for the Kolkata candidate
            assert len(claims) == 0

            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )

            assert len(report.eligible_stops) == 0
            assert len(report.editorial_recommendations) == 1
            assert (
                report.editorial_recommendations[0].existence_status
                == CandidateExistenceStatus.UNVERIFIED
            )

    async def test_nearby_unrelated_places_stays_unverified(self, session_factory: Any) -> None:
        """Observation with coordinates nearby (< 1.0 km) but different name must not verify candidate."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Observation for Sharma Sweets located near Victoria Memorial (~30m away)
            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://kolkatafoodies.com/sharma-sweets",
                canonical_url="https://kolkatafoodies.com/sharma-sweets",
                publisher="Kolkata Foodies",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Place: Sharma Sweets | Authentic Bengali sweets and hot samosas.",
                excerpt_locator="serpapi:places[0]",
                extraction_method="SERPAPI_SEARCH",
                artifact_ref={
                    "category": "restaurant",
                    "status": "AVAILABLE",
                    "title": "Sharma Sweets",
                    "latitude": 22.5450,
                    "longitude": 88.3428,
                },
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            # Candidate is Victoria Memorial
            candidate = RegisteredCandidate(
                primary_name="Victoria Memorial",
                category="attraction",
                point=CoordinatePoint(lat=22.5448, lng=88.3426),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Unrelated nearby place coordinates must not create an existence claim for candidate
            assert len(claims) == 0

            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )

            assert len(report.eligible_stops) == 0
            assert len(report.editorial_recommendations) == 1
            assert (
                report.editorial_recommendations[0].existence_status
                == CandidateExistenceStatus.UNVERIFIED
            )

    async def test_casual_name_mention_stays_unverified(self, session_factory: Any) -> None:
        """A casual passing mention in travel prose without coordinates must not verify existence."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Observation merely mentions name casually in passing
            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://travel-blog.com/kolkata-walk",
                canonical_url="https://travel-blog.com/kolkata-walk",
                publisher="Travel Blog",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="We walked past Casual Name Mention Spot on our way to the hotel.",
                excerpt_locator="p.travelogue",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={"category": "attraction", "status": "AVAILABLE"},
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            candidate = RegisteredCandidate(
                primary_name="Casual Name Mention Spot",
                category="attraction",
                point=CoordinatePoint(lat=22.5000, lng=88.3000),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Casual mention does not promote to a SUPPORTS existence claim
            assert len(claims) == 0

            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )

            assert len(report.eligible_stops) == 0
            assert len(report.editorial_recommendations) == 1
            assert (
                report.editorial_recommendations[0].existence_status
                == CandidateExistenceStatus.UNVERIFIED
            )

    async def test_url_name_mention_only_stays_unverified(self, session_factory: Any) -> None:
        """A candidate name in a URL or domain without substantive content does not verify existence."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # URL contains name, but content is generic travel prose without place entity
            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://example.com/urlmatchplace/details",
                canonical_url="https://example.com/urlmatchplace/details",
                publisher="Example Publisher",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Generic travel article containing general travel recommendations.",
                excerpt_locator="body",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={"category": "attraction", "status": "AVAILABLE"},
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            candidate = RegisteredCandidate(
                primary_name="Url Match Place",
                category="attraction",
                point=CoordinatePoint(lat=22.5000, lng=88.3000),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Name in URL alone must never match or create claims
            assert len(claims) == 0

            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )

            assert len(report.eligible_stops) == 0
            assert len(report.editorial_recommendations) == 1
            assert (
                report.editorial_recommendations[0].existence_status
                == CandidateExistenceStatus.UNVERIFIED
            )

    async def test_text_only_observation_does_not_use_candidate_coords(
        self, session_factory: Any
    ) -> None:
        """Candidate's own coordinates are never used as proof; text-only claims stay unverified location."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Text observation describing the place but lacking independent GPS coordinates
            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://wbtourism.gov.in/historic-gate",
                canonical_url="https://wbtourism.gov.in/historic-gate",
                publisher="West Bengal Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Historic Gate is an ancient stone monument constructed during the seventeenth century with decorative arches.",
                excerpt_locator="p.history",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={"category": "attraction", "status": "AVAILABLE"},
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            candidate = RegisteredCandidate(
                primary_name="Historic Gate",
                category="attraction",
                point=CoordinatePoint(lat=22.5000, lng=88.3000),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Claim is ingested from detailed text, but geographic_scope is None (not candidate's point)
            assert len(claims) == 1
            assert claims[0].geographic_scope is None

            # Because no independent coordinates corroborate the candidate location, gate keeps it unverified
            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )

            assert len(report.eligible_stops) == 0
            assert len(report.editorial_recommendations) == 1
            assert (
                report.editorial_recommendations[0].existence_status
                == CandidateExistenceStatus.UNVERIFIED
            )

    async def test_ingest_claims_preserves_indicative_search_for_lodging(
        self, session_factory: Any
    ) -> None:
        """Accommodation extractions strictly preserve INDICATIVE_SEARCH and are never promoted to LIVE_OFFER."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Insert accommodation observation
            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://makemytrip.com/hotels/heritage-inn-darjeeling.html",
                canonical_url="https://makemytrip.com/hotels/heritage-inn-darjeeling.html",
                publisher="MakeMyTrip",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now - timedelta(days=2),
                excerpt_text="Heritage Inn Darjeeling offering comfortable mountain rooms from INR 3500 per night.",
                excerpt_locator="div.hotel-details",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={
                    "category": "lodging",
                    "property_name": "Heritage Inn",
                    "price_per_night": 3500,
                    "currency": "INR",
                    "status": "AVAILABLE",
                },
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[],
                owner_id=owner_id,
            )
            await session.commit()

            lodging_claims = [c for c in claims if c.category == "LODGING"]
            assert len(lodging_claims) == 1
            stay_claim = lodging_claims[0]
            assert (
                stay_claim.typed_value.get("evidence_class")
                == EvidenceClass.INDICATIVE_SEARCH.value
            )
            assert stay_claim.typed_value.get("evidence_class") != EvidenceClass.LIVE_OFFER.value

    async def test_ingest_claims_skips_needs_browser_and_errors(self, session_factory: Any) -> None:
        """NEEDS_BROWSER placeholders and error/unavailable observations are never promoted to claims."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # NEEDS_BROWSER observation
            obs_browser = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://tripadvisor.com/hotel-spa",
                canonical_url="https://tripadvisor.com/hotel-spa",
                publisher="TripAdvisor",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="[DYNAMIC_PAGE: JAVASCRIPT_REQUIRED]",
                excerpt_locator="body",
                extraction_method="NEEDS_BROWSER",
                artifact_ref={
                    "category": "lodging",
                    "browser_reason": "DYNAMIC_JAVASCRIPT_REQUIRED",
                },
                created_at=now,
            )
            # Error / unavailable observation
            obs_error = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://bad-domain.com/details",
                canonical_url="https://bad-domain.com/details",
                publisher="Bad Domain",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Failed loading site",
                excerpt_locator="body",
                extraction_method="SCRAPLING_HTTP",
                artifact_ref={
                    "category": "attraction",
                    "status": "UNAVAILABLE",
                    "error": "HTTP 503",
                },
                created_at=now,
            )
            session.add_all([obs_browser, obs_error])
            await session.commit()

            cand = RegisteredCandidate(
                primary_name="Hotel Spa",
                category="lodging",
                point=CoordinatePoint(lat=27.0, lng=88.0),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[cand],
                owner_id=owner_id,
            )
            await session.commit()

            assert len(claims) == 0

    async def test_ingest_claims_idempotent(self, session_factory: Any) -> None:
        """Ingesting claims multiple times from the same observations produces zero duplicates."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://wbtourism.gov.in/darjeeling-mall",
                canonical_url="https://wbtourism.gov.in/darjeeling-mall",
                publisher="West Bengal Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Darjeeling Mall is a pedestrian square in Chowrasta.",
                excerpt_locator="div.center",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={
                    "category": "attraction",
                    "status": "AVAILABLE",
                    "title": "Darjeeling Mall",
                    "latitude": 27.0435,
                    "longitude": 88.2673,
                },
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            cand = RegisteredCandidate(
                primary_name="Darjeeling Mall",
                category="attraction",
                point=CoordinatePoint(lat=27.0435, lng=88.2673),
            )

            # First run
            first_run = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[cand],
                owner_id=owner_id,
            )
            await session.commit()
            assert len(first_run) == 1

            # Second run
            second_run = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[cand],
                owner_id=owner_id,
            )
            await session.commit()
            # Does not duplicate or error
            assert len(second_run) == 0

    async def test_shorter_candidate_matching_longer_title_stays_unverified(
        self, session_factory: Any
    ) -> None:
        """Shorter candidate name matching a different longer place title must not verify candidate."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Observation describes a cafe inside or near the museum: 'National Museum Cafe'
            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://kolkatacafe.com/national-museum-cafe",
                canonical_url="https://kolkatacafe.com/national-museum-cafe",
                publisher="Kolkata Cafe Guide",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Place: National Museum Cafe | Cozy espresso bar and bakery.",
                excerpt_locator="serpapi:places[0]",
                extraction_method="SERPAPI_SEARCH",
                artifact_ref={
                    "category": "restaurant",
                    "status": "AVAILABLE",
                    "title": "National Museum Cafe",
                    "latitude": 22.5580,
                    "longitude": 88.3510,
                },
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            # Shorter candidate name: 'National Museum'
            candidate = RegisteredCandidate(
                primary_name="National Museum",
                category="attraction",
                point=CoordinatePoint(lat=22.5580, lng=88.3510),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Must NOT promote the shorter candidate from a longer distinct title
            assert len(claims) == 0

            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )
            assert len(report.eligible_stops) == 0
            assert len(report.editorial_recommendations) == 1
            assert (
                report.editorial_recommendations[0].existence_status
                == CandidateExistenceStatus.UNVERIFIED
            )

    async def test_permanently_closed_or_defunct_place_stays_unverified(
        self, session_factory: Any
    ) -> None:
        """Observation indicating a place is permanently closed or defunct must not support existence."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Observation for a permanently closed heritage palace
            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://wbtourism.gov.in/old-heritage-palace",
                canonical_url="https://wbtourism.gov.in/old-heritage-palace",
                publisher="West Bengal Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Place: Old Heritage Palace | The historical palace is permanently closed to visitors.",
                excerpt_locator="div.notice",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={
                    "category": "attraction",
                    "business_status": "CLOSED_PERMANENTLY",
                    "title": "Old Heritage Palace",
                    "latitude": 22.5680,
                    "longitude": 88.3520,
                },
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            candidate = RegisteredCandidate(
                primary_name="Old Heritage Palace",
                category="attraction",
                point=CoordinatePoint(lat=22.5680, lng=88.3520),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Must NOT ingest active existence claim for a closed place
            assert len(claims) == 0

            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )
            assert len(report.eligible_stops) == 0
            assert len(report.editorial_recommendations) == 1
            assert (
                report.editorial_recommendations[0].existence_status
                == CandidateExistenceStatus.UNVERIFIED
            )

    async def test_coordinates_and_metadata_describing_different_entity_stays_unverified(
        self, session_factory: Any
    ) -> None:
        """Observation whose coordinates or metadata describe a different entity must not verify candidate."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Observation for Victoria Memorial Bus Stand (transit stop, not the monument)
            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://calcuttatransit.org/vm-bus-stand",
                canonical_url="https://calcuttatransit.org/vm-bus-stand",
                publisher="Calcutta Transit",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Place: Victoria Memorial Bus Stand | Busy transit stop on Queens Way.",
                excerpt_locator="serpapi:places[0]",
                extraction_method="SERPAPI_SEARCH",
                artifact_ref={
                    "category": "transit_station",
                    "types": ["transit_station", "bus_stop"],
                    "status": "AVAILABLE",
                    "title": "Victoria Memorial Bus Stand",
                    "latitude": 22.5448,
                    "longitude": 88.3425,
                },
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            # Candidate is the attraction 'Victoria Memorial'
            candidate = RegisteredCandidate(
                primary_name="Victoria Memorial",
                category="attraction",
                point=CoordinatePoint(lat=22.5448, lng=88.3425),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Transit station metadata must not promote the attraction candidate
            assert len(claims) == 0

            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )
            assert len(report.eligible_stops) == 0
            assert len(report.editorial_recommendations) == 1
            assert (
                report.editorial_recommendations[0].existence_status
                == CandidateExistenceStatus.UNVERIFIED
            )

    async def test_search_title_with_unrelated_page_creates_no_page_supports_promotion(
        self, session_factory: Any
    ) -> None:
        """Regression 1: Search title A with unrelated page B must not promote candidate via page SUPPORTS link."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Search observation for Victoria Memorial (Title A)
            search_obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://wbtourism.gov.in/victoria-memorial",
                canonical_url="https://wbtourism.gov.in/victoria-memorial",
                publisher="West Bengal Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Place: Victoria Memorial | Historic marble monument in Kolkata.",
                excerpt_locator="serpapi:places[0]",
                extraction_method="SERPAPI_SEARCH",
                artifact_ref={
                    "category": "attraction",
                    "title": "Victoria Memorial",
                    "latitude": 22.5448,
                    "longitude": 88.3425,
                    "status": "active",
                },
                created_at=now,
            )
            session.add(search_obs)

            # Scraped page observation from target URL describing Science City (Title B), not Victoria Memorial
            page_obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://sciencecitykolkata.org/overview",
                canonical_url="https://sciencecitykolkata.org/overview",
                publisher="Science City Kolkata",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Science City in Kolkata features the Space Odyssey, Dynamotion Hall, and Earth Exploration Hall.",
                excerpt_locator="main:text",
                extraction_method="SCRAPLING_HTTP",
                artifact_ref={
                    "category": "attraction",
                    "page_title": "Science City Kolkata",
                    "title": "Science City Kolkata",
                    "search_selected_title": "Victoria Memorial",
                },
                created_at=now,
            )
            session.add(page_obs)
            await session.commit()

            candidate = RegisteredCandidate(
                primary_name="Victoria Memorial",
                category="attraction",
                point=CoordinatePoint(lat=22.5448, lng=88.3425),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Active claim is ingested for Victoria Memorial
            assert len(claims) >= 1
            vm_claim = claims[0]
            assert vm_claim.subject_id == "victoria memorial"

            # Page observation for Science City must NOT have a SUPPORTS link to Victoria Memorial claim
            page_supports_stmt = select(ClaimEvidenceModel).where(
                ClaimEvidenceModel.observation_id == page_obs.id,
                ClaimEvidenceModel.support_type == "SUPPORTS",
            )
            page_supports_links = list(
                (await session.execute(page_supports_stmt)).scalars().all()
            )
            assert len(page_supports_links) == 0

            # Search observation DOES have a SUPPORTS link to Victoria Memorial claim
            search_supports_stmt = select(ClaimEvidenceModel).where(
                ClaimEvidenceModel.observation_id == search_obs.id,
                ClaimEvidenceModel.claim_id == vm_claim.id,
                ClaimEvidenceModel.support_type == "SUPPORTS",
            )
            search_supports_links = list(
                (await session.execute(search_supports_stmt)).scalars().all()
            )
            assert len(search_supports_links) == 1

    async def test_search_title_with_page_without_identity_evidence_stays_unverified(
        self, session_factory: Any
    ) -> None:
        """Regression 2: Search title with page lacking identity evidence must remain UNVERIFIED."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Search observation has no verified coordinates or independent proof
            search_obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://search.example.com/fictional-cloud",
                canonical_url="https://search.example.com/fictional-cloud",
                publisher="Search Engine",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="A casual search query for fictional cloud observatory in the sky.",
                excerpt_locator="serpapi:organic[0]",
                extraction_method="SERPAPI_SEARCH",
                artifact_ref={
                    "search_selected_title": "Fictional Cloud Observatory",
                },
                created_at=now,
            )
            session.add(search_obs)

            # Scraped page observation has generic portal content without identity evidence
            page_obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://portal.example.com/home",
                canonical_url="https://portal.example.com/home",
                publisher="Generic Portal",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Welcome to our municipal web portal. View public services, announcements, and directories.",
                excerpt_locator="main:text",
                extraction_method="SCRAPLING_HTTP",
                artifact_ref={
                    "category": "general",
                    "page_title": "",
                    "search_selected_title": "Fictional Cloud Observatory",
                },
                created_at=now,
            )
            session.add(page_obs)
            await session.commit()

            candidate = RegisteredCandidate(
                primary_name="Fictional Cloud Observatory",
                category="attraction",
                point=CoordinatePoint(lat=22.5448, lng=88.3425),
            )

            _ = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # No claims or unverified claims must not promote candidate to solver
            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )
            assert len(report.eligible_stops) == 0
            assert len(report.editorial_recommendations) == 1
            assert (
                report.editorial_recommendations[0].existence_status
                == CandidateExistenceStatus.UNVERIFIED
            )

    async def test_matching_page_with_genuine_supporting_evidence_eligible(
        self, session_factory: Any
    ) -> None:
        """Regression 3: Matching page with genuine supporting evidence becomes eligible when policy allows."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Search observation with verified coordinates
            search_obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://wbtourism.gov.in/prinsep-ghat",
                canonical_url="https://wbtourism.gov.in/prinsep-ghat",
                publisher="West Bengal Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Place: Prinsep Ghat Memorial | Historic riverside monument along Strand Road.",
                excerpt_locator="serpapi:places[0]",
                extraction_method="SERPAPI_SEARCH",
                artifact_ref={
                    "category": "attraction",
                    "title": "Prinsep Ghat Memorial",
                    "latitude": 22.5550,
                    "longitude": 88.3320,
                    "status": "active",
                },
                created_at=now,
            )
            session.add(search_obs)

            # Scraped page observation with genuine matching identity and substantive excerpt
            page_obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://wbtourism.gov.in/prinsep-ghat/details",
                canonical_url="https://wbtourism.gov.in/prinsep-ghat/details",
                publisher="West Bengal Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Prinsep Ghat Memorial is a majestic Greek Gothic style memorial built in 1843 along the Hooghly river.",
                excerpt_locator="main:text",
                extraction_method="SCRAPLING_HTTP",
                artifact_ref={
                    "category": "attraction",
                    "page_title": "Prinsep Ghat Memorial",
                    "title": "Prinsep Ghat Memorial",
                    "search_selected_title": "Prinsep Ghat Memorial",
                },
                created_at=now,
            )
            session.add(page_obs)
            await session.commit()

            candidate = RegisteredCandidate(
                primary_name="Prinsep Ghat Memorial",
                category="attraction",
                point=CoordinatePoint(lat=22.5550, lng=88.3320),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            assert len(claims) >= 1
            jp_claim = claims[0]

            # Page observation genuinely matches and has a SUPPORTS link
            page_supports_stmt = select(ClaimEvidenceModel).where(
                ClaimEvidenceModel.observation_id == page_obs.id,
                ClaimEvidenceModel.claim_id == jp_claim.id,
                ClaimEvidenceModel.support_type == "SUPPORTS",
            )
            page_supports_links = list(
                (await session.execute(page_supports_stmt)).scalars().all()
            )
            assert len(page_supports_links) == 1

            # Candidate evaluates to VERIFIED and enters eligible stops
            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )
            assert len(report.eligible_stops) == 1
            assert (
                report.eligible_stops[0].existence_status
                == CandidateExistenceStatus.VERIFIED
            )

    async def test_conflicting_page_and_search_metadata_preserves_conflict_honestly(
        self, session_factory: Any
    ) -> None:
        """Regression 4: Conflicting page and search metadata preserves conflict honestly as CONFLICTED."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Search observation asserts active existence
            search_obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://wbtourism.gov.in/heritage-museum",
                canonical_url="https://wbtourism.gov.in/heritage-museum",
                publisher="West Bengal Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Place: Heritage Museum | Open daily showcasing historical artifacts.",
                excerpt_locator="serpapi:places[0]",
                extraction_method="SERPAPI_SEARCH",
                artifact_ref={
                    "category": "attraction",
                    "title": "Heritage Museum",
                    "latitude": 22.5600,
                    "longitude": 88.3500,
                    "status": "active",
                },
                created_at=now,
            )
            session.add(search_obs)

            # Scraped page observation for the place asserts it is permanently closed
            page_obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://heritagemuseum.org/closure-notice",
                canonical_url="https://heritagemuseum.org/closure-notice",
                publisher="Heritage Museum Official",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Heritage Museum is permanently closed to all visitors following structural condemnation.",
                excerpt_locator="main:text",
                extraction_method="SCRAPLING_HTTP",
                artifact_ref={
                    "category": "attraction",
                    "page_title": "Heritage Museum",
                    "title": "Heritage Museum",
                    "business_status": "CLOSED_PERMANENTLY",
                    "status": "closed",
                    "search_selected_title": "Heritage Museum",
                },
                created_at=now,
            )
            session.add(page_obs)
            await session.commit()

            candidate = RegisteredCandidate(
                primary_name="Heritage Museum",
                category="attraction",
                point=CoordinatePoint(lat=22.5600, lng=88.3500),
            )

            _ = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Conflict is preserved honestly: claims are marked CONFLICTED
            claims_stmt = select(ClaimModel).where(
                ClaimModel.execution_id == exec_id,
                ClaimModel.subject_id == "heritage museum",
            )
            persisted_claims = list((await session.execute(claims_stmt)).scalars().all())
            assert len(persisted_claims) >= 2
            assert any(
                c.verification == VerificationStatus.CONFLICTED for c in persisted_claims
            )

            # Eligibility gate evaluates candidate as CONTRADICTED and excludes it
            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )
            assert len(report.eligible_stops) == 0
            assert len(report.excluded_candidates) == 1
            assert (
                report.excluded_candidates[0].existence_status
                == CandidateExistenceStatus.CONTRADICTED
            )
            assert (
                "Conflicting claims"
                in report.excluded_candidates[0].diagnostics.get("contradiction_reason", "")
            )
