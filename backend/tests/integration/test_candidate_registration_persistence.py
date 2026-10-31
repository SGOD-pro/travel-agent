"""Integration tests for candidate place registration, PostGIS persistence, and evidence eligibility on live PostgreSQL.

Conforms to spec 0007 AC-1, AC-3, and Slice 3 requirements:
- Validates candidate registration in canonical_places with is_candidate=True and EDITORIAL_DISCOVERY.
- Proves replay idempotency: re-registering identical proposal resolves to existing record without duplicate key error.
- Proves evidence eligibility against persisted source observations and claims.
- Enforces strict tenant and execution isolation on evidence evaluation.
- Verifies hours evaluation: verified hours vs explicitly labeled assumed daytime window (HOURS_ASSUMED).
- Verifies contradiction handling: refuting evidence excludes place entirely from solver and recommendations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from contracts.candidate import (
    CandidateExistenceStatus,
    CandidateHoursStatus,
)
from contracts.llm import PlaceProposal
from functions.api.models import TripModel
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.eligibility import (
    DEFAULT_ASSUMED_WINDOW_END,
    DEFAULT_ASSUMED_WINDOW_START,
    DeterministicEvidenceEligibilityGate,
)
from functions.orchestrator.evidence.models import ClaimEvidenceModel, ClaimModel
from functions.orchestrator.evidence.schemas import (
    ClaimCategory,
    FreshnessStatus,
    VerificationStatus,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.models import ExecutionModel, TaskModel
from functions.orchestrator.execution.runner import (
    ExecutionResumeEvent,
    ExecutionStartEvent,
    GraphRunnerService,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    compile_trip_planning_workflow,
    evaluate_candidates_node,
)
from functions.orchestrator.places.candidate_service import (
    CandidateRegistrationService,
)
from functions.orchestrator.places.models import CanonicalPlaceModel
from functions.orchestrator.places.schemas import CoordinatePoint


async def _create_test_execution_and_trip(session: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper creating a parent trip and execution row in live PostgreSQL."""
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
    return execution_id, owner_id


@pytest.mark.asyncio
async def test_candidate_registration_persists_canonical_place_in_postgresql(
    session_factory: Any,
) -> None:
    """Verifies that candidate proposals persist with is_candidate=True and EDITORIAL_DISCOVERY in canonical_places."""
    async with session_factory() as session:
        exec_id, owner_id = await _create_test_execution_and_trip(session)
        service = CandidateRegistrationService(session=session)

        corridor = [
            CoordinatePoint(lat=22.5726, lng=88.3639),  # Kolkata
            CoordinatePoint(lat=21.6266, lng=87.5074),  # Digha
        ]

        unique_name = f"Custom Heritage Landmark {uuid.uuid4().hex[:6]}"
        proposal = PlaceProposal(
            name=unique_name,
            category="temple",
            estimated_latitude=22.1234,
            estimated_longitude=88.1234,
            suggested_stay_minutes=90,
            price_estimate_inr=50.0,
        )

        valid_cands, excluded_cands = await service.register_candidate_proposals(
            proposals=[proposal],
            corridor_points=corridor,
            execution_id=exec_id,
            owner_id=owner_id,
        )

        await session.commit()

        assert len(valid_cands) == 1
        assert len(excluded_cands) == 0

        registered = valid_cands[0]
        assert registered.place_id is not None
        assert registered.primary_name == unique_name
        assert registered.uncertainty == "EDITORIAL_DISCOVERY"
        assert registered.is_candidate is True
        assert registered.existence_status == CandidateExistenceStatus.UNVERIFIED

        # Verify PostgreSQL database row
        stmt = select(CanonicalPlaceModel).where(CanonicalPlaceModel.id == registered.place_id)
        place_row = (await session.execute(stmt)).scalar_one_or_none()
        assert place_row is not None
        assert place_row.primary_name == unique_name
        assert place_row.is_candidate is True
        assert place_row.geocoder_provenance == "EDITORIAL_DISCOVERY"
        assert place_row.access_status == "UNKNOWN"
        assert place_row.provider_ids.get("category") == "temple"


@pytest.mark.asyncio
async def test_candidate_registration_replay_idempotency_avoids_duplicate_places(
    session_factory: Any,
) -> None:
    """Verifies replay idempotency: re-registering an identical proposal resolves to the same record."""
    async with session_factory() as session:
        exec_id, owner_id = await _create_test_execution_and_trip(session)
        service = CandidateRegistrationService(session=session)

        corridor = [
            CoordinatePoint(lat=22.5726, lng=88.3639),
            CoordinatePoint(lat=21.6266, lng=87.5074),
        ]

        unique_name = f"Custom Replay Landmark {uuid.uuid4().hex[:6]}"
        proposal = PlaceProposal(
            name=unique_name,
            category="heritage",
            estimated_latitude=22.1450,
            estimated_longitude=88.1450,
        )

        # Initial registration
        first_pass, _ = await service.register_candidate_proposals(
            [proposal], corridor, execution_id=exec_id, owner_id=owner_id
        )
        await session.commit()

        # Replay registration in a new transaction
        second_pass, _ = await service.register_candidate_proposals(
            [proposal], corridor, execution_id=exec_id, owner_id=owner_id
        )
        await session.commit()

        assert len(first_pass) == 1
        assert len(second_pass) == 1
        assert first_pass[0].place_id == second_pass[0].place_id

        # Verify exactly one database record exists
        stmt = select(CanonicalPlaceModel).where(CanonicalPlaceModel.primary_name == unique_name)
        places = (await session.execute(stmt)).scalars().all()
        assert len(places) == 1


