"""Unit tests for place candidate registration, spatial corridor validation, and evidence eligibility.

Conforms to spec 0007 AC-1, AC-3, and Slice 3 requirements:
1. Treat LLM output as editorial candidate proposals, never verified facts.
2. Register or resolve candidates using existing canonical place interfaces preserving provenance and idempotency.
3. A coordinate or corridor distance match alone must not prove existence.
4. Unverified or contradicted places must not enter the route solver.
5. A place with verified existence but unknown opening hours may use an explicitly labeled HOURS_ASSUMED window.
6. Preserve stale, conflicting, unavailable, and missing evidence states.
7. Test valid evidence, missing evidence, contradicted claims, unknown hours, malformed coordinates, duplicates, and tenant isolation.
8. Use deterministic offline fixtures without claiming live LAYA or hosted LLM verification.
"""

from __future__ import annotations

import uuid
from datetime import time
from typing import Any

import pytest

from contracts.candidate import (
    CandidateExistenceStatus,
    CandidateHoursStatus,
    CorridorValidationStatus,
    RegisteredCandidate,
)
from contracts.llm import PlaceProposal
from functions.orchestrator.evidence.eligibility import (
    DEFAULT_ASSUMED_WINDOW_END,
    DEFAULT_ASSUMED_WINDOW_START,
    DeterministicEvidenceEligibilityGate,
)
from functions.orchestrator.evidence.schemas import (
    ClaimCategory,
    EvidenceSupportType,
    FreshnessStatus,
    VerificationStatus,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    solve_schedule_node,
    synthesize_proposal_node,
)
from functions.orchestrator.places.candidate_service import (
    CandidateRegistrationService,
)
from functions.orchestrator.places.corridor import (
    haversine_distance_km,
    point_to_segment_distance_km,
    validate_candidate_corridor,
    validate_coordinates,
)
from functions.orchestrator.places.schemas import CoordinatePoint


class TestCorridorSpatialValidation:
    """Tests coordinate validation and corridor distance mathematics."""

    def test_coordinate_validation_valid(self) -> None:
        valid, err = validate_coordinates(22.5726, 88.3639)
        assert valid is True
        assert err is None

    def test_coordinate_validation_out_of_bounds_latitude(self) -> None:
        valid, err = validate_coordinates(95.0, 88.3639)
        assert valid is False
        assert "Latitude 95.0 out of bounds" in str(err)

        valid_neg, err_neg = validate_coordinates(-90.5, 88.3639)
        assert valid_neg is False
        assert "Latitude -90.5 out of bounds" in str(err_neg)

    def test_coordinate_validation_out_of_bounds_longitude(self) -> None:
        valid, err = validate_coordinates(22.5, 185.0)
        assert valid is False
        assert "Longitude 185.0 out of bounds" in str(err)

    def test_coordinate_validation_non_numeric_and_nan(self) -> None:
        valid, err = validate_coordinates(float("nan"), 88.3639)
        assert valid is False
        assert "finite" in str(err)

        valid_none, err_none = validate_coordinates(None, 88.3639)
        assert valid_none is False
        assert "None" in str(err_none)

        valid_str, err_str = validate_coordinates("invalid", 88.3639)
        assert valid_str is False
        assert "numeric" in str(err_str)

    def test_haversine_distance_accuracy(self) -> None:
        # Kolkata (22.5726, 88.3639) to Digha (21.6266, 87.5074) ~ 138km
        dist = haversine_distance_km(22.5726, 88.3639, 21.6266, 87.5074)
        assert 130.0 < dist < 150.0

    def test_point_to_segment_distance(self) -> None:
        # Segment from Kolkata to Digha
        # Kharagpur (22.3460, 87.2320) is roughly 40-60km off this direct line
        dist = point_to_segment_distance_km(22.3460, 87.2320, 22.5726, 88.3639, 21.6266, 87.5074)
        assert 30.0 < dist < 80.0

    def test_candidate_within_corridor(self) -> None:
        kolkata = CoordinatePoint(lat=22.5726, lng=88.3639)
        digha = CoordinatePoint(lat=21.6266, lng=87.5074)
        corridor = [kolkata, digha]

        # Midpoint near Mandarmani
        proposal = PlaceProposal(
            name="Mandarmani Beach Resort",
            category="beach",
            estimated_latitude=21.6667,
            estimated_longitude=87.7000,
            suggested_stay_minutes=120,
        )

        res = validate_candidate_corridor(proposal, corridor, max_distance_km=100.0)
        assert res.status == CorridorValidationStatus.WITHIN_CORRIDOR
        assert res.distance_to_corridor_km is not None
        assert res.distance_to_corridor_km <= 100.0
        # AC-3 invariant check: coordinate match alone does not prove existence
        assert "Coordinate match alone does not prove existence" in str(res.diagnostic)

    def test_candidate_outside_corridor_discarded(self) -> None:
        kolkata = CoordinatePoint(lat=22.5726, lng=88.3639)
        digha = CoordinatePoint(lat=21.6266, lng=87.5074)
        corridor = [kolkata, digha]

        # Darjeeling (27.0410, 88.2663) is >500km away from Kolkata-Digha corridor
        proposal = PlaceProposal(
            name="Tiger Hill Darjeeling",
            category="nature",
            estimated_latitude=27.0410,
            estimated_longitude=88.2663,
            suggested_stay_minutes=120,
        )

        res = validate_candidate_corridor(proposal, corridor, max_distance_km=100.0)
        assert res.status == CorridorValidationStatus.OUTSIDE_CORRIDOR
        assert res.distance_to_corridor_km is not None
        assert res.distance_to_corridor_km > 100.0
        assert "Discarded" in str(res.diagnostic)

    def test_candidate_malformed_coordinates_rejected(self) -> None:
        kolkata = CoordinatePoint(lat=22.5726, lng=88.3639)
        digha = CoordinatePoint(lat=21.6266, lng=87.5074)
        corridor = [kolkata, digha]

        proposal = PlaceProposal.model_construct(
            name="Malformed Point",
            category="sight",
            estimated_latitude=999.0,
            estimated_longitude=88.0,
            suggested_stay_minutes=60,
        )

        res = validate_candidate_corridor(proposal, corridor)
        assert res.status == CorridorValidationStatus.MALFORMED_COORDINATES
        assert res.distance_to_corridor_km is None
        assert "Malformed" in str(res.diagnostic)


