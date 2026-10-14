"""Unit tests for place resolution service and token similarity (AC-7).

Tests name token similarity, containment metrics, and resolution schemas.
"""

from __future__ import annotations

from functions.orchestrator.places.schemas import AccessStatus, CoordinatePoint
from functions.orchestrator.places.service import (
    CandidatePlaceInput,
    PlaceResolutionResult,
    ResolutionStatus,
    compute_name_similarity,
)


class TestNameSimilarity:
    """Tests for compute_name_similarity conforming to spec 0002 invariant 7."""

    def test_exact_match_returns_one(self) -> None:
        assert compute_name_similarity("Dakshineswar Kali Temple", "Dakshineswar Kali Temple") == 1.0
        assert compute_name_similarity("dakshineswar kali temple", "DAKSHINESWAR KALI TEMPLE") == 1.0

    def test_empty_or_whitespace_strings_return_zero(self) -> None:
        assert compute_name_similarity("", "Dakshineswar") == 0.0
        assert compute_name_similarity("   ", "   ") == 0.0
        assert compute_name_similarity("Victoria", "") == 0.0

    def test_token_subset_and_containment_exceeds_threshold(self) -> None:
        # "Dakshineswar Temple" is a multi-token subset of "Dakshineswar Kali Temple"
        score = compute_name_similarity("Dakshineswar Temple", "Dakshineswar Kali Temple")
        assert score >= 0.8, f"Expected similarity >= 0.8, got {score}"

    def test_single_token_containment_does_not_falsely_boost_score(self) -> None:
        # Single generic token "Park" in "National Water Park and Resort" must not trigger containment boost
        score = compute_name_similarity("Park", "National Water Park and Resort")
        assert score < 0.6, f"Expected single token subset < 0.6, got {score}"

        # Single generic token "Taj" in "Taj Hotel and Conference Center" must not trigger containment boost
        score2 = compute_name_similarity("Taj", "Taj Hotel and Conference Center")
        assert score2 < 0.6, f"Expected single token subset < 0.6, got {score2}"

    def test_spelling_variation_and_character_similarity(self) -> None:
        # "City Center" vs "City Centre"
        score = compute_name_similarity("City Center", "City Centre")
        assert score >= 0.8, f"Expected similarity >= 0.8, got {score}"

    def test_punctuation_stripping(self) -> None:
        # "St. Paul's Cathedral" vs "St Pauls Cathedral"
        score = compute_name_similarity("St. Paul's Cathedral", "St Pauls Cathedral")
        assert score >= 0.9, f"Expected similarity >= 0.9, got {score}"

    def test_distinct_places_fall_below_threshold(self) -> None:
        # "Dakshineswar Temple" vs "Kalighat Temple" share only generic token "Temple"
        score = compute_name_similarity("Dakshineswar Temple", "Kalighat Temple")
        assert score < 0.6, f"Expected distinct temples < 0.6, got {score}"

        # "Victoria Memorial" vs "Taj Mahal"
        score2 = compute_name_similarity("Victoria Memorial", "Taj Mahal")
        assert score2 < 0.2, f"Expected completely distinct places < 0.2, got {score2}"


class TestResolutionSchemas:
    """Tests for resolution data structures."""

    def test_candidate_place_input_defaults(self) -> None:
        point = CoordinatePoint(lng=88.3576, lat=22.6534)
        inp = CandidatePlaceInput(
            primary_name="Dakshineswar Temple",
            locality="Kolkata",
            point=point,
        )
        assert inp.primary_name == "Dakshineswar Temple"
        assert inp.locality == "Kolkata"
        assert inp.point == point
        assert inp.names == []
        assert inp.provider_ids == {}
        assert inp.access_status == AccessStatus.UNKNOWN

    def test_place_resolution_result_attributes(self) -> None:
        res = PlaceResolutionResult(
            status=ResolutionStatus.RESOLVED,
            place=None,
            confidence=0.95,
            distance_meters=42.5,
            reason="Matched within 100m",
        )
        assert res.status == ResolutionStatus.RESOLVED
        assert res.confidence == 0.95
        assert res.distance_meters == 42.5
        assert "within 100m" in res.reason