@pytest.mark.asyncio
async def test_candidate_eligibility_with_persisted_evidence_and_assumed_hours(
    session_factory: Any,
) -> None:
    """Verifies evidence eligibility with supporting observation and unknown hours resulting in HOURS_ASSUMED."""
    async with session_factory() as session:
        exec_id, owner_id = await _create_test_execution_and_trip(session)
        now = datetime.now(UTC)

        unique_name = f"Botanical Gardens {uuid.uuid4().hex[:6]}"
        service = CandidateRegistrationService(session=session)
        corridor = [CoordinatePoint(lat=22.5726, lng=88.3639)]
        proposal = PlaceProposal(
            name=unique_name,
            category="garden",
            estimated_latitude=22.5600,
            estimated_longitude=88.3000,
        )

        valid_cands, _ = await service.register_candidate_proposals(
            [proposal], corridor, execution_id=exec_id, owner_id=owner_id
        )
        registered = valid_cands[0]

        # Insert supporting observation into source_observations table
        obs = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            source_url="https://wbtourism.gov.in/botanical",
            canonical_url=f"https://wbtourism.gov.in/{unique_name.lower().replace(' ', '')}",
            publisher="wbtourism.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=5),
            excerpt_text=f"The {unique_name} is a historic landscaped garden near Kolkata.",
            excerpt_locator="article p",
            extraction_method="SCRAPLING_HTTP",
            created_at=now,
        )
        session.add(obs)
        await session.flush()

        # Insert verified fresh ATTRACTION claim confirming existence and link it to observation
        claim = ClaimModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            subject_id=unique_name,
            place_id=registered.place_id,
            category=ClaimCategory.ATTRACTION.value,
            typed_value={"verified_name": unique_name},
            verification=VerificationStatus.VERIFIED.value,
            availability="AVAILABLE",
            freshness=FreshnessStatus.FRESH.value,
            valid_from=now - timedelta(days=30),
            valid_until=now + timedelta(days=60),
            created_at=now,
            updated_at=now,
        )
        session.add(claim)
        await session.flush()

        evidence_link = ClaimEvidenceModel(
            claim_id=claim.id,
            observation_id=obs.id,
            support_type="SUPPORTS",
            locator="article p",
            created_at=now,
        )
        session.add(evidence_link)
        await session.commit()

        # Evaluate candidate eligibility using live PostgreSQL queries
        gate = DeterministicEvidenceEligibilityGate(session=session)
        report = await gate.evaluate_candidate_eligibility(
            candidates=[registered],
            execution_id=exec_id,
            owner_id=owner_id,
        )

        assert len(report.eligible_stops) == 1
        assert len(report.editorial_recommendations) == 0

        stop = report.eligible_stops[0]
        assert stop.existence_status == CandidateExistenceStatus.VERIFIED
        assert stop.hours_status == CandidateHoursStatus.HOURS_ASSUMED
        assert stop.is_hours_assumed is True
        assert stop.window_start == DEFAULT_ASSUMED_WINDOW_START
        assert stop.window_end == DEFAULT_ASSUMED_WINDOW_END


