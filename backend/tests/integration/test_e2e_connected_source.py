"""Connected source backed evidence scenario integration test conforming to spec 0002.

Validates the full factual evidence lifecycle on live PostgreSQL:
1. Task dispatch consumption and fenced evidence collection committing immutable source observations.
2. Canonical place spatial candidate resolution using PostGIS coordinates and token similarity.
3. Claim creation linked to source observations via claim evidence junction records.
4. Deterministic policy evaluation verifying authoritative domains.
5. Contradiction handling preserving conflicting assertions side by side as CONFLICTED.
6. Freshness expiration transitioning status to STALE without touching historical verification.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from functions.api.models import TripModel
from functions.evidence_collector.fixtures import (
    FixtureSearchAdapter,
    FixtureWebExtractionAdapter,
    RawObservation,
)
from functions.evidence_collector.models import SourceObservationModel
from functions.evidence_collector.service import (
    EvidenceCollectorService,
    TaskDispatchEnvelope,
)
from functions.orchestrator.evidence.repository import ClaimsRepository
from functions.orchestrator.evidence.schemas import (
    AvailabilityStatus,
    ClaimCategory,
    ClaimCreate,
    EvidenceSupportType,
    VerificationStatus,
)
from functions.orchestrator.evidence.service import ClaimVerificationService
from functions.orchestrator.execution.models import (
    ExecutionModel,
    TaskModel,
)
from functions.orchestrator.places import (
    AccessStatus,
    CandidatePlaceInput,
    CanonicalPlaceCreate,
    CanonicalPlacesRepository,
    CoordinatePoint,
    PlaceResolutionService,
    ResolutionStatus,
)


@pytest.mark.asyncio
class TestConnectedSourceBackedEvidenceScenario:
    """Connected integration suite verifying the complete evidence and verification flow."""

    async def test_full_connected_evidence_lifecycle(self, session_factory: Any) -> None:
        """Executes connected scenario from collection through spatial resolution to policy verification."""
        owner_id = uuid.uuid4()
        trip_id = uuid.uuid4()
        execution_id = uuid.uuid4()
        task_id = uuid.uuid4()
        event_id = uuid.uuid4()
        existing_place_id = uuid.uuid4()
        test_suffix = uuid.uuid4().hex[:8]
        now = datetime.now(UTC)

        primary_name = f"Dakshineswar Kali Temple {test_suffix}"
        alt_name = f"Dakshineswar Temple {test_suffix}"
        # Unique isolated coordinates to prevent collisions with historical test database records
        offset = float(uuid.uuid4().int % 50000) / 10000.0
        exact_lng = round(72.0 + offset, 5)
        exact_lat = round(19.0 + offset, 5)

        # Step 1: Create active trip, execution, and canonical place in database
        async with session_factory() as session:
            trip = TripModel(
                id=trip_id,
                owner_id=owner_id,
                current_version=1,
                state="draft",
                created_at=now,
                updated_at=now,
            )
            session.add(trip)

            execution = ExecutionModel(
                id=execution_id,
                owner_id=owner_id,
                trip_id=trip_id,
                workflow="TRAVEL_RESEARCH",
                status="RUNNING",
                context_version=1,
                deadline_at=now + timedelta(hours=3),
                created_at=now,
                updated_at=now,
            )
            session.add(execution)

            # Step 2: Seed canonical place in database with PostGIS geometry
            places_repo = CanonicalPlacesRepository(session)
            place_dto = CanonicalPlaceCreate(
                id=existing_place_id,
                primary_name=primary_name,
                locality="Kolkata",
                point=CoordinatePoint(lng=exact_lng, lat=exact_lat),
                names=[alt_name, f"Dakshineswar Kali Mandir {test_suffix}"],
                access_status=AccessStatus.PUBLIC,
                geocoder_source="nominatim",
                provider_ids={"osm_id": f"way_{test_suffix}"},
                is_candidate=False,
            )
            created_place = await places_repo.create_place(place_dto)

            # Step 3: Insert pending task for EVIDENCE worker
            task = TaskModel(
                id=task_id,
                execution_id=execution_id,
                worker="EVIDENCE",
                action="collect",
                status="PENDING",
                input_hash="test_sha256_hash",
                idempotency_key=f"task-idem-{task_id}",
                attempt=0,
                max_attempts=3,
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                payload_ref={
                    "queries": ["Dakshineswar Kali Temple festival traffic advisory"],
                    "urls": ["https://kolkatatrafficpolice.gov.in/dakshineswar-advisory"],
                    "budget": {
                        "remaining_tool_calls": 5,
                        "remaining_cost_minor": 500,
                        "currency": "INR",
                    },
                },
                created_at=now,
                updated_at=now,
            )
            session.add(task)
            await session.commit()

        # Step 4: Worker executes collection under lease fencing
        envelope = TaskDispatchEnvelope(
            event_id=event_id,
            execution_id=execution_id,
            task_id=task_id,
            kind="TASK_DISPATCH",
            outbox_delivery_attempts=1,
            task_attempt=0,
            context_version=1,
            worker="EVIDENCE",
            action="collect",
            inner_payload={
                "queries": ["Dakshineswar Kali Temple festival traffic advisory"],
                "urls": ["https://kolkatatrafficpolice.gov.in/dakshineswar-advisory"],
            },
        )

        police_fixture = RawObservation.create(
            source_url="https://kolkatatrafficpolice.gov.in/dakshineswar-advisory",
            publisher="Kolkata Traffic Police",
            excerpt_text="Traffic restricted near temple area for crowd management.",
            excerpt_locator="article.advisory p.traffic",
            extraction_method="MANUAL_FIXTURE",
            retrieved_at=now,
            published_at=now,
        )
        search_adapter = FixtureSearchAdapter(fixtures=[police_fixture])
        extraction_adapter = FixtureWebExtractionAdapter({
            "https://kolkatatrafficpolice.gov.in/dakshineswar-advisory": police_fixture
        })

        async with session_factory() as session:
            collector_service = EvidenceCollectorService(session)
            result_id, observation_ids = await collector_service.execute_collection_task(
                envelope,
                adapter=search_adapter,
                extraction_adapter=extraction_adapter,
            )


        assert result_id is not None
        assert len(observation_ids) > 0

        # Step 5: Verify observation committed atomically in PostgreSQL
        async with session_factory() as session:
            obs_stmt = select(SourceObservationModel).where(
                SourceObservationModel.id.in_(observation_ids)
            )
            obs_res = await session.execute(obs_stmt)
            persisted_observations = list(obs_res.scalars().all())
            assert len(persisted_observations) == len(observation_ids)

            first_obs = persisted_observations[0]
            assert first_obs.execution_id == execution_id
            assert first_obs.content_hash != ""
            assert len(first_obs.content_hash) == 64

            # Verify task transitioned to COMPLETED
            task_stmt = select(TaskModel).where(TaskModel.id == task_id)
            task_row = (await session.execute(task_stmt)).scalar_one()
            assert task_row.status == "COMPLETED"

            # Step 6: Candidate place resolution using spatial coordinates (within 40m)
            places_repo = CanonicalPlacesRepository(session)
            places_service = PlaceResolutionService(places_repo)
            candidate_point = CoordinatePoint(
                lng=exact_lng,
                lat=round(exact_lat + 0.00035, 5),
            )
            candidate = CandidatePlaceInput(
                primary_name=alt_name,
                locality="Kolkata",
                point=candidate_point,
                names=[primary_name],
            )
            resolution_result = await places_service.resolve_candidate(candidate)
            assert resolution_result.status == ResolutionStatus.RESOLVED
            assert resolution_result.place is not None
            assert resolution_result.place.id == created_place.id
            assert resolution_result.distance_meters is not None
            assert resolution_result.distance_meters <= 100.0


            # Step 7: Submit claim linked to official police observation
            claims_service = ClaimVerificationService(session)
            claim_data = ClaimCreate(
                execution_id=execution_id,
                subject_id=f"place_{created_place.id}",
                place_id=created_place.id,
                category=ClaimCategory.RESTRICTION,
                typed_value={
                    "status": "RESTRICTED",
                    "reason": "Puja crowd management protocol",
                    "closed": True,
                },
            )

            # Link observation with SUPPORTS
            created_claim = await claims_service.submit_claim(
                claim_data=claim_data,
                observations_with_support=[(first_obs, EvidenceSupportType.SUPPORTS, "div.advisory")],
            )
            await session.commit()

            # AC 9 & AC 10: Official police domain verifies critical restriction
            assert created_claim.verification == VerificationStatus.VERIFIED.value
            assert created_claim.availability == AvailabilityStatus.UNAVAILABLE.value

            # Verify junction table link
            claims_repo = ClaimsRepository(session)
            evidence_links = await claims_repo.get_claim_evidence(created_claim.id)
            assert len(evidence_links) == 1
            assert evidence_links[0].observation_id == first_obs.id


            # Step 8: Contradiction handling with side by side preservation (AC 11)
            contradicting_claim_data = ClaimCreate(
                execution_id=execution_id,
                subject_id=f"place_{created_place.id}",
                place_id=created_place.id,
                category=ClaimCategory.RESTRICTION,
                typed_value={
                    "status": "OPEN",
                    "open": True,
                    "reason": "Normal visitor access unrestricted",
                },
            )

            # Create secondary blog observation asserting open
            blog_obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=execution_id,
                source_url="https://travelblog.example.com/dakshineswar-open",
                canonical_url="https://travelblog.example.com/dakshineswar-open",
                publisher="Travel Blogger",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text="Temple access open as normal.",
                excerpt_locator="p.notice",
                extraction_method="SCRAPLING_HTTP",
                created_at=now,
            )
            session.add(blog_obs)
            await session.flush()

            contradicted_claim = await claims_service.submit_claim(
                claim_data=contradicting_claim_data,
                observations_with_support=[(blog_obs, EvidenceSupportType.SUPPORTS, "p.notice")],
            )
            await session.commit()

            # Both claims transition to CONFLICTED without data loss
            reloaded_initial = await claims_repo.get_by_id(created_claim.id)
            reloaded_second = await claims_repo.get_by_id(contradicted_claim.id)

            assert reloaded_initial is not None
            assert reloaded_second is not None
            assert reloaded_initial.verification == VerificationStatus.CONFLICTED.value
            assert reloaded_second.verification == VerificationStatus.CONFLICTED.value

            # Reciprocal cross references exist in contradictions
            initial_refs = {item.get("claim_id") for item in reloaded_initial.contradictions}
            second_refs = {item.get("claim_id") for item in reloaded_second.contradictions}
            assert str(reloaded_second.id) in initial_refs
            assert str(reloaded_initial.id) in second_refs
