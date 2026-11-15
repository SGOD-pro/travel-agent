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

    async def test_accommodation_preserves_indicative_search_and_never_promotes_to_live_offer(
        self, session_factory: Any
    ) -> None:
        """Accommodation rates from search must stay INDICATIVE_SEARCH without promotion to live offers."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://makemytrip.com/hotels/kolkata-grand-hotel",
                canonical_url="https://makemytrip.com/hotels/kolkata-grand-hotel",
                publisher="MakeMyTrip",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Hotel: Park Street Heritage Grand Hotel | Indicative rate ₹3500 per night",
                excerpt_locator="hotel:card[0]",
                extraction_method="SCRAPLING_HTTP",
                artifact_ref={
                    "category": "lodging",
                    "property_name": "Park Street Heritage Grand Hotel",
                    "price_per_night": 3500.0,
                    "currency": "INR",
                    "evidence_class": EvidenceClass.INDICATIVE_SEARCH.value,
                    "is_indicative_search": True,
                    "is_verified_offer": False,
                    "booking_confirmed": False,
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

            assert len(claims) == 1
            claim = claims[0]
            assert claim.category == "LODGING"
            assert claim.subject_id == "park street heritage grand hotel"
            assert claim.typed_value["evidence_class"] == EvidenceClass.INDICATIVE_SEARCH.value
            assert claim.typed_value["price_per_night"] == 3500.0
            assert claim.typed_value["currency"] == "INR"
            assert claim.typed_value["is_indicative_search"] is True
            assert claim.typed_value["is_verified_offer"] is False
            assert claim.typed_value["booking_confirmed"] is False
            assert claim.typed_value["availability"] == "UNKNOWN"

    async def test_unpriced_accommodation_observation_stays_none_without_zero_coercion(
        self, session_factory: Any
    ) -> None:
        """Unpriced accommodation observation preserves None price without zero coercion."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://booking.com/hotels/boutique-residency-kolkata",
                canonical_url="https://booking.com/hotels/boutique-residency-kolkata",
                publisher="Booking",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Hotel: Boutique Residency Kolkata | Room rates on request",
                excerpt_locator="hotel:card[1]",
                extraction_method="SCRAPLING_HTTP",
                artifact_ref={
                    "category": "lodging",
                    "property_name": "Boutique Residency Kolkata",
                    "price_per_night": None,
                    "currency": "INR",
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

            assert len(claims) == 1
            claim = claims[0]
            assert claim.typed_value["price_per_night"] is None
            assert claim.typed_value["price_per_night"] != 0
            assert claim.typed_value["evidence_class"] is None
            assert claim.typed_value["is_indicative_search"] is False

    async def test_food_dish_distinguished_from_business_and_not_promoted_as_place_existence(
        self, session_factory: Any
    ) -> None:
        """Culinary dishes must not be promoted as physical place candidates."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Observation for physical dining business with verified coordinates
            obs_biz = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://zomato.com/kolkata/nizam-heritage-restaurant",
                canonical_url="https://zomato.com/kolkata/nizam-heritage-restaurant",
                publisher="Zomato",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Place: Nizam Heritage Restaurant | Iconic dining on Hogg Street Kolkata",
                excerpt_locator="serpapi:places[0]",
                extraction_method="SERPAPI_SEARCH",
                artifact_ref={
                    "title": "Nizam Heritage Restaurant",
                    "category": "restaurant",
                    "address": "New Market, Hogg Street, Kolkata",
                    "gps_coordinates": {"latitude": 22.5601, "longitude": 88.3522},
                },
                created_at=now,
            )
            # Observation mentioning local dish
            obs_dish = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://culinary-tours.com/bengali-specialties",
                canonical_url="https://culinary-tours.com/bengali-specialties",
                publisher="Culinary Tours",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Famous Kolkata Kathi Roll is a classic street food snack across the city.",
                excerpt_locator="p.food-guide",
                extraction_method="SCRAPLING_HTTP",
                artifact_ref={
                    "category": "cuisine",
                    "title": "Kolkata Kathi Roll",
                },
                created_at=now,
            )
            session.add_all([obs_biz, obs_dish])
            await session.commit()

            cand_dish = RegisteredCandidate(
                primary_name="Kolkata Kathi Roll",
                category="street_food",
                point=CoordinatePoint(lat=22.5600, lng=88.3520),
            )
            cand_biz = RegisteredCandidate(
                primary_name="Nizam Heritage Restaurant",
                category="restaurant",
                point=CoordinatePoint(lat=22.5601, lng=88.3522),
            )

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[cand_dish, cand_biz],
                owner_id=owner_id,
            )
            await session.commit()

            # Physical business gets verified CUISINE claim, dish is skipped
            assert len(claims) == 1
            assert claims[0].subject_id == "nizam heritage restaurant"
            assert claims[0].category == ClaimCategory.CUISINE.value

            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [cand_dish, cand_biz],
                execution_id=exec_id,
                owner_id=owner_id,
            )

            # Business is eligible stop with verified existence and provenance
            assert len(report.eligible_stops) == 1
            assert report.eligible_stops[0].primary_name == "Nizam Heritage Restaurant"
            assert report.eligible_stops[0].existence_status == CandidateExistenceStatus.VERIFIED
            assert report.eligible_stops[0].provenance["uncertainty"] == "VERIFIED"
            assert len(report.eligible_stops[0].provenance["source_urls"]) >= 1

            # Dish stays unverified in editorial recommendations
            assert len(report.editorial_recommendations) == 1
            assert report.editorial_recommendations[0].primary_name == "Kolkata Kathi Roll"
            assert report.editorial_recommendations[0].existence_status == CandidateExistenceStatus.UNVERIFIED
            assert report.editorial_recommendations[0].provenance["uncertainty"] == "EDITORIAL_DISCOVERY"

    async def test_unverified_and_unrelated_business_not_presented_as_confirmed_recommendation(
        self, session_factory: Any
    ) -> None:
        """Unrelated businesses and unverified dining places must not become confirmed stops."""
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)

            cand_unrelated = RegisteredCandidate(
                primary_name="Kolkata Commercial Office Tower",
                category="corporate",
                point=CoordinatePoint(lat=22.5700, lng=88.3600),
            )
            cand_unverified_dining = RegisteredCandidate(
                primary_name="Unverified Street Cafe",
                category="restaurant",
                point=CoordinatePoint(lat=22.5710, lng=88.3610),
            )

            # Ingest with no matching observations in database
            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[cand_unrelated, cand_unverified_dining],
                owner_id=owner_id,
            )
            assert len(claims) == 0

            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                [cand_unrelated, cand_unverified_dining],
                execution_id=exec_id,
                owner_id=owner_id,
            )

            # Zero confirmed stops
            assert len(report.eligible_stops) == 0
            # Both remain in editorial recommendations with unverified status
            assert len(report.editorial_recommendations) == 2
            for rec in report.editorial_recommendations:
                assert rec.existence_status == CandidateExistenceStatus.UNVERIFIED
                assert rec.provenance["uncertainty"] == "EDITORIAL_DISCOVERY"
                assert "existence_unverified" in rec.provenance["unavailable_details"]

    async def test_food_and_accommodation_research_no_evidence_returns_unavailable_not_completed(
        self, session_factory: Any
    ) -> None:
        """Missing food and lodging evidence produces honest UNAVAILABLE status, never COMPLETED."""
        from functions.orchestrator.graphs.trip_planner.graph import (
            _build_accommodation_research,
            _build_food_research,
        )

        empty_state: dict[str, Any] = {
            "eligible_stops": [],
            "editorial_recommendations": [],
            "observations": [],
            "task_outputs": {},
            "brief": {"destinations": [{"name": "Kolkata"}]},
        }

        food_res = _build_food_research(empty_state)
        assert food_res["status"] == "UNAVAILABLE"
        assert food_res["status"] != "COMPLETED"
        assert "No usable source-backed food or dining evidence retrieved" in str(food_res["reason"])
        assert food_res["local_dishes"] == []
        assert food_res["dining_recommendations"] == []

        accom_res = _build_accommodation_research(empty_state)
        assert accom_res["status"] == "UNAVAILABLE"
        assert accom_res["status"] != "COMPLETED"
        assert "No usable source-backed accommodation evidence retrieved" in str(accom_res["reason"])
        assert accom_res["indicative_options"] == []
        assert accom_res["availability_status"] == "PRICE_UNAVAILABLE"

    async def test_food_and_accommodation_research_provider_failure_returns_unavailable(
        self, session_factory: Any
    ) -> None:
        """Provider failures return honest UNAVAILABLE status with informative diagnostic reasons."""
        from functions.orchestrator.graphs.trip_planner.graph import (
            _build_accommodation_research,
            _build_food_research,
        )

        failed_provider_state: dict[str, Any] = {
            "eligible_stops": [],
            "editorial_recommendations": [],
            "observations": [],
            "task_outputs": {
                "evidence_collection": {
                    "status": "FAILED",
                    "error": "Upstream search provider timeout",
                    "observations": [],
                }
            },
            "brief": {"destinations": [{"name": "Kolkata"}]},
        }

        food_res = _build_food_research(failed_provider_state)
        assert food_res["status"] == "UNAVAILABLE"
        assert food_res["status"] != "COMPLETED"
        assert food_res["reason"] is not None

        accom_res = _build_accommodation_research(failed_provider_state)
        assert accom_res["status"] == "UNAVAILABLE"
        assert accom_res["status"] != "COMPLETED"
        assert accom_res["reason"] is not None

    async def test_unsupported_candidate_price_kept_unknown_not_labeled_indicative_search(
        self, session_factory: Any
    ) -> None:
        """Candidate prices lacking matching source observations must not be labeled INDICATIVE_SEARCH."""
        from functions.orchestrator.graphs.trip_planner.graph import _build_accommodation_research

        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)

            cand_hotel = RegisteredCandidate(
                primary_name="Hypothetical Boutique Grand Hotel",
                category="lodging",
                point=CoordinatePoint(lat=22.5600, lng=88.3500),
            )

            cand_dict = cand_hotel.model_dump()
            cand_dict["price_estimate_inr"] = 7500

            state: dict[str, Any] = {
                "eligible_stops": [],
                "editorial_recommendations": [cand_dict],
                "observations": [],
                "task_outputs": {},
            }

            accom_res = _build_accommodation_research(state)
            assert accom_res["status"] == "PARTIAL"
            assert accom_res["reason"] == "Accommodation options identified without source-supported indicative rates"
            assert len(accom_res["indicative_options"]) == 1

            opt = accom_res["indicative_options"][0]
            assert opt["property_name"] == "Hypothetical Boutique Grand Hotel"
            assert opt["price_per_night"] is None
            assert opt["currency"] is None
            assert opt["evidence_class"] is None
            assert opt["is_indicative_search"] is False
            assert opt["is_verified_offer"] is False
            assert opt["booking_confirmed"] is False
            assert opt["availability_status"] == "PRICE_UNAVAILABLE"
            assert opt["uncertainty"] == "UNVERIFIED"
            assert "price_unsupported_by_source" in opt["unavailable_details"]

    async def test_valid_source_backed_dish_preserves_source_provenance_without_region_override(
        self, session_factory: Any
    ) -> None:
        """Dishes trace to source observations without region overrides or canned descriptions."""
        from functions.orchestrator.evidence.place_extraction import PlaceExtractionService

        now = datetime.now(UTC)
        obs_id_generic = uuid.uuid4()
        obs_id_regional = uuid.uuid4()

        obs_generic = SourceObservationModel(
            id=obs_id_generic,
            execution_id=uuid.uuid4(),
            source_url="https://food-gazette.com/delicacies",
            canonical_url="https://food-gazette.com/delicacies",
            publisher="Food Gazette",
            content_hash="generic_dish_hash_0123456789abcdef",
            retrieved_at=now,
            published_at=now,
            excerpt_text="The traditional feast features fragrant biryani cooked in sealed earthenware vessels.",
            excerpt_locator="article:body[0]",
            extraction_method="STATIC_SCRAPLING",
            artifact_ref={"category": "culinary"},
            created_at=now,
        )

        obs_regional = SourceObservationModel(
            id=obs_id_regional,
            execution_id=uuid.uuid4(),
            source_url="https://streetfood-atlas.org/kolkata-kathi",
            canonical_url="https://streetfood-atlas.org/kolkata-kathi",
            publisher="Street Food Atlas",
            content_hash="regional_dish_hash_0123456789abcdef",
            retrieved_at=now,
            published_at=now,
            excerpt_text="Kolkata Kathi Roll originated at historic dining stalls in central Kolkata.",
            excerpt_locator="article:body[1]",
            extraction_method="STATIC_SCRAPLING",
            artifact_ref={"category": "culinary"},
            created_at=now,
        )

        dishes = PlaceExtractionService.extract_local_dishes_from_observations(
            [obs_generic, obs_regional]
        )
        assert len(dishes) == 2

        dish_map = {d["name"]: d for d in dishes}

        # Generic biryani must NOT be coerced to Kolkata Biryani
        assert "Biryani" in dish_map
        biryani = dish_map["Biryani"]
        assert biryani["name"] == "Biryani"
        assert biryani["observation_id"] == str(obs_id_generic)
        assert biryani["source_url"] == "https://food-gazette.com/delicacies"
        assert biryani["retrieval_time"] is not None
        assert "fragrant biryani cooked in sealed earthenware vessels" in str(biryani["description"])

        # Regional kathi roll retains Kolkata Kathi Roll because it appeared in the source text
        assert "Kolkata Kathi Roll" in dish_map
        kathi = dish_map["Kolkata Kathi Roll"]
        assert kathi["observation_id"] == str(obs_id_regional)
        assert kathi["source_url"] == "https://streetfood-atlas.org/kolkata-kathi"
        assert kathi["retrieval_time"] is not None
        assert "Kolkata Kathi Roll originated at historic dining stalls" in str(kathi["description"])

    async def test_valid_indicative_lodging_rate_labeled_indicative_search_with_unconfirmed_booking(
        self, session_factory: Any
    ) -> None:
        """Source backed lodging rates are labeled INDICATIVE_SEARCH with unconfirmed booking status."""
        from functions.orchestrator.graphs.trip_planner.graph import _build_accommodation_research

        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://verified-booking-aggregator.com/the-oberoi-grand",
                canonical_url="https://verified-booking-aggregator.com/the-oberoi-grand",
                publisher="Aggregator",
                content_hash="lodging_rate_hash_0123456789abcdef",
                retrieved_at=now,
                published_at=now,
                excerpt_text="The Oberoi Grand Kolkata | Indicative standard room rate ₹9500 per night",
                excerpt_locator="hotel:card[0]",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={
                    "category": "lodging",
                    "property_name": "The Oberoi Grand Kolkata",
                    "price_per_night": 9500,
                    "currency": "INR",
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
            assert len(claims) == 1
            claim = claims[0]
            assert claim.category == "LODGING"
            assert claim.typed_value["evidence_class"] == EvidenceClass.INDICATIVE_SEARCH.value
            assert claim.typed_value["price_per_night"] == 9500
            assert claim.typed_value["currency"] == "INR"
            assert claim.typed_value["observation_id"] == str(obs.id)
            assert claim.typed_value["source_url"] == "https://verified-booking-aggregator.com/the-oberoi-grand"
            assert claim.typed_value["is_indicative_search"] is True
            assert claim.typed_value["is_verified_offer"] is False
            assert claim.typed_value["booking_confirmed"] is False

            state: dict[str, Any] = {
                "eligible_stops": [],
                "editorial_recommendations": [],
                "observations": [obs],
                "task_outputs": {},
            }
            accom_res = _build_accommodation_research(state)
            assert accom_res["status"] == "COMPLETED"
            assert accom_res["reason"] is None
            assert len(accom_res["indicative_options"]) == 1

            opt = accom_res["indicative_options"][0]
            assert opt["property_name"] == "The Oberoi Grand Kolkata"
            assert opt["price_per_night"] == 9500
            assert opt["currency"] == "INR"
            assert opt["observation_id"] == str(obs.id)
            assert opt["evidence_class"] == EvidenceClass.INDICATIVE_SEARCH.value
            assert opt["is_indicative_search"] is True
            assert opt["is_verified_offer"] is False
            assert opt["booking_confirmed"] is False
            assert opt["availability_status"] == "INDICATIVE_SEARCH_ONLY"
            assert "live_room_availability_unverified" in opt["unavailable_details"]
            assert "booking_unconfirmed" in opt["unavailable_details"]
            assert opt["source_url"] == "https://verified-booking-aggregator.com/the-oberoi-grand"

    async def test_accommodation_price_attribution_rejects_similar_different_property_names(
        self, session_factory: Any
    ) -> None:
        """Regression 1: Similar but different property names do not share indicative rates through proposal path."""
        from functions.orchestrator.graphs.trip_planner.graph import synthesize_proposal_node

        cand = {
            "category": "lodging",
            "primary_name": "Hotel Green Palace",
            "point": {"lat": 22.5726, "lng": 88.3639},
            "provenance": {
                "source_urls": ["https://hotels.example.com/green-palace"],
                "retrieval_time": "2026-11-15T12:00:00Z",
            },
        }

        now = datetime.now(UTC)
        obs_annex = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            source_url="https://hotels.example.com/green-palace-annex",
            canonical_url="https://hotels.example.com/green-palace-annex",
            publisher="Aggregator",
            content_hash="hash_green_palace_annex_12345",
            retrieved_at=now,
            published_at=now,
            excerpt_text="Hotel Green Palace Annex Kolkata | Room rate 6000 INR per night",
            excerpt_locator="hotel:card[0]",
            extraction_method="STATIC_SCRAPLING",
            artifact_ref={
                "category": "lodging",
                "property_name": "Hotel Green Palace Annex",
                "price_per_night": 6000,
                "currency": "INR",
                "latitude": 22.5727,
                "longitude": 88.3640,
            },
            created_at=now,
        )

        state: dict[str, Any] = {
            "trip_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "execution_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Kolkata",
                        "latitude": 22.5726,
                        "longitude": 88.3639,
                        "stay_days": 2,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-22",
                "rooms": 1,
                "adults": 2,
                "budget_tier": "standard",
            },
            "eligible_stops": [],
            "editorial_recommendations": [cand],
            "observations": [obs_annex],
            "task_outputs": {},
        }

        proposal_res = synthesize_proposal_node(state)
        proposal = proposal_res["proposal"]
        accom_res = proposal["accommodation_research"]
        indicative_options = accom_res["indicative_options"]

        # Hotel Green Palace must not take price from Hotel Green Palace Annex
        green_palace = next(h for h in indicative_options if h["property_name"] == "Hotel Green Palace")
        assert green_palace["price_per_night"] is None
        assert green_palace["currency"] is None
        assert green_palace["observation_id"] is None
        assert green_palace["evidence_class"] is None
        assert green_palace["is_indicative_search"] is False
        assert green_palace["availability_status"] == "PRICE_UNAVAILABLE"
        assert "price_unsupported_by_source" in green_palace["unavailable_details"]

        # Hotel Green Palace Annex retains its own price
        annex = next(h for h in indicative_options if h["property_name"] == "Hotel Green Palace Annex")
        assert annex["price_per_night"] == 6000
        assert annex["currency"] == "INR"
        assert annex["observation_id"] == str(obs_annex.id)
        assert annex["evidence_class"] == EvidenceClass.INDICATIVE_SEARCH.value

    async def test_accommodation_price_attribution_rejects_same_name_at_distant_locations(
        self, session_factory: Any
    ) -> None:
        """Regression 2: Same named property at distant location does not share indicative rates."""
        from functions.orchestrator.graphs.trip_planner.graph import synthesize_proposal_node

        cand_kolkata = {
            "category": "lodging",
            "primary_name": "Grand Heritage Hotel",
            "point": {"lat": 22.5726, "lng": 88.3639},
            "provenance": {
                "source_urls": ["https://hotels.example.com/kolkata-grand-heritage"],
                "retrieval_time": "2026-11-15T12:00:00Z",
            },
        }

        now = datetime.now(UTC)
        # Same hotel name located in New Delhi (> 1000 km away)
        obs_delhi = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            source_url="https://hotels.example.com/delhi-grand-heritage",
            canonical_url="https://hotels.example.com/delhi-grand-heritage",
            publisher="Aggregator",
            content_hash="hash_grand_heritage_delhi_12345",
            retrieved_at=now,
            published_at=now,
            excerpt_text="Grand Heritage Hotel New Delhi | Indicative rate 9000 INR per night",
            excerpt_locator="hotel:card[0]",
            extraction_method="STATIC_SCRAPLING",
            artifact_ref={
                "category": "lodging",
                "property_name": "Grand Heritage Hotel",
                "price_per_night": 9000,
                "currency": "INR",
                "latitude": 28.6139,
                "longitude": 77.2090,
            },
            created_at=now,
        )

        state: dict[str, Any] = {
            "trip_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "execution_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Kolkata",
                        "latitude": 22.5726,
                        "longitude": 88.3639,
                        "stay_days": 2,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-22",
                "rooms": 1,
                "adults": 2,
                "budget_tier": "standard",
            },
            "eligible_stops": [],
            "editorial_recommendations": [cand_kolkata],
            "observations": [obs_delhi],
            "task_outputs": {},
        }

        proposal_res = synthesize_proposal_node(state)
        accom_res = proposal_res["proposal"]["accommodation_research"]
        indicative_options = accom_res["indicative_options"]

        # Grand Heritage Hotel in Kolkata must not take price from Delhi branch
        kolkata_hotel = next(h for h in indicative_options if h["property_name"] == "Grand Heritage Hotel")
        assert kolkata_hotel["price_per_night"] is None
        assert kolkata_hotel["currency"] is None
        assert kolkata_hotel["observation_id"] is None
        assert kolkata_hotel["evidence_class"] is None
        assert kolkata_hotel["is_indicative_search"] is False
        assert kolkata_hotel["availability_status"] == "PRICE_UNAVAILABLE"
        assert "price_unsupported_by_source" in kolkata_hotel["unavailable_details"]

    async def test_accommodation_price_attribution_rejects_missing_source_url(
        self, session_factory: Any
    ) -> None:
        """Regression 3: Observation without usable source URL does not produce indicative rate."""
        from functions.orchestrator.graphs.trip_planner.graph import synthesize_proposal_node

        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Observation with price but non-http unusable source URL (e.g. URN or empty)
            obs_no_url = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="urn:placeholder:unusable",
                canonical_url="urn:placeholder:unusable",
                publisher="Aggregator",
                content_hash="hash_no_url_hotel_12345",
                retrieved_at=now,
                published_at=now,
                excerpt_text="Boutique Residency Kolkata | Indicative rate 3500 INR",
                excerpt_locator="hotel:card[0]",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={
                    "category": "lodging",
                    "property_name": "Boutique Residency Kolkata",
                    "price_per_night": 3500,
                    "currency": "INR",
                    "latitude": 22.5726,
                    "longitude": 88.3639,
                },
                created_at=now,
            )
            session.add(obs_no_url)
            await session.commit()

            # Verify claim ingestion rejects price without usable URL
            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[],
                owner_id=owner_id,
            )
            assert len(claims) == 1
            claim = claims[0]
            assert claim.category == "LODGING"
            assert claim.typed_value["evidence_class"] is None
            assert claim.typed_value["price_per_night"] is None
            assert claim.typed_value["currency"] is None
            assert claim.typed_value["is_indicative_search"] is False

            # Verify proposal path rejects price without usable URL (both unusable string and None)
            cand = {
                "category": "lodging",
                "primary_name": "Boutique Residency Kolkata",
                "point": {"lat": 22.5726, "lng": 88.3639},
            }
            obs_dict_none_url = {
                "id": str(uuid.uuid4()),
                "source_url": None,
                "canonical_url": None,
                "retrieved_at": now.isoformat(),
                "artifact_ref": {
                    "category": "lodging",
                    "property_name": "Boutique Residency Kolkata",
                    "price_per_night": 3500,
                    "currency": "INR",
                    "latitude": 22.5726,
                    "longitude": 88.3639,
                },
            }
            state: dict[str, Any] = {
                "trip_id": str(uuid.uuid4()),
                "owner_id": str(owner_id),
                "execution_id": str(exec_id),
                "brief": {
                    "origin_name": "Kolkata",
                    "origin_lat": 22.5726,
                    "origin_lng": 88.3639,
                    "destinations": [
                        {
                            "name": "Kolkata",
                            "latitude": 22.5726,
                            "longitude": 88.3639,
                            "stay_days": 2,
                        }
                    ],
                    "start_date": "2026-11-20",
                    "end_date": "2026-11-22",
                    "rooms": 1,
                    "adults": 2,
                    "budget_tier": "standard",
                },
                "eligible_stops": [],
                "editorial_recommendations": [cand],
                "observations": [obs_no_url, obs_dict_none_url],
                "task_outputs": {},
            }
            proposal_res = synthesize_proposal_node(state)
            accom_res = proposal_res["proposal"]["accommodation_research"]
            indicative_options = accom_res["indicative_options"]

            opt = next(h for h in indicative_options if h["property_name"] == "Boutique Residency Kolkata")
            assert opt["price_per_night"] is None
            assert opt["currency"] is None
            assert opt["observation_id"] is None
            assert opt["evidence_class"] is None
            assert opt["is_indicative_search"] is False
            assert opt["availability_status"] == "PRICE_UNAVAILABLE"
            assert "price_unsupported_by_source" in opt["unavailable_details"]

    async def test_accommodation_price_attribution_valid_matching_property_and_source_backed_rate(
        self, session_factory: Any
    ) -> None:
        """Regression 4: Valid matching property and source backed rate are preserved with full provenance."""
        from functions.orchestrator.graphs.trip_planner.graph import synthesize_proposal_node

        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            obs_id = uuid.uuid4()
            obs = SourceObservationModel(
                id=obs_id,
                execution_id=exec_id,
                source_url="https://hotels.example.com/the-lalit-kolkata",
                canonical_url="https://hotels.example.com/the-lalit-kolkata",
                publisher="Aggregator",
                content_hash="hash_the_lalit_kolkata_12345",
                retrieved_at=now,
                published_at=now,
                excerpt_text="The Lalit Great Eastern Kolkata | Indicative standard rate 7500 INR per night",
                excerpt_locator="hotel:card[0]",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={
                    "category": "lodging",
                    "property_name": "The Lalit Great Eastern Kolkata",
                    "price_per_night": 7500,
                    "currency": "INR",
                    "latitude": 22.5698,
                    "longitude": 88.3517,
                },
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            # Verify claim ingestion
            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[],
                owner_id=owner_id,
            )
            assert len(claims) == 1
            claim = claims[0]
            assert claim.category == "LODGING"
            assert claim.typed_value["evidence_class"] == EvidenceClass.INDICATIVE_SEARCH.value
            assert claim.typed_value["price_per_night"] == 7500
            assert claim.typed_value["currency"] == "INR"
            assert claim.typed_value["observation_id"] == str(obs_id)
            assert claim.typed_value["source_url"] == "https://hotels.example.com/the-lalit-kolkata"
            assert claim.typed_value["is_indicative_search"] is True

            # Verify proposal path
            cand = {
                "category": "lodging",
                "primary_name": "The Lalit Great Eastern Kolkata",
                "point": {"lat": 22.5697, "lng": 88.3518},
                "provenance": {
                    "source_urls": ["https://hotels.example.com/the-lalit-kolkata"],
                    "observation_ids": [str(obs_id)],
                    "retrieval_time": now.isoformat(),
                },
            }
            state: dict[str, Any] = {
                "trip_id": str(uuid.uuid4()),
                "owner_id": str(owner_id),
                "execution_id": str(exec_id),
                "brief": {
                    "origin_name": "Kolkata",
                    "origin_lat": 22.5726,
                    "origin_lng": 88.3639,
                    "destinations": [
                        {
                            "name": "Kolkata",
                            "latitude": 22.5726,
                            "longitude": 88.3639,
                            "stay_days": 2,
                        }
                    ],
                    "start_date": "2026-11-20",
                    "end_date": "2026-11-22",
                    "rooms": 1,
                    "adults": 2,
                    "budget_tier": "standard",
                },
                "eligible_stops": [],
                "editorial_recommendations": [cand],
                "observations": [obs],
                "task_outputs": {},
            }
            proposal_res = synthesize_proposal_node(state)
            accom_res = proposal_res["proposal"]["accommodation_research"]
            indicative_options = accom_res["indicative_options"]

            opt = next(h for h in indicative_options if h["property_name"] == "The Lalit Great Eastern Kolkata")
            assert opt["price_per_night"] == 7500
            assert opt["currency"] == "INR"
            assert opt["observation_id"] == str(obs_id)
            assert opt["source_url"] == "https://hotels.example.com/the-lalit-kolkata"
            assert opt["evidence_class"] == EvidenceClass.INDICATIVE_SEARCH.value
            assert opt["is_indicative_search"] is True
            assert opt["is_verified_offer"] is False
            assert opt["booking_confirmed"] is False
            assert opt["availability_status"] == "INDICATIVE_SEARCH_ONLY"
            assert "live_room_availability_unverified" in opt["unavailable_details"]
            assert "booking_unconfirmed" in opt["unavailable_details"]

    async def test_accommodation_price_attribution_unsupported_candidate_price(
        self, session_factory: Any
    ) -> None:
        """Regression 5: Unsupported candidate price is preserved as price unavailable."""
        from functions.orchestrator.graphs.trip_planner.graph import synthesize_proposal_node

        cand = {
            "category": "lodging",
            "primary_name": "Victoria Guest House",
            "point": {"lat": 22.5448, "lng": 88.3426},
            "price_estimate_inr": 2500.0,
            "provenance": {
                "source_urls": ["https://travel-guide.example/victoria-guest-house"],
                "retrieval_time": "2026-11-15T12:00:00Z",
            },
        }

        state: dict[str, Any] = {
            "trip_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "execution_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Kolkata",
                        "latitude": 22.5726,
                        "longitude": 88.3639,
                        "stay_days": 2,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-22",
                "rooms": 1,
                "adults": 2,
                "budget_tier": "standard",
            },
            "eligible_stops": [],
            "editorial_recommendations": [cand],
            "observations": [],  # No supporting observations
            "task_outputs": {},
        }

        proposal_res = synthesize_proposal_node(state)
        accom_res = proposal_res["proposal"]["accommodation_research"]
        indicative_options = accom_res["indicative_options"]

        opt = next(h for h in indicative_options if h["property_name"] == "Victoria Guest House")
        assert opt["price_per_night"] is None
        assert opt["currency"] is None
        assert opt["observation_id"] is None
        assert opt["evidence_class"] is None
        assert opt["is_indicative_search"] is False
        assert opt["availability_status"] == "PRICE_UNAVAILABLE"
        assert "price_unsupported_by_source" in opt["unavailable_details"]

    async def test_advisory_research_relevant_active_warning(
        self, session_factory: Any
    ) -> None:
        """Advisory 1: Active relevant road closure warning is preserved with full provenance and diagnostics."""
        from functions.orchestrator.graphs.trip_planner.graph import synthesize_proposal_node

        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            obs_closure = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://wbtrafficpolice.gov.in/advisories/nh116b-closure",
                canonical_url="https://wbtrafficpolice.gov.in/advisories/nh116b-closure",
                publisher="West Bengal Traffic Police",
                content_hash="closure_hash_nh116b_12345",
                retrieved_at=now,
                published_at=now,
                excerpt_text="NH-116B submerged at Nandakumar culvert. Total road closure in effect. Traffic diverted.",
                excerpt_locator="advisory:notice[0]",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={
                    "category": "road_closure",
                    "advisory_type": "ROAD_CLOSURE",
                    "headline": "NH-116B culvert damage and road closure near Nandakumar",
                    "affected_area": "NH-116B Nandakumar to Digha",
                    "severity": "CRITICAL",
                    "applicable_start": "2026-11-20T00:00:00Z",
                    "applicable_end": "2026-11-23T00:00:00Z",
                    "is_authoritative": True,
                },
                created_at=now,
            )
            session.add(obs_closure)
            await session.commit()

            # Test claim ingestion
            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[],
                owner_id=owner_id,
            )
            assert len(claims) == 1
            claim = claims[0]
            assert claim.category == "RESTRICTION"
            assert claim.typed_value["advisory_type"] == "ROAD_CLOSURE"
            assert claim.typed_value["severity"] == "CRITICAL"
            assert claim.typed_value["observation_id"] == str(obs_closure.id)
            assert claim.typed_value["source_url"] == "https://wbtrafficpolice.gov.in/advisories/nh116b-closure"
            assert claim.typed_value["is_authoritative"] is True
            assert claim.typed_value["uncertainty"] == "CONFIRMED_AUTHORITATIVE"

            # Test proposal path
            state: dict[str, Any] = {
                "trip_id": str(uuid.uuid4()),
                "owner_id": str(owner_id),
                "execution_id": str(exec_id),
                "brief": {
                    "origin_name": "Kolkata",
                    "origin_lat": 22.5726,
                    "origin_lng": 88.3639,
                    "destinations": [
                        {
                            "name": "Digha",
                            "latitude": 21.6266,
                            "longitude": 87.5074,
                            "stay_days": 2,
                        }
                    ],
                    "start_date": "2026-11-20",
                    "end_date": "2026-11-22",
                },
                "eligible_stops": [],
                "editorial_recommendations": [],
                "observations": [obs_closure],
                "task_outputs": {},
            }

            proposal_res = synthesize_proposal_node(state)
            proposal = proposal_res["proposal"]
            advisory_res = proposal["advisory_research"]

            assert advisory_res["status"] == "ADVISORIES_PRESENT"
            assert len(advisory_res["advisories"]) == 1

            adv = advisory_res["advisories"][0]
            assert adv["headline"] == "NH-116B culvert damage and road closure near Nandakumar"
            assert adv["advisory_type"] == "ROAD_CLOSURE"
            assert adv["severity"] == "CRITICAL"
            assert adv["affected_area"] == "NH-116B Nandakumar to Digha"
            assert adv["source_url"] == "https://wbtrafficpolice.gov.in/advisories/nh116b-closure"
            assert adv["observation_id"] == str(obs_closure.id)
            assert adv["is_authoritative"] is True
            assert adv["uncertainty"] == "CONFIRMED_AUTHORITATIVE"
            assert adv["status"] == "ACTIVE"

            assert proposal["advisory_status"] == "ADVISORIES_PRESENT"
            assert any("NH-116B" in diag for diag in proposal["route_diagnostics"])
            assert "Absence of travel advisories" in advisory_res["safety_notice"]

    async def test_advisory_research_unrelated_area_excluded_and_reports_advisory_unknown(
        self, session_factory: Any
    ) -> None:
        """Advisory 2: Warning for unrelated corridor is excluded and route reports ADVISORY_UNKNOWN."""
        from functions.orchestrator.graphs.trip_planner.graph import synthesize_proposal_node

        now = datetime.now(UTC)
        # Landslide on NH-10 in North Sikkim when the trip is Kolkata to Digha in South Bengal
        obs_sikkim = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            source_url="https://sikkimpolice.gov.in/advisories/nh10-landslide",
            canonical_url="https://sikkimpolice.gov.in/advisories/nh10-landslide",
            publisher="Sikkim Traffic Police",
            content_hash="sikkim_hash_nh10_12345",
            retrieved_at=now,
            published_at=now,
            excerpt_text="Massive landslide blocks NH-10 near Rangpo in Sikkim. Traffic suspended indefinitely.",
            excerpt_locator="advisory:notice[0]",
            extraction_method="STATIC_SCRAPLING",
            artifact_ref={
                "category": "road_closure",
                "advisory_type": "LANDSLIDE",
                "headline": "Landslide blocks NH-10 in North Sikkim",
                "affected_area": "NH-10 Rangpo Sikkim",
                "severity": "CRITICAL",
                "is_authoritative": True,
            },
            created_at=now,
        )

        state: dict[str, Any] = {
            "trip_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "execution_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 2,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-22",
            },
            "eligible_stops": [],
            "editorial_recommendations": [],
            "observations": [obs_sikkim],
            "task_outputs": {},
        }

        proposal_res = synthesize_proposal_node(state)
        adv_res = proposal_res["proposal"]["advisory_research"]

        assert len(adv_res["advisories"]) == 0
        assert len(adv_res["unrelated_advisories"]) == 1
        assert adv_res["unrelated_advisories"][0]["headline"] == "Landslide blocks NH-10 in North Sikkim"
        assert adv_res["status"] == "ADVISORY_UNKNOWN"
        assert adv_res["status"] != "SAFE"
        assert "No verified advisories for route corridor" in adv_res["reason"]
        assert "Absence of travel advisories" in adv_res["safety_notice"]

    async def test_advisory_research_expired_warning_treated_as_expired_not_safe(
        self, session_factory: Any
    ) -> None:
        """Advisory 3: Expired warning is classified as expired and route reports ADVISORY_UNKNOWN."""
        from functions.orchestrator.graphs.trip_planner.graph import synthesize_proposal_node

        now = datetime.now(UTC)
        # Advisory that ended on 2026-11-10 when trip is on 2026-11-20
        obs_expired = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            source_url="https://wbtrafficpolice.gov.in/advisories/nh116b-culvert-completed",
            canonical_url="https://wbtrafficpolice.gov.in/advisories/nh116b-culvert-completed",
            publisher="West Bengal Traffic Police",
            content_hash="expired_hash_nh116b_12345",
            retrieved_at=now,
            published_at=now,
            excerpt_text="NH-116B temporary culvert inspection closure concluded on November 10 2026.",
            excerpt_locator="advisory:notice[0]",
            extraction_method="STATIC_SCRAPLING",
            artifact_ref={
                "category": "road_closure",
                "advisory_type": "ROAD_CLOSURE",
                "headline": "NH-116B culvert inspection closure",
                "affected_area": "NH-116B Nandakumar to Digha",
                "severity": "WARNING",
                "applicable_start": "2026-11-08T00:00:00Z",
                "applicable_end": "2026-11-10T00:00:00Z",
                "is_authoritative": True,
            },
            created_at=now,
        )

        state: dict[str, Any] = {
            "trip_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "execution_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 2,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-22",
            },
            "eligible_stops": [],
            "editorial_recommendations": [],
            "observations": [obs_expired],
            "task_outputs": {},
        }

        proposal_res = synthesize_proposal_node(state)
        adv_res = proposal_res["proposal"]["advisory_research"]

        assert len(adv_res["advisories"]) == 0
        assert len(adv_res["expired_advisories"]) == 1
        exp = adv_res["expired_advisories"][0]
        assert exp["status"] == "EXPIRED"
        assert exp["uncertainty"] == "EXPIRED"
        assert adv_res["status"] == "ADVISORY_UNKNOWN"
        assert adv_res["status"] != "SAFE"
        assert "expired" in adv_res["reason"].lower()
        assert "Absence of travel advisories" in adv_res["safety_notice"]

    async def test_advisory_research_conflicting_reports_preserved_honestly(
        self, session_factory: Any
    ) -> None:
        """Advisory 4: Conflicting reports are preserved side by side without guessing."""
        from functions.orchestrator.graphs.trip_planner.graph import synthesize_proposal_node

        now = datetime.now(UTC)
        # Source 1 reports road closed
        obs_closed = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            source_url="https://wbtrafficpolice.gov.in/advisories/nh116b-closed",
            canonical_url="https://wbtrafficpolice.gov.in/advisories/nh116b-closed",
            publisher="West Bengal Traffic Police",
            content_hash="conflict_hash_nh116b_closed_12345",
            retrieved_at=now,
            published_at=now,
            excerpt_text="NH-116B closed at Nandakumar due to heavy waterlogging and damaged culvert.",
            excerpt_locator="advisory:notice[0]",
            extraction_method="STATIC_SCRAPLING",
            artifact_ref={
                "category": "road_closure",
                "advisory_type": "ROAD_CLOSURE",
                "headline": "NH-116B closed at Nandakumar due to heavy waterlogging",
                "affected_area": "NH-116B Nandakumar to Digha",
                "severity": "CRITICAL",
                "status": "ACTIVE",
                "is_authoritative": True,
            },
            created_at=now,
        )

        # Source 2 reports road reopened to all traffic
        obs_reopened = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            source_url="https://nhai.gov.in/updates/nh116b-reopened",
            canonical_url="https://nhai.gov.in/updates/nh116b-reopened",
            publisher="NHAI Regional Office",
            content_hash="conflict_hash_nh116b_reopened_12345",
            retrieved_at=now,
            published_at=now,
            excerpt_text="NH-116B reopened to all vehicular traffic following water pumping operations.",
            excerpt_locator="advisory:notice[0]",
            extraction_method="STATIC_SCRAPLING",
            artifact_ref={
                "category": "road_closure",
                "advisory_type": "ROAD_CLOSURE",
                "headline": "NH-116B reopened to all vehicular traffic",
                "affected_area": "NH-116B Nandakumar to Digha",
                "severity": "INFO",
                "status": "LIFTED",
                "is_authoritative": True,
            },
            created_at=now,
        )

        state: dict[str, Any] = {
            "trip_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "execution_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 2,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-22",
            },
            "eligible_stops": [],
            "editorial_recommendations": [],
            "observations": [obs_closed, obs_reopened],
            "task_outputs": {},
        }

        proposal_res = synthesize_proposal_node(state)
        adv_res = proposal_res["proposal"]["advisory_research"]

        assert len(adv_res["conflicting_advisories"]) >= 2
        assert all(c["uncertainty"] == "CONFLICTED" for c in adv_res["conflicting_advisories"])
        assert adv_res["status"] == "ADVISORIES_PRESENT"
        assert "Conflicting travel advisory reports" in adv_res["reason"]
        assert "Absence of travel advisories" in adv_res["safety_notice"]

    async def test_advisory_research_unavailable_evidence_reports_advisory_unknown(
        self, session_factory: Any
    ) -> None:
        """Advisory 5: Unavailable evidence reports ADVISORY_UNKNOWN with explicit reason."""
        from functions.orchestrator.graphs.trip_planner.graph import synthesize_proposal_node

        state: dict[str, Any] = {
            "trip_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "execution_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 2,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-22",
            },
            "eligible_stops": [],
            "editorial_recommendations": [],
            "observations": [],
            "task_outputs": {},
        }

        proposal_res = synthesize_proposal_node(state)
        adv_res = proposal_res["proposal"]["advisory_research"]

        assert adv_res["status"] == "ADVISORY_UNKNOWN"
        assert adv_res["status"] != "SAFE"
        assert adv_res["status"] != "COMPLETED"
        assert "No road hazard or travel advisory evidence retrieved" in adv_res["reason"]
        assert adv_res["advisories"] == []
        assert "Absence of travel advisories" in adv_res["safety_notice"]