@pytest.mark.asyncio
async def test_candidate_eligibility_with_persisted_verified_hours_claim(
    session_factory: Any,
) -> None:
    """Verifies that a verified fresh HOURS claim sets VERIFIED_HOURS with exact schedule windows."""
    async with session_factory() as session:
        exec_id, owner_id = await _create_test_execution_and_trip(session)
        now = datetime.now(UTC)

        unique_name = f"Victoria Hall {uuid.uuid4().hex[:6]}"
        service = CandidateRegistrationService(session=session)
        corridor = [CoordinatePoint(lat=22.5726, lng=88.3639)]
        proposal = PlaceProposal(
            name=unique_name,
            category="museum",
            estimated_latitude=22.5448,
            estimated_longitude=88.3426,
        )

        valid_cands, _ = await service.register_candidate_proposals(
            [proposal], corridor, execution_id=exec_id, owner_id=owner_id
        )
        registered = valid_cands[0]

        # Insert supporting observation
        obs = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            source_url="https://victoria.org",
            canonical_url=f"https://victoria.org/{unique_name.lower().replace(' ', '')}",
            publisher="victoria.org",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=2),
            excerpt_text=f"Visit {unique_name} open daily for tourists.",
            excerpt_locator="main p",
            extraction_method="SCRAPLING_HTTP",
            created_at=now,
        )
        session.add(obs)
        await session.flush()

        # Insert verified fresh ATTRACTION claim confirming existence and link it
        exist_claim = ClaimModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            subject_id=unique_name,
            place_id=registered.place_id,
            category=ClaimCategory.ATTRACTION.value,
            typed_value={"verified_name": unique_name},
            verification=VerificationStatus.VERIFIED.value,
            availability="AVAILABLE",
            freshness=FreshnessStatus.FRESH.value,
            valid_from=now - timedelta(days=30),
            valid_until=now + timedelta(days=60),
            created_at=now,
            updated_at=now,
        )
        session.add(exist_claim)
        await session.flush()

        exist_link = ClaimEvidenceModel(
            claim_id=exist_claim.id,
            observation_id=obs.id,
            support_type="SUPPORTS",
            locator="main p",
            created_at=now,
        )
        session.add(exist_link)

        # Insert verified fresh HOURS claim and link it
        claim = ClaimModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            subject_id=unique_name,
            place_id=registered.place_id,
            category=ClaimCategory.HOURS.value,
            typed_value={"opens": "10:00", "closes": "17:00"},
            verification=VerificationStatus.VERIFIED.value,
            availability="AVAILABLE",
            freshness=FreshnessStatus.FRESH.value,
            valid_from=now - timedelta(days=30),
            valid_until=now + timedelta(days=60),
            created_at=now,
            updated_at=now,
        )
        session.add(claim)
        await session.flush()

        hours_link = ClaimEvidenceModel(
            claim_id=claim.id,
            observation_id=obs.id,
            support_type="SUPPORTS",
            locator="main p",
            created_at=now,
        )
        session.add(hours_link)
        await session.commit()

        gate = DeterministicEvidenceEligibilityGate(session=session)
        report = await gate.evaluate_candidate_eligibility(
            candidates=[registered],
            execution_id=exec_id,
            owner_id=owner_id,
        )

        assert len(report.eligible_stops) == 1
        stop = report.eligible_stops[0]
        assert stop.existence_status == CandidateExistenceStatus.VERIFIED
        assert stop.hours_status == CandidateHoursStatus.VERIFIED_HOURS
        assert stop.window_start == time(10, 0)
        assert stop.window_end == time(17, 0)
        assert stop.is_hours_assumed is False


@pytest.mark.asyncio
async def test_candidate_eligibility_cross_owner_isolation_in_postgresql(
    session_factory: Any,
) -> None:
    """Verifies that evidence belonging to Execution A cannot verify a candidate evaluated in Execution B."""
    async with session_factory() as session:
        exec_a, owner_a = await _create_test_execution_and_trip(session)
        exec_b, owner_b = await _create_test_execution_and_trip(session)
        now = datetime.now(UTC)

        unique_name = f"Isolated Club {uuid.uuid4().hex[:6]}"
        service = CandidateRegistrationService(session=session)
        corridor = [CoordinatePoint(lat=22.5726, lng=88.3639)]
        proposal = PlaceProposal(
            name=unique_name,
            category="club",
            estimated_latitude=22.5500,
            estimated_longitude=88.3500,
        )

        # Register candidate under Execution B
        valid_cands, _ = await service.register_candidate_proposals(
            [proposal], corridor, execution_id=exec_b, owner_id=owner_b
        )
        registered_b = valid_cands[0]

        # Insert supporting observation under Execution A (different tenant)
        obs_a = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=exec_a,
            source_url="https://tenant-a.com",
            canonical_url=f"https://tenant-a.com/{unique_name.lower().replace(' ', '')}",
            publisher="tenant-a.com",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=1),
            excerpt_text=f"Verified location of {unique_name}.",
            excerpt_locator="p",
            extraction_method="SCRAPLING_HTTP",
            created_at=now,
        )
        session.add(obs_a)
        await session.commit()

        # Evaluate candidate for Execution B
        gate = DeterministicEvidenceEligibilityGate(session=session)
        report_b = await gate.evaluate_candidate_eligibility(
            candidates=[registered_b],
            execution_id=exec_b,
            owner_id=owner_b,
        )

        # Cross-tenant observation must not leak; candidate remains UNVERIFIED in editorial recommendations
        assert len(report_b.eligible_stops) == 0
        assert len(report_b.editorial_recommendations) == 1
        assert (
            report_b.editorial_recommendations[0].existence_status
            == CandidateExistenceStatus.UNVERIFIED
        )


