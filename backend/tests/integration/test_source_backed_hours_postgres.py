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

    @pytest.mark.asyncio
    async def test_seo_title_attribution_positive_and_multi_venue_or_wrong_city_negatives(
        self, session_factory: Any
    ) -> None:
        """SEO-titled pages verify hours only when excerpt+locality match; multi-venue, cafe, and wrong-city pages do not."""
        # Positive: SCRAPLING_HTTP and BROWSER_HTML with SEO titles attribute hours to the matching venue
        async with session_factory() as session:
            exec_id, owner_id = await _seed_execution(session)
            now = datetime.now(UTC)
            suffix = uuid.uuid4().hex[:6]
            bg_name = f"Government Botanical Garden {suffix}"

            session.add_all(
                [
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=exec_id,
                        source_url=f"https://nilgiris.nic.in/place-{suffix}",
                        canonical_url=f"https://nilgiris.nic.in/place-{suffix}",
                        publisher="google_maps",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=f"Place: {bg_name} | Address: Vannarapettai, Ooty, Tamil Nadu",
                        excerpt_locator="serpapi:place_results",
                        extraction_method="SERPAPI_SEARCH",
                        artifact_ref={
                            "title": bg_name,
                            "address": "Vannarapettai, Ooty, Tamil Nadu",
                            "gps_coordinates": {"latitude": 11.4189, "longitude": 76.7114},
                            "query_purpose": "attractions",
                        },
                        created_at=now,
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=exec_id,
                        source_url=f"https://www.tourdeooty.com/bg-{suffix}.html",
                        canonical_url=f"https://www.tourdeooty.com/bg-{suffix}.html",
                        publisher="tourdeooty.com",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"{bg_name} Entrance Timings: 7:00 AM - 6:30 PM. "
                            f"{bg_name} Ooty: 7:00 AM - 6:30 PM. "
                            f"Monday: 07:00 AM - 06:30 PM. Tuesday: Closed. "
                            f"The {bg_name} in Ooty is open Monday to Sunday 7:00 AM to 6:30 PM. "
                            "Nearest airport is Kempegowda International Airport in Bengaluru; buses from Bengaluru and Coimbatore."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "page_title": f"{bg_name} - Timings, Entry Fee, Location & Hotels Nearby",
                            "query_purpose": "hours",
                        },
                        created_at=now,
                    ),
                ]
            )
            await session.commit()

            candidates = [
                RegisteredCandidate(
                    primary_name=bg_name,
                    category="nature",
                    point=CoordinatePoint(lat=11.4189, lng=76.7114),
                    existence_status=CandidateExistenceStatus.VERIFIED,
                    is_must_visit=True,
                )
            ]
            await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                owner_id=owner_id,
                candidates=candidates,
                expected_cities={"ooty", "udhagamandalam"},
                include_hours_claims=True,
            )
            await session.commit()

        async with session_factory() as eval_session:
            gate = DeterministicEvidenceEligibilityGate(session=eval_session)
            report = await gate.evaluate_candidate_eligibility(
                candidates=candidates,
                execution_id=exec_id,
                owner_id=owner_id,
                visit_dates=[date(2026, 12, 5)],
                local_timezone="Asia/Kolkata",
            )
            stop = (report.eligible_stops + report.editorial_recommendations)[0]
            from functions.orchestrator.evidence.claim_ingestion import (
                _scope_page_text_to_candidate,
            )

            assert stop.hours_status == CandidateHoursStatus.VERIFIED_HOURS
            assert stop.window_start == time(7, 0)
            assert stop.window_end == time(18, 30)
            assert "Tuesday: Closed." in _scope_page_text_to_candidate(
                "Government Botanical Garden is open 09:00 AM - 05:00 PM. Tuesday: Closed.",
                "Government Botanical Garden",
            )
            assert "Monday: 07:00 AM - 06:30 PM" in _scope_page_text_to_candidate(
                "Government Botanical Garden Ooty Timings:\nMonday: 07:00 AM - 06:30 PM\nTuesday: 07:00 AM - 06:30 PM",
                "Government Botanical Garden",
                expected_cities={"ooty"},
            )

        # Positive 2: BROWSER_HTML with plural/info SEO title attributes hours to the matching venue
        async with session_factory() as browser_session:
            b_exec_id, b_owner_id = await _seed_execution(browser_session)
            now = datetime.now(UTC)
            suffix = uuid.uuid4().hex[:6]
            b_name = f"Government Botanical Garden {suffix}"
            browser_session.add_all(
                [
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=b_exec_id,
                        source_url=f"https://nilgiris.nic.in/bplace-{suffix}",
                        canonical_url=f"https://nilgiris.nic.in/bplace-{suffix}",
                        publisher="google_maps",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=f"Place: {b_name} | Address: Vannarapettai, Ooty, Tamil Nadu",
                        excerpt_locator="serpapi:place_results",
                        extraction_method="SERPAPI_SEARCH",
                        artifact_ref={
                            "title": b_name,
                            "address": "Vannarapettai, Ooty, Tamil Nadu",
                            "gps_coordinates": {"latitude": 11.4189, "longitude": 76.7114},
                            "query_purpose": "attractions",
                        },
                        created_at=now,
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=b_exec_id,
                        source_url=f"https://ootytourism.co.in/bg-{suffix}",
                        canonical_url=f"https://ootytourism.co.in/bg-{suffix}",
                        publisher="ootytourism.co.in",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"Ooty Botanical Gardens Timings Monday to Sunday 7:00 am – 6:30 pm. "
                            f"{b_name} is a famous sightseeing place in Ooty."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="BROWSER_HTML",
                        artifact_ref={
                            "page_title": f"Botanical Gardens Ooty {suffix} (Timings, Entry Fee, Entrance Ticket Cost)",
                            "query_purpose": "hours",
                        },
                        created_at=now,
                    ),
                ]
            )
            await browser_session.commit()
            b_candidates = [
                RegisteredCandidate(
                    primary_name=b_name,
                    category="nature",
                    point=CoordinatePoint(lat=11.4189, lng=76.7114),
                    existence_status=CandidateExistenceStatus.VERIFIED,
                    is_must_visit=True,
                )
            ]
            await ingest_claims_from_observations(
                session=browser_session,
                execution_id=b_exec_id,
                owner_id=b_owner_id,
                candidates=b_candidates,
                expected_cities={"ooty", "udhagamandalam"},
                include_hours_claims=True,
            )
            await browser_session.commit()

        async with session_factory() as b_eval_session:
            b_gate = DeterministicEvidenceEligibilityGate(session=b_eval_session)
            b_report = await b_gate.evaluate_candidate_eligibility(
                candidates=b_candidates,
                execution_id=b_exec_id,
                owner_id=b_owner_id,
                visit_dates=[date(2026, 12, 5)],
                local_timezone="Asia/Kolkata",
            )
            b_stop = (b_report.eligible_stops + b_report.editorial_recommendations)[0]
            assert b_stop.hours_status == CandidateHoursStatus.VERIFIED_HOURS
            assert b_stop.window_start == time(7, 0)
            assert b_stop.window_end == time(18, 30)

        # Negative: Multi-venue distance/listicle, distinct-entity Cafe title, and wrong-city past char 300 must not verify
        async with session_factory() as neg_session:
            neg_exec_id, neg_owner_id = await _seed_execution(neg_session)
            now = datetime.now(UTC)
            suffix = uuid.uuid4().hex[:6]
            bg_name = f"Government Botanical Garden {suffix}"
            padding = " ".join(["Lush"] * 90)

            neg_session.add_all(
                [
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://nilgiris.nic.in/neg-place-{suffix}",
                        canonical_url=f"https://nilgiris.nic.in/neg-place-{suffix}",
                        publisher="google_maps",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=f"Place: {bg_name} | Address: Vannarapettai, Ooty, Tamil Nadu",
                        excerpt_locator="serpapi:place_results",
                        extraction_method="SERPAPI_SEARCH",
                        artifact_ref={
                            "title": bg_name,
                            "address": "Vannarapettai, Ooty, Tamil Nadu",
                            "gps_coordinates": {"latitude": 11.4189, "longitude": 76.7114},
                            "query_purpose": "attractions",
                        },
                        created_at=now,
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://guide.example/multi-{suffix}",
                        canonical_url=f"https://guide.example/multi-{suffix}",
                        publisher="guide.example",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"{bg_name} is 2 km from Rose Garden. Rose Garden is open 09:00 AM to 05:00 PM."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "page_title": "Ooty Gardens Guide - Timings & Hotels Nearby",
                            "query_purpose": "hours",
                        },
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://list.example/top10-{suffix}",
                        canonical_url=f"https://list.example/top10-{suffix}",
                        publisher="list.example",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"Places to Visit in Ooty: {bg_name} is a major landmark in Ooty. "
                            "Doddabetta Peak: Open 07:00 AM to 06:00 PM."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "page_title": "Top 10 Places to Visit in Ooty",
                            "query_purpose": "hours",
                        },
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://cafe.example/garden-{suffix}",
                        canonical_url=f"https://cafe.example/garden-{suffix}",
                        publisher="cafe.example",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"Our cozy cafe sits right inside the {bg_name} in Ooty and serves coffee from 08:00 AM to 09:00 PM daily."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "page_title": f"Garden View Cafe at {bg_name}",
                            "query_purpose": "hours",
                        },
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://ooty-nav.example/botanical-{suffix}",
                        canonical_url=f"https://ooty-nav.example/botanical-{suffix}",
                        publisher="ooty-nav.example",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"Explore {bg_name} {padding} Located in Shibpur, Kolkata, West Bengal. Open 06:00 AM to 05:00 PM."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "page_title": f"{bg_name} Timings",
                            "query_purpose": "hours",
                        },
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://rose.example/garden-{suffix}",
                        canonical_url=f"https://rose.example/garden-{suffix}",
                        publisher="rose.example",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"{bg_name} is near Rose Garden, which is open 08:30 AM to 06:30 PM. "
                            f"{bg_name} is in Ooty while Rose Garden is open 08:30 AM to 06:30 PM. "
                            "Rose Garden opening hours: 08:30 AM - 06:30 PM. "
                            "Doddabetta Peak opens 7:00 AM to 6:00 PM. Rose Garden (Ooty): 08:30 AM - 06:30 PM. "
                            "1. Doddabetta Peak: Open 07:00 AM to 06:00 PM. It is open daily."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "page_title": f"{bg_name} - Timings & Nearby Attractions",
                            "query_purpose": "hours",
                        },
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://compare.example/vs-{suffix}",
                        canonical_url=f"https://compare.example/vs-{suffix}",
                        publisher="compare.example",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"{bg_name} Ooty vs Kolkata Botanical Garden. Kolkata garden is open 06:00 AM to 05:00 PM."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "city_hint": "Ooty",
                            "page_title": f"{bg_name} Ooty - Timings",
                            "query_purpose": "hours",
                        },
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://howrah.example/station-{suffix}",
                        canonical_url=f"https://howrah.example/station-{suffix}",
                        publisher="howrah.example",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"{bg_name} is near Nearest railway station is Howrah, Kolkata. Open 06:00 AM to 05:00 PM daily."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "page_title": f"{bg_name} - Timings",
                            "query_purpose": "hours",
                        },
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://eqlen.example/rose-{suffix}",
                        canonical_url=f"https://eqlen.example/rose-{suffix}",
                        publisher="eqlen.example",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"Open 08:30 AM to 06:30 PM daily. Located 2 km away from the {bg_name}."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "page_title": f"Government Rose Garden {suffix}",
                            "query_purpose": "hours",
                        },
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://hotels.example/nearby-{suffix}",
                        canonical_url=f"https://hotels.example/nearby-{suffix}",
                        publisher="hotels.example",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"Located 2 km from {bg_name}, this property has front desk hours 08:00 AM - 10:00 PM."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "page_title": f"{bg_name} - Hotels Nearby",
                            "query_purpose": "hours",
                        },
                    ),
                    SourceObservationModel(
                        id=uuid.uuid4(),
                        execution_id=neg_exec_id,
                        source_url=f"https://compare2.example/vs-{suffix}",
                        canonical_url=f"https://compare2.example/vs-{suffix}",
                        publisher="compare2.example",
                        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        retrieved_at=now,
                        excerpt_text=(
                            f"Kolkata vs Ooty {bg_name}: open 06:00 AM to 05:00 PM."
                        ),
                        excerpt_locator="main:text",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={
                            "city_hint": "Ooty",
                            "page_title": f"{bg_name} Ooty - Timings",
                            "query_purpose": "hours",
                        },
                    ),
                ]
            )
            await neg_session.commit()

            neg_candidates = [
                RegisteredCandidate(
                    primary_name=bg_name,
                    category="nature",
                    point=CoordinatePoint(lat=11.4189, lng=76.7114),
                    existence_status=CandidateExistenceStatus.VERIFIED,
                    is_must_visit=True,
                )
            ]
            await ingest_claims_from_observations(
                session=neg_session,
                execution_id=neg_exec_id,
                owner_id=neg_owner_id,
                candidates=neg_candidates,
                expected_cities={"ooty", "udhagamandalam"},
                include_hours_claims=True,
            )
            await neg_session.commit()

            neg_hours_claims = (
                (
                    await neg_session.execute(
                        select(ClaimModel).where(
                            ClaimModel.execution_id == neg_exec_id,
                            ClaimModel.category == ClaimCategory.HOURS.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(neg_hours_claims) == 0

            neg_gate = DeterministicEvidenceEligibilityGate(session=neg_session)
            neg_report = await neg_gate.evaluate_candidate_eligibility(
                candidates=neg_candidates,
                execution_id=neg_exec_id,
                owner_id=neg_owner_id,
                visit_dates=[date(2026, 12, 5)],
                local_timezone="Asia/Kolkata",
            )
            neg_stop = (neg_report.eligible_stops + neg_report.editorial_recommendations)[0]
            assert neg_stop.hours_status != CandidateHoursStatus.VERIFIED_HOURS
            assert neg_stop.is_hours_assumed is True
