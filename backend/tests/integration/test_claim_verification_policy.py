"""Integration tests for claim verification policy, freshness, and conflict preservation.

Conforms to spec 0002 AC-9, AC-10, AC-11, and AC-12 on live PostgreSQL.
Verifies domain authority gates, side by side conflict preservation,
and freshness expiration without altering historical verification records.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from functions.api.models import TripModel
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.models import ClaimEvidenceModel, ClaimModel
from functions.orchestrator.evidence.policy import ClaimVerificationPolicy
from functions.orchestrator.evidence.repository import ClaimsRepository
from functions.orchestrator.evidence.schemas import (
    AvailabilityStatus,
    ClaimCategory,
    ClaimCreate,
    EvidenceSupportType,
    FreshnessStatus,
    VerificationStatus,
)
from functions.orchestrator.evidence.service import ClaimVerificationService
from functions.orchestrator.execution.models import ExecutionModel


async def create_test_execution_and_observation(
    session: Any,
    canonical_url: str,
    publisher: str,
    published_at: datetime | None = None,
) -> tuple[uuid.UUID, SourceObservationModel]:
    """Helper creating a test execution and persisted source observation."""
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

    obs = SourceObservationModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        source_url=canonical_url,
        canonical_url=canonical_url,
        publisher=publisher,
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        retrieved_at=now,
        published_at=published_at or now,
        excerpt_text="Traffic advisory excerpt text for testing.",
        excerpt_locator="p.advisory",
        extraction_method="SCRAPLING_HTTP",
        created_at=now,
    )
    session.add(obs)
    await session.flush()
    return execution_id, obs


@pytest.mark.asyncio
class TestClaimVerificationPolicyIntegration:
    """Integration test suite verifying deterministic policy behavior on live PostgreSQL."""

    async def test_authoritative_police_advisory_verifies_road_closure(
        self, session_factory: Any
    ) -> None:
        """AC-9 & AC-10: Official police advisory verifies road closure and sets availability to UNAVAILABLE."""
        async with session_factory() as session:
            now = datetime.now(UTC)
            exec_id, obs = await create_test_execution_and_observation(
                session=session,
                canonical_url="https://kolkatatrafficpolice.gov.in/durga-puja-closure-2026.html",
                publisher="Kolkata Traffic Police",
                published_at=now - timedelta(hours=2),
            )

            service = ClaimVerificationService(session)
            claim_data = ClaimCreate(
                execution_id=exec_id,
                subject_id="central_avenue_closure",
                category=ClaimCategory.RESTRICTION,
                typed_value={"status": "CLOSED", "reason": "Durga Puja Immersion"},
            )

            created_claim = await service.submit_claim(
                claim_data=claim_data,
                observations_with_support=[(obs, EvidenceSupportType.SUPPORTS, "p.advisory")],
            )
            await session.commit()

            assert created_claim.verification == VerificationStatus.VERIFIED.value
            assert created_claim.availability == AvailabilityStatus.UNAVAILABLE.value
            assert created_claim.freshness == FreshnessStatus.FRESH.value

            # Query database directly to confirm persisted state and evidence junction
            stmt = select(ClaimModel).where(ClaimModel.id == created_claim.id)
            res = await session.execute(stmt)
            persisted = res.scalar_one()
            assert persisted.verification == "VERIFIED"
            assert persisted.availability == "UNAVAILABLE"

            ev_stmt = select(ClaimEvidenceModel).where(ClaimEvidenceModel.claim_id == created_claim.id)
            ev_res = await session.execute(ev_stmt)
            ev_links = list(ev_res.scalars().all())
            assert len(ev_links) == 1
            assert ev_links[0].observation_id == obs.id
            assert ev_links[0].support_type == "SUPPORTS"

    async def test_secondary_blog_mention_remains_unverified_for_critical_category(
        self, session_factory: Any
    ) -> None:
        """AC-10: Blog mentions of road closure remain UNVERIFIED while availability is evaluated."""
        async with session_factory() as session:
            now = datetime.now(UTC)
            exec_id, obs = await create_test_execution_and_observation(
                session=session,
                canonical_url="https://kolkatablog.example.com/traffic-updates.html",
                publisher="City Travel Blog",
                published_at=now - timedelta(hours=1),
            )

            service = ClaimVerificationService(session)
            claim_data = ClaimCreate(
                execution_id=exec_id,
                subject_id="red_road_advisory",
                category=ClaimCategory.RESTRICTION,
                typed_value={"status": "CLOSED", "reason": "Parade rehearsal"},
            )

            created_claim = await service.submit_claim(
                claim_data=claim_data,
                observations_with_support=[(obs, EvidenceSupportType.SUPPORTS, "div.blog-post")],
            )
            await session.commit()

            # Blog evidence cannot verify critical road closures
            assert created_claim.verification == VerificationStatus.UNVERIFIED.value
            assert created_claim.availability == AvailabilityStatus.UNAVAILABLE.value

    async def test_side_by_side_conflict_preservation(self, session_factory: Any) -> None:
        """AC-11: Conflicting claims are preserved side by side as CONFLICTED without data loss."""
        async with session_factory() as session:
            now = datetime.now(UTC)
            subject = f"park_street_walkway_{uuid.uuid4().hex[:6]}"

            # 1. First claim asserts open
            exec_id_1, obs_1 = await create_test_execution_and_observation(
                session=session,
                canonical_url="https://tourism.example.com/park-street-hours",
                publisher="Tourism Guide",
                published_at=now - timedelta(hours=4),
            )
            service = ClaimVerificationService(session)
            claim_1 = await service.submit_claim(
                claim_data=ClaimCreate(
                    execution_id=exec_id_1,
                    subject_id=subject,
                    category=ClaimCategory.HOURS,
                    typed_value={"status": "OPEN", "open": True, "hours": "08:00 - 22:00"},
                ),
                observations_with_support=[(obs_1, EvidenceSupportType.SUPPORTS, "p.hours")],
            )
            await session.commit()
            assert claim_1.verification == VerificationStatus.VERIFIED.value

            # 2. Second claim asserts closed for the exact same subject and category
            exec_id_2, obs_2 = await create_test_execution_and_observation(
                session=session,
                canonical_url="https://kolkatatrafficpolice.gov.in/special-closure",
                publisher="Kolkata Police",
                published_at=now - timedelta(hours=1),
            )
            claim_2 = await service.submit_claim(
                claim_data=ClaimCreate(
                    execution_id=exec_id_2,
                    subject_id=subject,
                    category=ClaimCategory.HOURS,
                    typed_value={"status": "CLOSED", "open": False, "hours": "CLOSED"},
                ),
                observations_with_support=[(obs_2, EvidenceSupportType.SUPPORTS, "div.notice")],
            )
            await session.commit()

            # 3. Reload both claims from database to verify side by side preservation
            repo = ClaimsRepository(session)
            loaded_1 = await repo.get_by_id(claim_1.id)
            loaded_2 = await repo.get_by_id(claim_2.id)

            assert loaded_1 is not None
            assert loaded_2 is not None

            # Both claims transition to CONFLICTED
            assert loaded_1.verification == VerificationStatus.CONFLICTED.value
            assert loaded_2.verification == VerificationStatus.CONFLICTED.value

            # Cross references exist in contradictions lists
            refs_1 = [c.get("claim_id") for c in loaded_1.contradictions]
            refs_2 = [c.get("claim_id") for c in loaded_2.contradictions]
            assert str(loaded_2.id) in refs_1
            assert str(loaded_1.id) in refs_2

    async def test_multiple_contradictions_batch_update(
        self, session_factory: Any
    ) -> None:
        """Nit 2: Verifies record_contradictions_batch handles multiple conflicting claims in one flush."""
        async with session_factory() as session:
            subject = f"venue_{uuid.uuid4().hex[:8]}"

            exec_id_1, obs_1 = await create_test_execution_and_observation(
                session=session,
                canonical_url="https://tourism.example.com/site",
                publisher="Guide A",
            )
            exec_id_2, obs_2 = await create_test_execution_and_observation(
                session=session,
                canonical_url="https://tourism2.example.com/site",
                publisher="Guide B",
            )
            exec_id_3, obs_3 = await create_test_execution_and_observation(
                session=session,
                canonical_url="https://kolkatatrafficpolice.gov.in/site-closed",
                publisher="Police",
            )

            service = ClaimVerificationService(session)
            # Two claims with status OPEN
            c1 = await service.submit_claim(
                claim_data=ClaimCreate(
                    execution_id=exec_id_1,
                    subject_id=subject,
                    category=ClaimCategory.RESTRICTION,
                    typed_value={"status": "OPEN", "open": True},
                ),
                observations_with_support=[(obs_1, EvidenceSupportType.SUPPORTS, "p.info")],
            )
            c2 = await service.submit_claim(
                claim_data=ClaimCreate(
                    execution_id=exec_id_2,
                    subject_id=subject,
                    category=ClaimCategory.RESTRICTION,
                    typed_value={"status": "OPEN", "open": True},
                ),
                observations_with_support=[(obs_2, EvidenceSupportType.SUPPORTS, "p.info")],
            )
            await session.commit()

            # Third claim contradicts both c1 and c2 with status CLOSED
            c3 = await service.submit_claim(
                claim_data=ClaimCreate(
                    execution_id=exec_id_3,
                    subject_id=subject,
                    category=ClaimCategory.RESTRICTION,
                    typed_value={"status": "CLOSED", "open": False},
                ),
                observations_with_support=[(obs_3, EvidenceSupportType.SUPPORTS, "p.closed")],
            )
            await session.commit()

            repo = ClaimsRepository(session)
            l1 = await repo.get_by_id(c1.id)
            l2 = await repo.get_by_id(c2.id)
            l3 = await repo.get_by_id(c3.id)

            assert l1 is not None and l2 is not None and l3 is not None
            assert l1.verification == VerificationStatus.CONFLICTED.value
            assert l2.verification == VerificationStatus.CONFLICTED.value
            assert l3.verification == VerificationStatus.CONFLICTED.value

            # Check that c3 points to both c1 and c2
            c3_refs = {item.get("claim_id") for item in l3.contradictions}
            assert str(l1.id) in c3_refs
            assert str(l2.id) in c3_refs


    async def test_claim_freshness_expiration_to_stale_without_altering_verification(
        self, session_factory: Any
    ) -> None:
        """AC-12: Expired validity transitions freshness to STALE without touching historical verification."""
        async with session_factory() as session:
            now = datetime.now(UTC)
            exec_id, obs = await create_test_execution_and_observation(
                session=session,
                canonical_url="https://kolkatatrafficpolice.gov.in/past-advisory",
                publisher="Kolkata Police",
                published_at=now - timedelta(hours=36),
            )

            service = ClaimVerificationService(session)
            claim_data = ClaimCreate(
                execution_id=exec_id,
                subject_id="old_traffic_diversion",
                category=ClaimCategory.RESTRICTION,
                typed_value={"status": "CLOSED"},
            )

            # When submitted as of 36 hours ago, it was fresh
            past_time = now - timedelta(hours=35)
            policy_past = ClaimVerificationPolicy()
            service_past = ClaimVerificationService(session, policy=policy_past)

            claim = await service_past.submit_claim(
                claim_data=claim_data,
                observations_with_support=[(obs, EvidenceSupportType.SUPPORTS, "p.notice")],
                as_of=past_time,
            )
            await session.commit()
            assert claim.freshness == FreshnessStatus.FRESH.value

            # Re evaluate freshness as of current time (36 hours elapsed, exceeding 24h limit)
            updated_claim = await service.recheck_claim_freshness(claim.id, as_of=now)
            await session.commit()

            assert updated_claim.freshness == FreshnessStatus.STALE.value
            # AC-12: historical verification remains intact!
            assert updated_claim.verification == VerificationStatus.VERIFIED.value

            # Verify history records the transition
            history_events = [item.get("event") for item in updated_claim.decision_history]
            assert "FRESHNESS_TRANSITION" in history_events

    async def test_generative_model_proposal_remains_unverified_on_db(
        self, session_factory: Any
    ) -> None:
        """AC-9 & Invariant 5: Proposed claim without observations persists as UNVERIFIED on PostgreSQL."""
        async with session_factory() as session:
            exec_id, _ = await create_test_execution_and_observation(
                session=session,
                canonical_url="https://tourism.example.com/unrelated",
                publisher="Unrelated",
            )
            service = ClaimVerificationService(session)
            claim_data = ClaimCreate(
                execution_id=exec_id,
                subject_id="proposed_venue_hours",
                category=ClaimCategory.HOURS,
                typed_value={"status": "OPEN", "hours": "09:00 - 18:00"},
            )

            # Submitting claim with zero supporting observations
            created_claim = await service.submit_claim(
                claim_data=claim_data,
                observations_with_support=[],
            )
            await session.commit()

            assert created_claim.verification == VerificationStatus.UNVERIFIED.value
            assert created_claim.availability == AvailabilityStatus.AVAILABLE.value
            assert created_claim.freshness == FreshnessStatus.UNKNOWN.value