@pytest.mark.asyncio
async def test_candidate_eligibility_with_persisted_contradiction_in_postgresql(
    session_factory: Any,
) -> None:
    """Verifies that refuting observations or conflicted claims mark place CONTRADICTED and exclude it entirely."""
    async with session_factory() as session:
        exec_id, owner_id = await _create_test_execution_and_trip(session)
        now = datetime.now(UTC)

        unique_name = f"Custom Demolished Place {uuid.uuid4().hex[:6]}"
        service = CandidateRegistrationService(session=session)
        corridor = [CoordinatePoint(lat=22.5726, lng=88.3639)]
        proposal = PlaceProposal(
            name=unique_name,
            category="sight",
            estimated_latitude=22.1890,
            estimated_longitude=88.1890,
        )

        valid_cands, _ = await service.register_candidate_proposals(
            [proposal], corridor, execution_id=exec_id, owner_id=owner_id
        )
        registered = valid_cands[0]

        # Insert refuting observation asserting place is permanently closed or demolished
        obs = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            source_url="https://wbtourism.gov.in/closed",
            canonical_url=f"https://wbtourism.gov.in/{unique_name.lower().replace(' ', '')}",
            publisher="wbtourism.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=1),
            excerpt_text=f"{unique_name} is permanently closed and demolished.",
            excerpt_locator="p",
            extraction_method="SCRAPLING_HTTP",
            created_at=now,
        )
        session.add(obs)

        # Insert claim marked CONFLICTED
        claim = ClaimModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            subject_id=unique_name,
            place_id=registered.place_id,
            category="EXISTENCE",
            typed_value={"status": "permanently_closed"},
            verification=VerificationStatus.CONFLICTED.value,
            availability="UNAVAILABLE",
            freshness=FreshnessStatus.FRESH.value,
            created_at=now,
            updated_at=now,
        )
        session.add(claim)
        await session.commit()

        gate = DeterministicEvidenceEligibilityGate(session=session)
        report = await gate.evaluate_candidate_eligibility(
            candidates=[registered],
            execution_id=exec_id,
            owner_id=owner_id,
        )

        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 0
        assert len(report.excluded_candidates) == 1
        assert (
            report.excluded_candidates[0].existence_status == CandidateExistenceStatus.CONTRADICTED
        )


