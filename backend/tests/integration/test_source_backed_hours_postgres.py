"""PostgreSQL integration tests for source backed attractions and opening hours claims."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from contracts.candidate import (
    CandidateExistenceStatus,
    CandidateHoursStatus,
    RegisteredCandidate,
)
from functions.api.models import TripModel
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.claim_ingestion import ingest_claims_from_observations
from functions.orchestrator.evidence.eligibility import DeterministicEvidenceEligibilityGate
from functions.orchestrator.evidence.models import ClaimModel
from functions.orchestrator.evidence.schemas import ClaimCategory, VerificationStatus
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.places.schemas import CoordinatePoint


async def _seed_execution(session: Any) -> tuple[uuid.UUID, uuid.UUID]:
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)

    session.add(
        TripModel(
            id=trip_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    session.add(
        ExecutionModel(
            id=exec_id,
            owner_id=owner_id,
            trip_id=trip_id,
            workflow="TRAVEL",
            status="RUNNING",
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    return exec_id, owner_id


class TestSourceBackedHoursPostgres:
    """End to end claim ingestion and eligibility gate checks on PostgreSQL."""

    @pytest.mark.asyncio
    async def test_ingest_hours_with_cutoffs_rejects_wrong_city_and_snippet_only(
        self, session_factory: Any
    ) -> None:
        async with session_factory() as session:
            exec_id, owner_id = await _seed_execution(session)
            now = datetime.now(UTC)

            suffix = uuid.uuid4().hex[:6]
            bg_name = f"Government Botanical Garden {suffix}"
            rg_name = f"Government Rose Garden {suffix}"

            # 1. Attributable tourism page for Ooty Botanical Garden with closed day and last entry
            obs_official = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://nilgiris.nic.in/government-botanical-garden",
                canonical_url="https://nilgiris.nic.in/government-botanical-garden",
                publisher="Nilgiris District Administration",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now - timedelta(days=3),
                excerpt_text=(
                    f"{bg_name} Ooty open daily 07:00 AM - 06:30 PM. "
                    "Ticketing hours: 07:00 AM - 05:30 PM. Last entry at 06:00 PM."
                ),
                excerpt_locator="main:text",
                extraction_method="SCRAPLING_HTTP",
                artifact_ref={
                    "category": "attraction",
                    "status": "SUCCESS",
                    "page_title": bg_name,
                    "city_hint": "Ooty",
                    "latitude": 11.4189,
                    "longitude": 76.7115,
                },
                created_at=now,
            )

            # 2. Wrong city observation without coordinates (Kolkata garden) must be rejected
            obs_wrong_city = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://wb.gov.in/botanical-garden-kolkata",
                canonical_url="https://wb.gov.in/botanical-garden-kolkata",
                publisher="WB Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now - timedelta(days=2),
                excerpt_text=f"{bg_name} Kolkata open Tuesday to Sunday 06:00 AM - 05:00 PM. Closed on Monday.",
                excerpt_locator="main:text",
                extraction_method="SCRAPLING_HTTP",
                artifact_ref={
                    "category": "attraction",
                    "status": "SUCCESS",
                    "page_title": bg_name,
                    "city_hint": "Kolkata",
                    "address": "Shibpur, Kolkata, West Bengal",
                },
                created_at=now,
            )

            # 3. Search snippet only hours for Rose Garden must NOT produce VERIFIED_HOURS
            obs_snippet = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://example.org/rose-garden-snippet",
                canonical_url="https://example.org/rose-garden-snippet",
                publisher="SerpAPI Search",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now,
                excerpt_text=f"{rg_name} Ooty Open daily 08:30 AM - 06:30 PM",
                excerpt_locator="serpapi:snippet",
                extraction_method="SERPAPI_SEARCH",
                artifact_ref={
                    "category": "attraction",
                    "status": "SUCCESS",
                    "title": rg_name,
                    "latitude": 11.4041,
                    "longitude": 76.7081,
                },
                created_at=now,
            )

            session.add_all([obs_official, obs_wrong_city, obs_snippet])
            await session.commit()

            candidates = [
                RegisteredCandidate(
                    primary_name=bg_name,
                    category="garden",
                    point=CoordinatePoint(lat=11.4189, lng=76.7115),
                ),
                RegisteredCandidate(
                    primary_name=rg_name,
                    category="garden",
                    point=CoordinatePoint(lat=11.4041, lng=76.7081),
                ),
            ]

            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=candidates,
                owner_id=owner_id,
                expected_cities={"Ooty", "Udhagamandalam", "Nilgiris"},
                local_timezone="Asia/Kolkata",
                include_hours_claims=True,
            )
            await session.commit()

            hours_claims = list(
                (
                    await session.execute(
                        select(ClaimModel).where(
                            ClaimModel.execution_id == exec_id,
                            ClaimModel.category == ClaimCategory.HOURS.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            # Only Botanical Garden official page yields a persisted HOURS claim; Kolkata and Rose Garden snippet do not
            assert len(hours_claims) == 1
            botanical_hours = hours_claims[0]
            assert botanical_hours.subject_id == bg_name.lower()
            assert botanical_hours.verification == VerificationStatus.VERIFIED
            assert botanical_hours.typed_value["ticketing_closes"] == "17:30"
            assert botanical_hours.typed_value["last_entry"] == "18:00"
            assert botanical_hours.typed_value["local_timezone"] == "Asia/Kolkata"
            assert "wb.gov.in" not in str(botanical_hours.typed_value)
            assert "Shibpur" not in str(botanical_hours.typed_value)
            assert claims

        async with session_factory() as eval_session:
            gate = DeterministicEvidenceEligibilityGate(session=eval_session)
            report = await gate.evaluate_candidate_eligibility(
                candidates=candidates,
                execution_id=exec_id,
                owner_id=owner_id,
                visit_dates=[date(2026, 12, 5)],
                local_timezone="Asia/Kolkata",
            )
            by_name = {c.primary_name: c for c in report.eligible_stops}
            assert by_name[bg_name].existence_status == CandidateExistenceStatus.VERIFIED
            assert by_name[bg_name].hours_status == CandidateHoursStatus.VERIFIED_HOURS
            assert by_name[bg_name].window_start == time(7, 0)
            assert by_name[bg_name].window_end == time(18, 30)
            assert by_name[bg_name].hours_details["ticketing_closes"] == "17:30"
            assert by_name[bg_name].hours_details["last_entry"] == "18:00"

            assert by_name[rg_name].hours_status != CandidateHoursStatus.VERIFIED_HOURS
            assert by_name[rg_name].is_hours_assumed is True