class TestCandidateRegistrationAndDeduplication:
    """Tests candidate place registration, deduplication, and replay idempotency."""

    @pytest.mark.asyncio
    async def test_register_proposals_offline_partitioning(self) -> None:
        service = CandidateRegistrationService(session=None)
        corridor = [
            CoordinatePoint(lat=22.5726, lng=88.3639),
            CoordinatePoint(lat=21.6266, lng=87.5074),
        ]

        valid_prop = PlaceProposal(
            name="Tajpur Beach",
            category="beach",
            estimated_latitude=21.6500,
            estimated_longitude=87.6300,
        )
        far_prop = PlaceProposal(
            name="Sikkim Monastery",
            category="culture",
            estimated_latitude=27.3389,
            estimated_longitude=88.6065,
        )
        malformed_prop = PlaceProposal.model_construct(
            name="Bad Point",
            category="test",
            estimated_latitude=999.0,
            estimated_longitude=88.0,
        )

        valid_cands, excluded_cands = await service.register_candidate_proposals(
            proposals=[valid_prop, far_prop, malformed_prop],
            corridor_points=corridor,
        )

        assert len(valid_cands) == 1
        assert valid_cands[0].primary_name == "Tajpur Beach"
        assert valid_cands[0].is_candidate is True
        assert valid_cands[0].uncertainty == "EDITORIAL_DISCOVERY"
        # AC-3 invariant: newly registered candidate existence remains UNVERIFIED
        assert valid_cands[0].existence_status == CandidateExistenceStatus.UNVERIFIED

        assert len(excluded_cands) == 2
        reasons = [c.diagnostics.get("corridor_status") for c in excluded_cands]
        assert CorridorValidationStatus.OUTSIDE_CORRIDOR.value in reasons
        assert CorridorValidationStatus.MALFORMED_COORDINATES.value in reasons

    @pytest.mark.asyncio
    async def test_registration_idempotency_same_place_resolves_identically(self) -> None:
        service = CandidateRegistrationService(session=None)
        corridor = [CoordinatePoint(lat=22.5726, lng=88.3639)]

        prop = PlaceProposal(
            name="Victoria Memorial Hall",
            category="heritage",
            estimated_latitude=22.5448,
            estimated_longitude=88.3426,
        )

        valid1, _ = await service.register_candidate_proposals([prop], corridor)
        valid2, _ = await service.register_candidate_proposals([prop], corridor)

        assert len(valid1) == 1
        assert len(valid2) == 1
        # Replay resolves to identical place_id without creating duplicate candidates
        assert valid1[0].place_id == valid2[0].place_id