@pytest.mark.asyncio
async def test_candidate_eligibility_caller_supplying_another_owner_real_execution_id_raises_permission_error(
    session_factory: Any,
) -> None:
    """Verifies that an owner supplying another owner's real execution ID in PostgreSQL raises PermissionError."""
    async with session_factory() as session:
        exec_a, owner_a = await _create_test_execution_and_trip(session)
        exec_b, owner_b = await _create_test_execution_and_trip(session)
        service = CandidateRegistrationService(session=session)
        corridor = [CoordinatePoint(lat=22.5726, lng=88.3639)]

        unique_name = f"Guarded Spot {uuid.uuid4().hex[:6]}"
        proposal = PlaceProposal(
            name=unique_name,
            category="museum",
            estimated_latitude=22.5000,
            estimated_longitude=88.3000,
        )

        valid_cands, _ = await service.register_candidate_proposals(
            [proposal], corridor, execution_id=exec_b, owner_id=owner_b
        )
        registered = valid_cands[0]

        gate = DeterministicEvidenceEligibilityGate(session=session)
        # Caller is owner_b, but supplies owner_a's real execution ID
        with pytest.raises(PermissionError) as exc_info:
            await gate.evaluate_candidate_eligibility(
                candidates=[registered],
                execution_id=exec_a,
                owner_id=owner_b,
            )

        assert "authorization failed" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_candidate_registration_same_named_places_far_apart_remain_distinct_in_postgresql(
    session_factory: Any,
) -> None:
    """Verifies that same-named places far apart remain distinct and replay does not create duplicates."""
    async with session_factory() as session:
        exec_id, owner_id = await _create_test_execution_and_trip(session)
        service = CandidateRegistrationService(session=session)
        corridor = [
            CoordinatePoint(lat=22.5726, lng=88.3639),  # Kolkata
            CoordinatePoint(lat=21.6266, lng=87.5074),  # Digha (~140 km)
        ]

        shared_name = f"Shiva Temple {uuid.uuid4().hex[:6]}"
        proposal_kolkata = PlaceProposal(
            name=shared_name,
            category="temple",
            estimated_latitude=22.5700,
            estimated_longitude=88.3600,
        )
        proposal_digha = PlaceProposal(
            name=shared_name,
            category="temple",
            estimated_latitude=21.6300,
            estimated_longitude=87.5100,
        )

        # 1. Register Kolkata place
        pass_kolkata, _ = await service.register_candidate_proposals(
            [proposal_kolkata], corridor, execution_id=exec_id, owner_id=owner_id
        )
        await session.commit()

        # 2. Register Digha place with identical name ~140km away
        pass_digha, _ = await service.register_candidate_proposals(
            [proposal_digha], corridor, execution_id=exec_id, owner_id=owner_id
        )
        await session.commit()

        assert len(pass_kolkata) == 1
        assert len(pass_digha) == 1
        # Distinct places created because they are far apart
        assert pass_kolkata[0].place_id != pass_digha[0].place_id
        assert pass_kolkata[0].primary_name == pass_digha[0].primary_name

        stmt = select(CanonicalPlaceModel).where(CanonicalPlaceModel.primary_name == shared_name)
        rows = (await session.execute(stmt)).scalars().all()
        assert len(rows) == 2
        row_ids = {r.id for r in rows}
        assert pass_kolkata[0].place_id in row_ids
        assert pass_digha[0].place_id in row_ids

        # 3. Replay Digha registration: must resolve to existing Digha record without creating a 3rd record
        replay_digha, _ = await service.register_candidate_proposals(
            [proposal_digha], corridor, execution_id=exec_id, owner_id=owner_id
        )
        await session.commit()

        assert len(replay_digha) == 1
        assert replay_digha[0].place_id == pass_digha[0].place_id

        rows_after_replay = (await session.execute(stmt)).scalars().all()
        assert len(rows_after_replay) == 2

        # 4. Link verified claim and observation explicitly to Place A
        now = datetime.now(UTC)
        obs_kolkata = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            source_url="https://wbtourism.gov.in/kolkata_shiva",
            canonical_url=f"https://wbtourism.gov.in/{shared_name.lower().replace(' ', '_')}",
            publisher="wbtourism.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=5),
            excerpt_text=f"The ancient {shared_name} in Kolkata is an active pilgrimage temple.",
            excerpt_locator="article p",
            extraction_method="SCRAPLING_HTTP",
            artifact_ref={
                "status": "SUCCESS",
                "gps": {"lat": 22.5700, "lng": 88.3600},
            },
            created_at=now,
        )
        session.add(obs_kolkata)
        await session.flush()

        claim_kolkata = ClaimModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            place_id=pass_kolkata[0].place_id,
            subject_id=shared_name,
            category=ClaimCategory.ATTRACTION.value,
            typed_value={"verified_name": shared_name, "locality": "Kolkata"},
            verification=VerificationStatus.VERIFIED.value,
            availability="AVAILABLE",
            freshness=FreshnessStatus.FRESH.value,
            valid_from=now - timedelta(days=30),
            valid_until=now + timedelta(days=60),
            created_at=now,
            updated_at=now,
        )
        session.add(claim_kolkata)
        await session.flush()

        evidence_link = ClaimEvidenceModel(
            claim_id=claim_kolkata.id,
            observation_id=obs_kolkata.id,
            support_type="SUPPORTS",
            locator="article p",
            created_at=now,
        )
        session.add(evidence_link)
        await session.commit()

        # 5. Evaluate evidence eligibility on PostgreSQL
        gate = DeterministicEvidenceEligibilityGate(session=session)
        report = await gate.evaluate_candidate_eligibility(
            [pass_kolkata[0], pass_digha[0]],
            execution_id=exec_id,
            owner_id=owner_id,
        )

        # Place A (Kolkata) has verified evidence linked to its place_id -> eligible stop
        eligible_place_ids = {s.place_id for s in report.eligible_stops}
        assert pass_kolkata[0].place_id in eligible_place_ids

        # Place B (Digha, 140km away) has identical name, but evidence is linked only to Place A
        # Place B MUST NOT be verified by Place A's evidence
        assert pass_digha[0].place_id not in eligible_place_ids
        rec_place_ids = {r.place_id for r in report.editorial_recommendations}
        assert pass_digha[0].place_id in rec_place_ids


