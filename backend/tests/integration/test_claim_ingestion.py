"""Integration tests for claim ingestion service conforming to spec 0007 AC-13 and AC-14."""

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
from functions.orchestrator.evidence.models import ClaimEvidenceModel
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
        async with session_factory() as session:
            exec_id, owner_id = await _create_test_execution(session)
            now = datetime.now(UTC)

            # Insert a genuine collected source observation
            obs_id = uuid.uuid4()
            obs = SourceObservationModel(
                id=obs_id,
                execution_id=exec_id,
                source_url="https://wbtourism.gov.in/victoria-memorial",
                canonical_url="https://wbtourism.gov.in/victoria-memorial",
                publisher="West Bengal Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now - timedelta(days=10),
                excerpt_text="Victoria Memorial is a grand marble building in Kolkata open daily from 10:00 to 17:00.",
                excerpt_locator="p.details",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={"category": "attraction", "status": "AVAILABLE"},
                created_at=now,
            )
            session.add(obs)
            await session.commit()

            candidate = RegisteredCandidate(
                primary_name="Victoria Memorial",
                category="attraction",
                point=CoordinatePoint(lat=22.5448, lng=88.3426),
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
            assert claim.subject_id == "victoria memorial"
            assert claim.category == ClaimCategory.ATTRACTION.value
            assert claim.verification == VerificationStatus.VERIFIED

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
            assert report.eligible_stops[0].primary_name == "Victoria Memorial"
            assert report.eligible_stops[0].existence_status == CandidateExistenceStatus.VERIFIED
            assert len(report.editorial_recommendations) == 0

    async def test_ingest_claims_preserves_indicative_search_for_lodging(
        self, session_factory: Any
    ) -> None:
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
                artifact_ref={"category": "attraction", "status": "AVAILABLE"},
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