class TestDeterministicEvidenceEligibilityGate:
    """Tests deterministic evidence eligibility, hours evaluation, and state preservation."""

    @pytest.mark.asyncio
    async def test_scenario_a_verified_existence_and_verified_hours(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        owner_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Indian Museum",
            category="museum",
            point=CoordinatePoint(lat=22.5579, lng=88.3511),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        fixture_obs = [
            {
                "execution_id": str(exec_id),
                "owner_id": str(owner_id),
                "place_id": str(cand_id),
                "excerpt_text": "Indian Museum in Kolkata is open to visitors",
                "canonical_url": "https://indianmuseumkolkata.org",
                "support_type": EvidenceSupportType.SUPPORTS.value,
                "extraction_method": "SCRAPLING_HTTP",
                "artifact_ref": {"status": "SUCCESS"},
            }
        ]

        fixture_claims = [
            {
                "execution_id": str(exec_id),
                "owner_id": str(owner_id),
                "place_id": str(cand_id),
                "subject_id": "Indian Museum",
                "category": ClaimCategory.ATTRACTION.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "evidence_links": [
                    {
                        "support_type": EvidenceSupportType.SUPPORTS.value,
                        "observation": fixture_obs[0],
                    }
                ],
            },
            {
                "execution_id": str(exec_id),
                "owner_id": str(owner_id),
                "place_id": str(cand_id),
                "subject_id": "Indian Museum",
                "category": ClaimCategory.HOURS.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "typed_value": {"opens": "10:00", "closes": "17:00"},
            },
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            owner_id=owner_id,
            fixture_observations=fixture_obs,
            fixture_claims=fixture_claims,
        )

        assert len(report.eligible_stops) == 1
        assert len(report.editorial_recommendations) == 0
        assert len(report.excluded_candidates) == 0

        verified_cand = report.eligible_stops[0]
        assert verified_cand.existence_status == CandidateExistenceStatus.VERIFIED
        assert verified_cand.hours_status == CandidateHoursStatus.VERIFIED_HOURS
        assert verified_cand.window_start == time(10, 0)
        assert verified_cand.window_end == time(17, 0)
        assert verified_cand.is_hours_assumed is False

    @pytest.mark.asyncio
    async def test_scenario_b_verified_existence_with_unknown_hours_assumes_daytime(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        owner_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Princep Ghat",
            category="heritage",
            point=CoordinatePoint(lat=22.5540, lng=88.3320),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        # Supporting observation and verified attraction claim confirm existence, but operating hours are absent
        fixture_obs = [
            {
                "execution_id": str(exec_id),
                "owner_id": str(owner_id),
                "place_id": str(cand_id),
                "excerpt_text": "Princep Ghat is a historic riverside memorial in Kolkata",
                "canonical_url": "https://wbtourism.gov.in/princep_ghat",
                "support_type": EvidenceSupportType.SUPPORTS.value,
                "extraction_method": "SCRAPLING_HTTP",
                "artifact_ref": {"status": "SUCCESS"},
            }
        ]

        fixture_claims = [
            {
                "execution_id": str(exec_id),
                "owner_id": str(owner_id),
                "place_id": str(cand_id),
                "subject_id": "Princep Ghat",
                "category": ClaimCategory.ATTRACTION.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "evidence_links": [
                    {
                        "support_type": EvidenceSupportType.SUPPORTS.value,
                        "observation": fixture_obs[0],
                    }
                ],
            }
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            owner_id=owner_id,
            fixture_observations=fixture_obs,
            fixture_claims=fixture_claims,
        )

        assert len(report.eligible_stops) == 1
        assumed_cand = report.eligible_stops[0]

        # Invariant 5: Verified existence but unknown hours uses explicitly labeled HOURS_ASSUMED
        assert assumed_cand.existence_status == CandidateExistenceStatus.VERIFIED
        assert assumed_cand.hours_status == CandidateHoursStatus.HOURS_ASSUMED
        assert assumed_cand.is_hours_assumed is True
        assert assumed_cand.window_start == DEFAULT_ASSUMED_WINDOW_START  # 09:00
        assert assumed_cand.window_end == DEFAULT_ASSUMED_WINDOW_END  # 18:00
        assert "HOURS_ASSUMED" in str(assumed_cand.diagnostics.get("hours_diagnostic"))

    @pytest.mark.asyncio
    async def test_scenario_c_unverified_candidate_preserved_as_editorial_recommendation(
        self,
    ) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=uuid.uuid4(),
            primary_name="Unverified Secret Cafe",
            category="restaurant",
            point=CoordinatePoint(lat=22.5600, lng=88.3500),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        # No observations or claims exist
        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=[],
            fixture_claims=[],
        )

        # Invariant 4: Unverified places must NOT enter route solver stops
        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 1
        assert len(report.excluded_candidates) == 0

        editorial_cand = report.editorial_recommendations[0]
        assert editorial_cand.existence_status == CandidateExistenceStatus.UNVERIFIED
        assert editorial_cand.uncertainty == "EDITORIAL_DISCOVERY"
        assert editorial_cand.is_candidate is True
        assert "editorial recommendation" in str(editorial_cand.diagnostics.get("evidence_note"))

    @pytest.mark.asyncio
    async def test_scenario_d_contradicted_place_excluded_entirely(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=uuid.uuid4(),
            primary_name="Demolished Heritage House",
            category="heritage",
            point=CoordinatePoint(lat=22.5700, lng=88.3600),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        # Refuting observation asserts structure is demolished
        fixture_obs = [
            {
                "execution_id": str(exec_id),
                "excerpt_text": "Demolished Heritage House was permanently demolished in 2024",
                "canonical_url": "https://example.com/demolished",
                "support_type": EvidenceSupportType.REFUTES.value,
            }
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=fixture_obs,
            fixture_claims=[],
        )

        # Invariant 4: Contradicted places must not enter route solver and are excluded entirely
        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 0
        assert len(report.excluded_candidates) == 1

        excluded = report.excluded_candidates[0]
        assert excluded.existence_status == CandidateExistenceStatus.CONTRADICTED
        assert "Refuting evidence" in str(excluded.diagnostics.get("contradiction_reason"))

    @pytest.mark.asyncio
    async def test_scenario_e_stale_hours_claim_falls_back_to_hours_assumed(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Science City",
            category="science",
            point=CoordinatePoint(lat=22.5400, lng=88.3950),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        fixture_obs = [
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "excerpt_text": "Science City Kolkata is a prominent science museum",
                "canonical_url": "https://sciencecitykolkata.org",
                "support_type": EvidenceSupportType.SUPPORTS.value,
                "artifact_ref": {
                    "status": "SUCCESS",
                    "gps": {"lat": 22.5400, "lng": 88.3950},
                },
            }
        ]

        # Verified attraction claim confirms existence, while hours claim has expired validity or STALE freshness
        fixture_claims = [
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Science City",
                "category": ClaimCategory.ATTRACTION.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "evidence_links": [
                    {
                        "support_type": EvidenceSupportType.SUPPORTS.value,
                        "observation": fixture_obs[0],
                    }
                ],
            },
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Science City",
                "category": ClaimCategory.HOURS.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.STALE.value,
                "typed_value": {"opens": "09:00", "closes": "20:00"},
            },
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=fixture_obs,
            fixture_claims=fixture_claims,
        )

        assert len(report.eligible_stops) == 1
        cand = report.eligible_stops[0]
        # Invariant 6: Preserves stale state honestly and falls back to HOURS_ASSUMED
        assert cand.hours_status == CandidateHoursStatus.HOURS_ASSUMED
        assert cand.is_hours_assumed is True
        assert "STALE" in str(cand.diagnostics.get("hours_diagnostic"))

    @pytest.mark.asyncio
    async def test_scenario_f_tenant_and_execution_isolation(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_tenant_a = uuid.uuid4()
        exec_tenant_b = uuid.uuid4()

        candidate_a = RegisteredCandidate(
            place_id=uuid.uuid4(),
            primary_name="Private Club",
            category="club",
            point=CoordinatePoint(lat=22.5500, lng=88.3500),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        # Supporting observation belongs to Tenant B
        fixture_obs_tenant_b = [
            {
                "execution_id": str(exec_tenant_b),
                "excerpt_text": "Private Club verified location in Kolkata",
                "canonical_url": "https://example.com/private_club",
                "support_type": EvidenceSupportType.SUPPORTS.value,
            }
        ]

        # Evaluate eligibility for Tenant A
        report_a = await gate.evaluate_candidate_eligibility(
            candidates=[candidate_a],
            execution_id=exec_tenant_a,
            fixture_observations=fixture_obs_tenant_b,
            fixture_claims=[],
        )

        # Cross-tenant observation must NOT verify candidate A
        assert len(report_a.eligible_stops) == 0
        assert len(report_a.editorial_recommendations) == 1
        assert (
            report_a.editorial_recommendations[0].existence_status
            == CandidateExistenceStatus.UNVERIFIED
        )


class TestGraphSchedulingBoundaries:
    """Verifies that unverified places never enter the route solver schedule in TripPlannerGraph."""

    def test_solve_schedule_node_excludes_unverified_candidates(self) -> None:
        state: TripPlanningState = {
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
                        "confidence": 0.95,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-03",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "25000.00", "currency": "INR"},
            },
            # Raw discovered places containing unverified candidates
            "discovered_places": [
                {
                    "name": "Unverified Beach Hut",
                    "lat": 21.6400,
                    "lng": 87.5200,
                    "confidence": 0.5,
                    "stay_minutes": 60,
                }
            ],
            # Eligible stops containing only verified existence places
            "eligible_stops": [
                {
                    "primary_name": "Verified Marine Aquarium",
                    "point": {"lat": 21.6300, "lng": 87.5100},
                    "suggested_stay_minutes": 90,
                    "is_hours_assumed": True,
                    "hours_status": "HOURS_ASSUMED",
                }
            ],
            "editorial_recommendations": [
                {
                    "primary_name": "Unverified Beach Hut",
                    "uncertainty": "EDITORIAL_DISCOVERY",
                }
            ],
        }

        result = solve_schedule_node(state)
        assert result["current_step"] == "schedule_optimized"
        schedule = result["optimized_schedule"]
        scheduled_names = [s["name"] for s in schedule["stops"]]

        # Verified destination from brief and eligible stop are scheduled
        assert "Verified Marine Aquarium" in scheduled_names
        # AC-3 invariant: unverified candidate must NEVER enter the solver schedule
        assert "Unverified Beach Hut" not in scheduled_names

    def test_synthesize_proposal_preserves_editorial_recommendations(self) -> None:
        state: TripPlanningState = {
            "trip_id": str(uuid.uuid4()),
            "version": 1,
            "optimized_schedule": {"stops": [{"name": "Digha"}]},
            "editorial_recommendations": [
                {"primary_name": "Editorial Discovery Cafe", "uncertainty": "EDITORIAL_DISCOVERY"}
            ],
            "excluded_candidates": [{"primary_name": "Demolished House", "reason": "Contradicted"}],
        }

        res = synthesize_proposal_node(state)
        assert res["current_step"] == "proposal_synthesized"
        proposal = res["proposal"]
        assert len(proposal["editorial_recommendations"]) == 1
        assert (
            proposal["editorial_recommendations"][0]["primary_name"] == "Editorial Discovery Cafe"
        )
        assert len(proposal["excluded_candidates"]) == 1


class TestSlice3CorrectionRegressions:
    """Tests covering the five reviewed Slice 3 correction findings."""

    @pytest.mark.asyncio
    async def test_finding_2_name_mention_only_does_not_verify_existence(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Casual Name Mention Spot",
            category="sight",
            point=CoordinatePoint(lat=22.5000, lng=88.3000),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        # Observation merely mentions name in text without any verified existence claim
        fixture_obs = [
            {
                "execution_id": str(exec_id),
                "excerpt_text": "We walked past Casual Name Mention Spot on our way to the hotel.",
                "canonical_url": "https://travel-blog.com/post",
                "support_type": EvidenceSupportType.SUPPORTS.value,
            }
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=fixture_obs,
            fixture_claims=[],
        )

        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 1
        assert (
            report.editorial_recommendations[0].existence_status
            == CandidateExistenceStatus.UNVERIFIED
        )

    @pytest.mark.asyncio
    async def test_finding_2_url_substring_does_not_verify_existence(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Url Match Place",
            category="sight",
            point=CoordinatePoint(lat=22.5000, lng=88.3000),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        fixture_obs = [
            {
                "execution_id": str(exec_id),
                "excerpt_text": "Generic article with no verified facts.",
                "canonical_url": "https://example.com/urlmatchplace/details",
                "support_type": EvidenceSupportType.SUPPORTS.value,
            }
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=fixture_obs,
            fixture_claims=[],
        )

        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 1
        assert (
            report.editorial_recommendations[0].existence_status
            == CandidateExistenceStatus.UNVERIFIED
        )

    @pytest.mark.asyncio
    async def test_finding_2_missing_support_type_rejected(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Missing Support Spot",
            category="museum",
            point=CoordinatePoint(lat=22.5000, lng=88.3000),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        obs = {
            "execution_id": str(exec_id),
            "place_id": str(cand_id),
            "excerpt_text": "Missing Support Spot text.",
            "canonical_url": "https://example.com/missing",
            "support_type": None,  # Missing support_type
        }

        claims = [
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Missing Support Spot",
                "category": ClaimCategory.ATTRACTION.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "evidence_links": [{"support_type": None, "observation": obs}],
            }
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=[obs],
            fixture_claims=claims,
        )

        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 1

    @pytest.mark.asyncio
    async def test_finding_2_unavailable_page_rejected(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Unavailable Page Spot",
            category="museum",
            point=CoordinatePoint(lat=22.5000, lng=88.3000),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        obs = {
            "execution_id": str(exec_id),
            "place_id": str(cand_id),
            "excerpt_text": "Unavailable Page Spot text.",
            "canonical_url": "https://example.com/unavail",
            "support_type": EvidenceSupportType.SUPPORTS.value,
            "artifact_ref": {"status": "UNAVAILABLE", "error": "404 Not Found"},
        }

        claims = [
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Unavailable Page Spot",
                "category": ClaimCategory.ATTRACTION.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "evidence_links": [{"support_type": "SUPPORTS", "observation": obs}],
            }
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=[obs],
            fixture_claims=claims,
        )

        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 1

    @pytest.mark.asyncio
    async def test_finding_2_needs_browser_placeholder_rejected(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Pending Render Spot",
            category="museum",
            point=CoordinatePoint(lat=22.5000, lng=88.3000),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        obs = {
            "execution_id": str(exec_id),
            "place_id": str(cand_id),
            "excerpt_text": "[DYNAMIC_PAGE: JAVASCRIPT_REQUIRED]",
            "canonical_url": "https://example.com/dynamic",
            "support_type": EvidenceSupportType.SUPPORTS.value,
            "extraction_method": "NEEDS_BROWSER",
        }

        claims = [
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Pending Render Spot",
                "category": ClaimCategory.ATTRACTION.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "evidence_links": [{"support_type": "SUPPORTS", "observation": obs}],
            }
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=[obs],
            fixture_claims=claims,
        )

        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 1

    @pytest.mark.asyncio
    async def test_finding_2_unrelated_verified_claim_rejected(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Place With Weather Claim",
            category="museum",
            point=CoordinatePoint(lat=22.5000, lng=88.3000),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        obs = {
            "execution_id": str(exec_id),
            "place_id": str(cand_id),
            "excerpt_text": "Kolkata weather report for region.",
            "canonical_url": "https://example.com/weather",
            "support_type": EvidenceSupportType.SUPPORTS.value,
            "extraction_method": "OPEN_METEO",
        }

        # Unrelated claim category (WEATHER) must NOT verify existence of the place!
        claims = [
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Place With Weather Claim",
                "category": ClaimCategory.WEATHER.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "evidence_links": [{"support_type": "SUPPORTS", "observation": obs}],
            }
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=[obs],
            fixture_claims=claims,
        )

        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 1

    @pytest.mark.asyncio
    async def test_finding_3_owner_authorization_mismatch_raises_permission_error(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        real_owner_id = uuid.uuid4()
        imposter_owner_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Fenced Spot",
            category="museum",
            point=CoordinatePoint(lat=22.5000, lng=88.3000),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        fixture_obs = [
            {
                "execution_id": str(exec_id),
                "owner_id": str(real_owner_id),
                "place_id": str(cand_id),
                "excerpt_text": "Fenced Spot details.",
                "canonical_url": "https://example.com/fenced",
                "support_type": EvidenceSupportType.SUPPORTS.value,
            }
        ]

        # Imposter supplies real execution ID belonging to real_owner_id
        with pytest.raises(PermissionError) as exc_info:
            await gate.evaluate_candidate_eligibility(
                candidates=[candidate],
                execution_id=exec_id,
                owner_id=imposter_owner_id,
                fixture_observations=fixture_obs,
                fixture_claims=[],
            )

        assert "authorization failed" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_finding_4_same_name_different_location_remain_distinct_in_memory(self) -> None:
        service = CandidateRegistrationService(session=None)
        corridor = [
            CoordinatePoint(lat=22.5726, lng=88.3639),  # Kolkata
            CoordinatePoint(lat=21.6266, lng=87.5074),  # Digha
        ]

        proposal_kolkata = PlaceProposal(
            name="Kali Temple",
            category="temple",
            estimated_latitude=22.5700,
            estimated_longitude=88.3600,
        )
        proposal_digha = PlaceProposal(
            name="Kali Temple",  # Same name!
            category="temple",
            estimated_latitude=21.6300,
            estimated_longitude=87.5100,
        )

        valid_cands, _ = await service.register_candidate_proposals(
            [proposal_kolkata, proposal_digha], corridor
        )

        assert len(valid_cands) == 2
        # Same name in different locations must have distinct place IDs
        assert valid_cands[0].place_id != valid_cands[1].place_id
        assert valid_cands[0].primary_name == valid_cands[1].primary_name

    def test_finding_1_missing_gate_output_safe_path(self) -> None:
        state: TripPlanningState = {
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
                        "confidence": 0.95,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-03",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "25000.00", "currency": "INR"},
            },
            # Gate output is missing entirely (eligible_stops not in state)
            "discovered_places": [
                {
                    "name": "Unverified Proposal Beach Shack",
                    "lat": 21.6400,
                    "lng": 87.5200,
                    "confidence": 0.5,
                    "stay_minutes": 60,
                }
            ],
        }

        result = solve_schedule_node(state)
        schedule = result["optimized_schedule"]
        scheduled_names = [s["name"] for s in schedule["stops"]]

        # Reconciled 0007 policy: unverified brief destination must NOT enter solver schedule
        assert "Digha" not in scheduled_names
        # Safe path: unverified proposal must NOT be scheduled
        assert "Unverified Proposal Beach Shack" not in scheduled_names
        # Both unverified brief destination and proposal are preserved in editorial recommendations
        recs = result.get("editorial_recommendations", [])
        assert any(r.get("primary_name") == "Unverified Proposal Beach Shack" for r in recs)
        assert any(r.get("primary_name") == "Digha" for r in recs)

    def test_finding_5_scheduled_stops_preserve_windows_and_labels(self) -> None:
        state: TripPlanningState = {
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
                        "latitude": 21.6266,
                        "longitude": 87.5074,
                        "stay_days": 1,
                        "confidence": 1.0,
                    }
                ],
                "start_date": "2026-11-01",
                "end_date": "2026-11-02",
                "travelers": 1,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {
                    "primary_name": "Verified Museum",
                    "point": {"lat": 21.6300, "lng": 87.5100},
                    "suggested_stay_minutes": 60,
                    "is_hours_assumed": False,
                    "hours_status": "VERIFIED_HOURS",
                    "window_start": "10:00",
                    "window_end": "17:00",
                },
                {
                    "primary_name": "Assumed Garden",
                    "point": {"lat": 21.6350, "lng": 87.5150},
                    "suggested_stay_minutes": 60,
                    "is_hours_assumed": True,
                    "hours_status": "HOURS_ASSUMED",
                    "window_start": time(9, 0),
                    "window_end": time(18, 0),
                },
            ],
        }

        result = solve_schedule_node(state)
        schedule = result["optimized_schedule"]
        stops_by_name = {s["name"]: s for s in schedule["stops"]}

        assert "Verified Museum" in stops_by_name
        vm_stop = stops_by_name["Verified Museum"]
        assert vm_stop["is_hours_assumed"] is False
        assert vm_stop["hours_status"] == "VERIFIED_HOURS"
        assert vm_stop["window_start"] is not None
        assert vm_stop["window_end"] is not None

        assert "Assumed Garden" in stops_by_name
        ag_stop = stops_by_name["Assumed Garden"]
        assert ag_stop["is_hours_assumed"] is True
        assert ag_stop["hours_status"] == "HOURS_ASSUMED"
        assert ag_stop["window_start"] is not None
        assert ag_stop["window_end"] is not None

    @pytest.mark.asyncio
    async def test_finding_1_compiled_graph_lifecycle_with_evaluate_candidates(self) -> None:
        from functions.decision_router.fixtures import FixtureLayaDecisionAdapter
        from functions.orchestrator.graphs.trip_planner.graph import (
            compile_trip_planning_workflow,
        )

        workflow = compile_trip_planning_workflow()

        exec_id = uuid.uuid4()
        owner_id = uuid.uuid4()

        initial_state: TripPlanningState = {
            "trip_id": str(uuid.uuid4()),
            "owner_id": str(owner_id),
            "execution_id": str(exec_id),
            "version": 1,
            "_offline_fixture_mode": True,
            "_laya_adapter": FixtureLayaDecisionAdapter(),
            "static_search_completed": True,
            "remaining_search_budget": 0,
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
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
                    "name": "Digha Marine Station",
                    "category": "museum",
                    "lat": 21.6300,
                    "lng": 87.5100,
                    "stay_minutes": 60,
                }
            ],
            "task_outputs": {
                "search-1": {
                    "observations": [
                        {
                            "execution_id": str(exec_id),
                            "owner_id": str(owner_id),
                            "canonical_url": "https://digha.org/guide",
                            "content_hash": "hash_dest_digha",
                            "excerpt_text": "Digha is a coastal resort town in West Bengal with beaches and attractions.",
                            "support_type": "SUPPORTS",
                            "extraction_method": "SCRAPLING_HTTP",
                            "artifact_ref": {
                                "status": "SUCCESS",
                                "gps": {"lat": 21.6266, "lng": 87.5074},
                            },
                        },
                        {
                            "execution_id": str(exec_id),
                            "owner_id": str(owner_id),
                            "canonical_url": "https://digha.org/marine",
                            "content_hash": "hash_marine_cand",
                            "excerpt_text": "Digha Marine Station is a research facility and aquarium in Digha.",
                            "support_type": "SUPPORTS",
                            "extraction_method": "SCRAPLING_HTTP",
                            "artifact_ref": {
                                "status": "SUCCESS",
                                "gps": {"lat": 21.6300, "lng": 87.5100},
                            },
                        },
                    ],
                    "claims": [
                        {
                            "execution_id": str(exec_id),
                            "owner_id": str(owner_id),
                            "subject_id": "Digha",
                            "category": "ATTRACTION",
                            "verification": "VERIFIED",
                            "freshness": "FRESH",
                            "evidence_links": [
                                {
                                    "support_type": "SUPPORTS",
                                    "observation": {
                                        "extraction_method": "SCRAPLING_HTTP",
                                        "artifact_ref": {
                                            "status": "SUCCESS",
                                            "gps": {"lat": 21.6266, "lng": 87.5074},
                                        },
                                    },
                                }
                            ],
                        },
                        {
                            "execution_id": str(exec_id),
                            "owner_id": str(owner_id),
                            "subject_id": "Digha Marine Station",
                            "category": "ATTRACTION",
                            "verification": "VERIFIED",
                            "freshness": "FRESH",
                            "evidence_links": [
                                {
                                    "support_type": "SUPPORTS",
                                    "observation": {
                                        "extraction_method": "SCRAPLING_HTTP",
                                        "artifact_ref": {
                                            "status": "SUCCESS",
                                            "gps": {"lat": 21.6300, "lng": 87.5100},
                                        },
                                    },
                                }
                            ],
                        },
                    ],
                    "search_completed": True,
                }
            },
        }

        result = await workflow.ainvoke(initial_state)
        assert result["status"] == "completed"
        assert result["current_step"] in ("proposal_synthesized", "proposal_published")
        assert "validated_candidates" in result
        assert "candidate_eligibility" in result
        assert "optimized_schedule" in result
        schedule = result["optimized_schedule"]
        scheduled_names = [s["name"] for s in schedule["stops"]]
        assert "Digha" in scheduled_names
        assert "Digha Marine Station" in scheduled_names

    @pytest.mark.asyncio
    async def test_evaluate_candidates_node_rejects_silent_in_memory_fallback_in_production(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verifies that evaluate_candidates_node raises RuntimeError rather than falling back silently in memory."""
        from functions.orchestrator.graphs.trip_planner.graph import evaluate_candidates_node

        def _mock_no_factory() -> None:
            raise RuntimeError("Database unconfigured in test environment")

        monkeypatch.setattr(
            "functions.orchestrator.graphs.trip_planner.graph.get_session_factory",
            _mock_no_factory,
        )

        state: TripPlanningState = {
            "execution_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "_offline_fixture_mode": False,
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Digha",
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
                    "name": "Production Landmark",
                    "category": "museum",
                    "lat": 21.6300,
                    "lng": 87.5100,
                    "stay_minutes": 60,
                }
            ],
        }

        with pytest.raises(RuntimeError) as exc_info:
            await evaluate_candidates_node(state)

        assert "silent in-memory registration fallback is prohibited" in str(exc_info.value).lower()

    def test_solve_schedule_node_labels_unverified_brief_destination_honestly(self) -> None:
        """Verifies that brief destinations without source-backed evidence are honestly labeled USER_SUPPLIED_UNVERIFIED."""
        from functions.orchestrator.graphs.trip_planner.graph import solve_schedule_node

        state: TripPlanningState = {
            "brief": {
                "origin_name": "Kolkata",
                "origin_lat": 22.5726,
                "origin_lng": 88.3639,
                "destinations": [
                    {
                        "name": "Unverified Digha Destination",
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
            "eligible_stops": [],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        result = solve_schedule_node(state)
        schedule = result["optimized_schedule"]
        stops_by_name = {s["name"]: s for s in schedule["stops"]}

        # Under spec 0007 AC-3, unverified candidate places never enter the route solver
        assert "Unverified Digha Destination" not in stops_by_name

        # Retained in editorial recommendations with honest user supplied labeling
        recs = result["editorial_recommendations"]
        recs_by_name = {r["primary_name"]: r for r in recs}
        assert "Unverified Digha Destination" in recs_by_name
        dest_rec = recs_by_name["Unverified Digha Destination"]
        assert dest_rec["hours_status"] == CandidateHoursStatus.USER_SUPPLIED_UNVERIFIED.value
        assert dest_rec["is_hours_assumed"] is True
        assert dest_rec["existence_status"] == CandidateExistenceStatus.UNVERIFIED.value

    @pytest.mark.asyncio
    async def test_finding_evidence_identity_two_same_named_candidates_and_name_only_claim_without_location(
        self,
    ) -> None:
        """Verifies that a name-only claim with no location cannot verify either of two same-named candidates."""
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_kolkata = RegisteredCandidate(
            place_id=uuid.uuid4(),
            primary_name="Shiva Temple",
            category="temple",
            point=CoordinatePoint(lat=22.5700, lng=88.3600),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )
        cand_digha = RegisteredCandidate(
            place_id=uuid.uuid4(),
            primary_name="Shiva Temple",
            category="temple",
            point=CoordinatePoint(lat=21.6300, lng=87.5100),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        obs_no_location = {
            "execution_id": str(exec_id),
            "canonical_url": "https://temple.org/shiva",
            "excerpt_text": "Ancient Shiva Temple attracts devotees across the state.",
            "support_type": EvidenceSupportType.SUPPORTS.value,
            "extraction_method": "SCRAPLING_HTTP",
            "artifact_ref": {"status": "SUCCESS"},  # No gps, location, or coordinates
        }
        name_only_claim = {
            "execution_id": str(exec_id),
            "place_id": None,  # Name-only claim
            "subject_id": "Shiva Temple",
            "category": ClaimCategory.ATTRACTION.value,
            "verification": VerificationStatus.VERIFIED.value,
            "freshness": FreshnessStatus.FRESH.value,
            "evidence_links": [
                {
                    "support_type": EvidenceSupportType.SUPPORTS.value,
                    "observation": obs_no_location,
                }
            ],
        }

        report = await gate.evaluate_candidate_eligibility(
            candidates=[cand_kolkata, cand_digha],
            execution_id=exec_id,
            fixture_observations=[obs_no_location],
            fixture_claims=[name_only_claim],
        )

        # Neither candidate may be verified by a name-only claim lacking location
        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 2
        for cand in report.editorial_recommendations:
            assert cand.existence_status == CandidateExistenceStatus.UNVERIFIED
            assert cand.is_hours_assumed is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_gps",
        [
            None,
            {},
            {"lat": "invalid", "lng": "invalid"},
            {"lat": True, "lng": 88.3600},
            {"lat": float("nan"), "lng": 88.3600},
            {"lat": float("inf"), "lng": 88.3600},
            {"lat": 95.0, "lng": 88.3600},  # Out of bounds lat
            {"lat": 22.5700, "lng": 190.0},  # Out of bounds lng
            {"lat": -95.0, "lng": 88.3600},
            {"lat": 22.5700, "lng": -195.0},
        ],
    )
    async def test_finding_evidence_identity_missing_malformed_non_finite_coordinates_rejected(
        self, bad_gps: Any
    ) -> None:
        """Verifies that missing, malformed, non-finite, or out-of-bounds coordinates cannot establish match."""
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand = RegisteredCandidate(
            place_id=uuid.uuid4(),
            primary_name="Belur Math",
            category="temple",
            point=CoordinatePoint(lat=22.6322, lng=88.3557),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        obs = {
            "execution_id": str(exec_id),
            "canonical_url": "https://belurmath.org",
            "excerpt_text": "Belur Math is the headquarters of Ramakrishna Math and Ramakrishna Mission.",
            "support_type": EvidenceSupportType.SUPPORTS.value,
            "extraction_method": "SCRAPLING_HTTP",
            "artifact_ref": (
                {"status": "SUCCESS", "gps": bad_gps}
                if bad_gps is not None
                else {"status": "SUCCESS"}
            ),
        }
        claim = {
            "execution_id": str(exec_id),
            "place_id": None,
            "subject_id": "Belur Math",
            "category": ClaimCategory.ATTRACTION.value,
            "verification": VerificationStatus.VERIFIED.value,
            "freshness": FreshnessStatus.FRESH.value,
            "evidence_links": [{"support_type": "SUPPORTS", "observation": obs}],
        }

        report = await gate.evaluate_candidate_eligibility(
            candidates=[cand],
            execution_id=exec_id,
            fixture_observations=[obs],
            fixture_claims=[claim],
        )

        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 1
        assert (
            report.editorial_recommendations[0].existence_status
            == CandidateExistenceStatus.UNVERIFIED
        )

    @pytest.mark.asyncio
    async def test_finding_evidence_identity_valid_name_only_claim_with_established_nearby_location_matches(
        self,
    ) -> None:
        """Verifies that a valid name-only claim with an established nearby location (<= 1.0 km) verifies candidate."""
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand = RegisteredCandidate(
            place_id=uuid.uuid4(),
            primary_name="Dakshineswar Kali Temple",
            category="temple",
            point=CoordinatePoint(lat=22.6530, lng=88.3575),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        # Evidence observation has coordinates ~30 meters away from candidate
        obs = {
            "execution_id": str(exec_id),
            "canonical_url": "https://dakshineswarkalitemple.org",
            "excerpt_text": "Dakshineswar Kali Temple is a Hindu navaratna temple by the Hooghly River.",
            "support_type": EvidenceSupportType.SUPPORTS.value,
            "extraction_method": "SCRAPLING_HTTP",
            "artifact_ref": {
                "status": "SUCCESS",
                "gps": {"lat": 22.6532, "lng": 88.3577},
            },
        }
        claim = {
            "execution_id": str(exec_id),
            "place_id": None,  # Name-only claim
            "subject_id": "Dakshineswar Kali Temple",
            "category": ClaimCategory.ATTRACTION.value,
            "verification": VerificationStatus.VERIFIED.value,
            "freshness": FreshnessStatus.FRESH.value,
            "evidence_links": [{"support_type": "SUPPORTS", "observation": obs}],
        }

        report = await gate.evaluate_candidate_eligibility(
            candidates=[cand],
            execution_id=exec_id,
            fixture_observations=[obs],
            fixture_claims=[claim],
        )

        assert len(report.eligible_stops) == 1
        assert len(report.editorial_recommendations) == 0
        stop = report.eligible_stops[0]
        assert stop.primary_name == "Dakshineswar Kali Temple"
        assert stop.existence_status == CandidateExistenceStatus.VERIFIED
        assert stop.hours_status == CandidateHoursStatus.HOURS_ASSUMED
        assert stop.is_hours_assumed is True
        assert stop.window_start == time(9, 0)
        assert stop.window_end == time(18, 0)

    @pytest.mark.asyncio
    async def test_finding_evidence_identity_claim_linked_to_another_place_id_rejected(
        self,
    ) -> None:
        """Verifies that a claim explicitly linked to place ID A cannot verify candidate place ID B."""
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_a_id = uuid.uuid4()
        cand_b_id = uuid.uuid4()

        cand_a = RegisteredCandidate(
            place_id=cand_a_id,
            primary_name="Kali Temple",
            category="temple",
            point=CoordinatePoint(lat=22.6530, lng=88.3575),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        # Claim carries place_id=cand_b_id (linked to place B)
        obs = {
            "execution_id": str(exec_id),
            "place_id": str(cand_b_id),
            "canonical_url": "https://kalitemple-b.org",
            "excerpt_text": "Kali Temple B details.",
            "support_type": EvidenceSupportType.SUPPORTS.value,
            "extraction_method": "SCRAPLING_HTTP",
            "artifact_ref": {
                "status": "SUCCESS",
                "gps": {"lat": 22.6530, "lng": 88.3575},  # Even if coordinates match!
            },
        }
        claim_b = {
            "execution_id": str(exec_id),
            "place_id": str(cand_b_id),  # Belongs to place B
            "subject_id": "Kali Temple",
            "category": ClaimCategory.ATTRACTION.value,
            "verification": VerificationStatus.VERIFIED.value,
            "freshness": FreshnessStatus.FRESH.value,
            "evidence_links": [{"support_type": "SUPPORTS", "observation": obs}],
        }

        report = await gate.evaluate_candidate_eligibility(
            candidates=[cand_a],
            execution_id=exec_id,
            fixture_observations=[obs],
            fixture_claims=[claim_b],
        )

        # Candidate A must NOT borrow Place B's claim
        assert len(report.eligible_stops) == 0
        assert len(report.editorial_recommendations) == 1
        assert (
            report.editorial_recommendations[0].existence_status
            == CandidateExistenceStatus.UNVERIFIED
        )