@pytest.mark.asyncio
async def test_graph_runner_trip_planner_candidate_lifecycle_on_postgresql(
    session_factory: Any,
) -> None:
    """Verifies candidate lifecycle through GraphRunnerService and the compiled graph on PostgreSQL.

    Proves:
    1. Runner provided session factory is used by evaluate_candidates_node.
    2. Genuine interrupt and resume lifecycle through LangGraph and PostgreSQL checkpointer.
    3. Candidate records persist in PostgreSQL after session closure.
    4. Linked verified evidence permits eligible stops into the solver schedule.
    5. Unverified candidate proposals cannot enter the solver schedule.
    6. Replay and resume does not duplicate registrations.
    7. Owner authorization and cancellation barriers protect persistent writes.
    """
    now = datetime.now(UTC)
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    exec_id = uuid.uuid4()

    brief_dest_name = f"Digha Beach {uuid.uuid4().hex[:6]}"
    verified_cand_name = f"Digha Science Center {uuid.uuid4().hex[:6]}"
    unverified_cand_name = f"Unverified Beach Shack {uuid.uuid4().hex[:6]}"

    # Phase 1: Seed parent trip and execution record in PostgreSQL
    async with session_factory() as setup_session:
        trip = TripModel(
            id=trip_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        setup_session.add(trip)

        execution = ExecutionModel(
            id=exec_id,
            owner_id=owner_id,
            trip_id=trip_id,
            workflow="PLANNING",
            status="CREATED",
            context_version=1,
            deadline_at=now + timedelta(hours=2),
            created_at=now,
            updated_at=now,
        )
        setup_session.add(execution)
        await setup_session.commit()

    # Phase 2: Run through GraphRunnerService with genuine interrupt at AWAIT_EVIDENCE_RESULTS
    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    compiled_graph = compile_trip_planning_workflow(checkpointer=checkpointer)
    runner = GraphRunnerService(
        session_factory=session_factory,
        default_graph=compiled_graph,
    )

    start_event = ExecutionStartEvent(
        execution_id=exec_id,
        workflow="PLANNING",
        context_version=1,
        payload={
            "owner_id": str(owner_id),
            "trip_id": str(trip_id),
            "static_search_completed": False,
            "remaining_search_budget": 1,
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": brief_dest_name,
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 1,
                        "confidence": 1.0,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-02",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "15000.00", "currency": "INR"},
            },
            "discovered_places": [
                {
                    "name": verified_cand_name,
                    "category": "attraction",
                    "lat": 21.6300,
                    "lng": 87.5100,
                    "stay_minutes": 60,
                },
                {
                    "name": unverified_cand_name,
                    "category": "restaurant",
                    "lat": 21.6400,
                    "lng": 87.5200,
                    "stay_minutes": 45,
                },
            ],
        },
    )

    start_result = await runner.handle_execution_start(event=start_event)
    # Proves genuine graph interrupt before AWAIT_EVIDENCE_RESULTS
    assert start_result.status == "WAITING_TASKS"
    assert start_result.graph_advanced is True

    # Phase 3: Seed verified observations and claims in PostgreSQL during WAITING_TASKS
    async with session_factory() as evidence_session:
        # Observation and claim for verified brief destination
        obs_brief = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            source_url="https://wbtourism.gov.in/digha_beach",
            canonical_url=f"https://wbtourism.gov.in/{brief_dest_name.lower().replace(' ', '_')}",
            publisher="wbtourism.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=5),
            excerpt_text=f"The {brief_dest_name} is the primary coastal beach destination in Digha.",
            excerpt_locator="article p",
            extraction_method="SCRAPLING_HTTP",
            artifact_ref={
                "status": "SUCCESS",
                "gps": {"lat": 21.6266, "lng": 87.5074},
            },
            created_at=now,
        )
        evidence_session.add(obs_brief)
        await evidence_session.flush()

        claim_brief = ClaimModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            subject_id=brief_dest_name,
            category=ClaimCategory.ATTRACTION.value,
            typed_value={"verified_name": brief_dest_name},
            verification=VerificationStatus.VERIFIED.value,
            availability="AVAILABLE",
            freshness=FreshnessStatus.FRESH.value,
            valid_from=now - timedelta(days=30),
            valid_until=now + timedelta(days=60),
            created_at=now,
            updated_at=now,
        )
        evidence_session.add(claim_brief)
        await evidence_session.flush()

        link_brief = ClaimEvidenceModel(
            claim_id=claim_brief.id,
            observation_id=obs_brief.id,
            support_type="SUPPORTS",
            locator="article p",
            created_at=now,
        )
        evidence_session.add(link_brief)

        # Observation and claim for verified discovered candidate
        obs_cand = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            source_url="https://wbtourism.gov.in/science_center",
            canonical_url=f"https://wbtourism.gov.in/{verified_cand_name.lower().replace(' ', '_')}",
            publisher="wbtourism.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=5),
            excerpt_text=f"The {verified_cand_name} is an active science center and planetarium near {brief_dest_name}.",
            excerpt_locator="article p",
            extraction_method="SCRAPLING_HTTP",
            artifact_ref={
                "status": "SUCCESS",
                "gps": {"lat": 21.6300, "lng": 87.5100},
            },
            created_at=now,
        )
        evidence_session.add(obs_cand)
        await evidence_session.flush()

        claim_cand = ClaimModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            subject_id=verified_cand_name,
            category=ClaimCategory.ATTRACTION.value,
            typed_value={"verified_name": verified_cand_name},
            verification=VerificationStatus.VERIFIED.value,
            availability="AVAILABLE",
            freshness=FreshnessStatus.FRESH.value,
            valid_from=now - timedelta(days=30),
            valid_until=now + timedelta(days=60),
            created_at=now,
            updated_at=now,
        )
        evidence_session.add(claim_cand)
        await evidence_session.flush()

        link_cand = ClaimEvidenceModel(
            claim_id=claim_cand.id,
            observation_id=obs_cand.id,
            support_type="SUPPORTS",
            locator="article p",
            created_at=now,
        )
        evidence_session.add(link_cand)

        # Complete dispatched evidence collection tasks so runner can resume
        await evidence_session.execute(
            update(TaskModel)
            .where(TaskModel.execution_id == exec_id)
            .values(status="COMPLETED", updated_at=now)
        )
        await evidence_session.commit()

    # Phase 4: Resume graph execution from persisted checkpoint in PostgreSQL
    resume_event = ExecutionResumeEvent(
        execution_id=exec_id,
        context_version=1,
        action="resume_graph",
    )
    resume_result = await runner.handle_execution_resume(event=resume_event)
    assert resume_result.status == "SUCCEEDED"

    # Phase 5: Inspect persisted state in a fresh database session
    async with session_factory() as verify_session:
        stmt = select(CanonicalPlaceModel).where(
            CanonicalPlaceModel.primary_name.in_(
                [
                    brief_dest_name,
                    verified_cand_name,
                    unverified_cand_name,
                ]
            )
        )
        persisted_places = (await verify_session.execute(stmt)).scalars().all()
        persisted_by_name = {p.primary_name: p for p in persisted_places}

        # Proves runner provided session factory was used and records persist after session closure
        assert brief_dest_name in persisted_by_name
        assert persisted_by_name[brief_dest_name].is_candidate is True
        assert persisted_by_name[brief_dest_name].geocoder_provenance == "EDITORIAL_DISCOVERY"

        assert verified_cand_name in persisted_by_name
        assert persisted_by_name[verified_cand_name].is_candidate is True

        assert unverified_cand_name in persisted_by_name
        assert persisted_by_name[unverified_cand_name].is_candidate is True

    # Phase 6: Verify graph schedule output
    graph_state = await compiled_graph.aget_state(
        config={"configurable": {"thread_id": str(exec_id)}}
    )
    state_vals = graph_state.values
    schedule = state_vals["optimized_schedule"]
    stops = schedule["stops"]
    stops_by_name = {s["name"]: s for s in stops}

    # Verified brief destination is scheduled with assumed daytime window
    assert brief_dest_name in stops_by_name
    assert stops_by_name[brief_dest_name]["is_hours_assumed"] is True
    assert stops_by_name[brief_dest_name]["hours_status"] == "HOURS_ASSUMED"
    assert stops_by_name[brief_dest_name]["window_start"] is not None
    assert stops_by_name[brief_dest_name]["window_end"] is not None

    # Linked verified evidence permits eligible discovered stop into the schedule
    assert verified_cand_name in stops_by_name
    assert stops_by_name[verified_cand_name]["is_hours_assumed"] is True
    assert stops_by_name[verified_cand_name]["hours_status"] == "HOURS_ASSUMED"

    # Unverified discovered candidate cannot enter the solver schedule
    assert unverified_cand_name not in stops_by_name

    rec_names = [
        (r.get("primary_name") if isinstance(r, dict) else getattr(r, "primary_name", ""))
        for r in state_vals.get("editorial_recommendations", [])
    ]
    assert unverified_cand_name in rec_names

    # Phase 7: Replay / Retry through handle_execution_resume does not duplicate records
    replay_result = await runner.handle_execution_resume(event=resume_event)
    assert replay_result.status == "SUCCEEDED"

    async with session_factory() as replay_session:
        stmt = select(CanonicalPlaceModel).where(
            CanonicalPlaceModel.primary_name.in_(
                [
                    brief_dest_name,
                    verified_cand_name,
                    unverified_cand_name,
                ]
            )
        )
        all_rows = (await replay_session.execute(stmt)).scalars().all()
        # Exactly 3 canonical places exist: no duplicate rows created
        assert len(all_rows) == 3

    # Phase 8: Caller supplying another owner real execution ID is rejected before persistent writes
    other_owner_id = uuid.uuid4()
    with pytest.raises(PermissionError):
        await evaluate_candidates_node(
            {
                "brief": {
                    "origin_name": "Kolkata",
                    "origin_lat": 22.5726,
                    "origin_lng": 88.3639,
                    "destinations": [
                        {
                            "name": brief_dest_name,
                            "latitude": 21.6266,
                            "longitude": 87.5074,
                            "stay_days": 1,
                            "confidence": 1.0,
                        }
                    ],
                    "start_date": "2026-11-01",
                    "end_date": "2026-11-02",
                    "travelers": 2,
                    "rooms": 1,
                    "budget": {"amount": "15000.00", "currency": "INR"},
                },
                "execution_id": str(exec_id),
                "owner_id": str(other_owner_id),
            },
            config={"configurable": {"session_factory": session_factory}},
        )


@pytest.mark.asyncio
async def test_name_only_claim_requires_established_location_in_postgresql(
    session_factory: Any,
) -> None:
    """Verifies that in PostgreSQL, name-only claims with no or invalid location cannot verify candidates,

    while established nearby locations verify matching candidates and cross-place links remain isolated.
    """
    async with session_factory() as session:
        exec_id, owner_id = await _create_test_execution_and_trip(session)
        service = CandidateRegistrationService(session=session)
        corridor = [
            CoordinatePoint(lat=22.5726, lng=88.3639),  # Kolkata
            CoordinatePoint(lat=21.6266, lng=87.5074),  # Digha (~140 km)
        ]

        shared_name = f"Radha Krishna Temple {uuid.uuid4().hex[:6]}"
        proposal_kolkata = PlaceProposal(
            name=shared_name,
            category="temple",
            estimated_latitude=22.5700,
            estimated_longitude=88.3600,
        )
        proposal_digha = PlaceProposal(
            name=shared_name,
            category="temple",
            estimated_latitude=21.6300,
            estimated_longitude=87.5100,
        )

        pass_kolkata, _ = await service.register_candidate_proposals(
            [proposal_kolkata], corridor, execution_id=exec_id, owner_id=owner_id
        )
        pass_digha, _ = await service.register_candidate_proposals(
            [proposal_digha], corridor, execution_id=exec_id, owner_id=owner_id
        )
        await session.commit()

        cand_k = pass_kolkata[0]
        cand_d = pass_digha[0]

        now = datetime.now(UTC)

        # 1. Observation with NO coordinates and a name-only claim (place_id=None)
        obs_no_coords = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            source_url="https://wbtourism.gov.in/temple_no_coords",
            canonical_url=f"https://wbtourism.gov.in/{shared_name.lower().replace(' ', '_')}_no_coords",
            publisher="wbtourism.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=2),
            excerpt_text=f"The famous {shared_name} is visited by pilgrims.",
            excerpt_locator="article p",
            extraction_method="SCRAPLING_HTTP",
            artifact_ref={"status": "SUCCESS"},  # No coordinates!
            created_at=now,
        )
        session.add(obs_no_coords)
        await session.flush()

        claim_name_only = ClaimModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            place_id=None,  # Name-only claim without place_id
            subject_id=shared_name,
            category=ClaimCategory.ATTRACTION.value,
            typed_value={"verified_name": shared_name},
            verification=VerificationStatus.VERIFIED.value,
            availability="AVAILABLE",
            freshness=FreshnessStatus.FRESH.value,
            valid_from=now - timedelta(days=30),
            valid_until=now + timedelta(days=60),
            created_at=now,
            updated_at=now,
        )
        session.add(claim_name_only)
        await session.flush()

        link_1 = ClaimEvidenceModel(
            claim_id=claim_name_only.id,
            observation_id=obs_no_coords.id,
            support_type="SUPPORTS",
            locator="article p",
            created_at=now,
        )
        session.add(link_1)
        await session.commit()

        gate = DeterministicEvidenceEligibilityGate(session=session)
        report_1 = await gate.evaluate_candidate_eligibility(
            [cand_k, cand_d],
            execution_id=exec_id,
            owner_id=owner_id,
        )

        # Neither candidate may be verified by name-only claim without established location
        assert len(report_1.eligible_stops) == 0
        assert len(report_1.editorial_recommendations) == 2

        # 2. Update observation with malformed / out-of-bounds coordinates
        obs_no_coords.artifact_ref = {
            "status": "SUCCESS",
            "gps": {"lat": 95.0, "lng": 88.3600},  # Latitude out of bounds [-90, 90]
        }
        await session.commit()

        report_2 = await gate.evaluate_candidate_eligibility(
            [cand_k, cand_d],
            execution_id=exec_id,
            owner_id=owner_id,
        )
        assert len(report_2.eligible_stops) == 0
        assert len(report_2.editorial_recommendations) == 2

        # 3. Update observation with valid nearby coordinates matching Kolkata (~30 meters away)
        obs_no_coords.artifact_ref = {
            "status": "SUCCESS",
            "gps": {"lat": 22.5702, "lng": 88.3602},
        }
        await session.commit()

        report_3 = await gate.evaluate_candidate_eligibility(
            [cand_k, cand_d],
            execution_id=exec_id,
            owner_id=owner_id,
        )

        # Kolkata is now verified via established location match (<= 1.0 km)
        eligible_pids = {s.place_id for s in report_3.eligible_stops}
        assert cand_k.place_id in eligible_pids
        # Digha (140 km away) remains unverified
        assert cand_d.place_id not in eligible_pids
        assert cand_d.place_id in {r.place_id for r in report_3.editorial_recommendations}

        # 4. A claim explicitly linked to another place ID must never verify Digha
        claim_other_place = ClaimModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            place_id=cand_k.place_id,  # Linked to Kolkata's place ID, not Digha
            subject_id=shared_name,
            category=ClaimCategory.ATTRACTION.value,
            typed_value={"verified_name": shared_name},
            verification=VerificationStatus.VERIFIED.value,
            availability="AVAILABLE",
            freshness=FreshnessStatus.FRESH.value,
            valid_from=now - timedelta(days=30),
            valid_until=now + timedelta(days=60),
            created_at=now,
            updated_at=now,
        )
        session.add(claim_other_place)
        await session.flush()

        link_other = ClaimEvidenceModel(
            claim_id=claim_other_place.id,
            observation_id=obs_no_coords.id,
            support_type="SUPPORTS",
            locator="article p",
            created_at=now,
        )
        session.add(link_other)
        await session.commit()

        report_4 = await gate.evaluate_candidate_eligibility(
            [cand_d],
            execution_id=exec_id,
            owner_id=owner_id,
        )
        assert len(report_4.eligible_stops) == 0
        assert len(report_4.editorial_recommendations) == 1
