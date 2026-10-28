"""Unit tests for language model domain contracts, fixture adapter, and factory."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from config.config import Settings
from contracts.llm import (
    DayOpeningHours,
    LlmProviderConfig,
    PlaceProposal,
    RankingRequest,
    RankingResponse,
    ResearchRequest,
    ResearchResponse,
    StructuredOpeningHours,
)
from functions.orchestrator.llm.adapters.fixture_client import FixtureLlmClient
from functions.orchestrator.llm.factory import create_llm_client
from functions.orchestrator.llm.port import HostedLlmClientPort


class TestLlmDomainContracts:
    """Verifies typed schema validation and constraints for language model contracts."""

    def test_place_proposal_valid_schema(self) -> None:
        place = PlaceProposal(
            name="Kolaghat Express Dhaba",
            category="restaurant",
            estimated_latitude=22.4285,
            estimated_longitude=87.8712,
            description="Highway dining plaza",
            fit_score=0.95,
            suggested_stay_minutes=45,
            price_estimate_inr=350.0,
        )
        assert place.name == "Kolaghat Express Dhaba"
        assert place.category == "restaurant"
        assert place.estimated_latitude == 22.4285
        assert place.estimated_longitude == 87.8712
        assert place.fit_score == 0.95
        assert place.suggested_stay_minutes == 45
        assert place.price_estimate_inr == 350.0
        assert place.uncertainty == "EDITORIAL_DISCOVERY"
        assert place.is_candidate is True

    def test_day_opening_hours_model(self) -> None:
        hours = DayOpeningHours(
            day="monday",
            opens="09:00",
            closes="18:00",
            is_closed=False,
        )
        assert hours.day == "monday"
        assert hours.opens == "09:00"
        assert hours.closes == "18:00"
        assert hours.is_closed is False

    def test_place_proposal_with_structured_hours(self) -> None:
        day_hours = DayOpeningHours(
            day="tuesday",
            opens="10:00",
            closes="17:00",
        )
        structured = StructuredOpeningHours(
            days=[day_hours],
            raw_text="Tue 10:00 to 17:00",
        )
        place = PlaceProposal(
            name="Victoria Memorial",
            category="heritage",
            estimated_latitude=22.5448,
            estimated_longitude=88.3426,
            structured_hours=structured,
        )
        assert place.structured_hours is not None
        assert len(place.structured_hours.days) == 1
        assert place.structured_hours.days[0].day == "tuesday"
        assert place.structured_hours.raw_text == "Tue 10:00 to 17:00"

    def test_place_proposal_coordinate_validation(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PlaceProposal(
                name="Invalid Place",
                category="sight",
                estimated_latitude=95.0,  # Invalid: > 90.0
                estimated_longitude=88.0,
            )
        assert "estimated_latitude" in str(exc_info.value)

        with pytest.raises(ValidationError) as exc_info:
            PlaceProposal(
                name="Invalid Place",
                category="sight",
                estimated_latitude=22.0,
                estimated_longitude=-190.0,  # Invalid: < -180.0
            )
        assert "estimated_longitude" in str(exc_info.value)

    def test_place_proposal_fit_score_bounds(self) -> None:
        with pytest.raises(ValidationError):
            PlaceProposal(
                name="Invalid Score",
                category="sight",
                estimated_latitude=22.0,
                estimated_longitude=88.0,
                fit_score=1.5,  # Invalid: > 1.0
            )

        with pytest.raises(ValidationError):
            PlaceProposal(
                name="Invalid Score",
                category="sight",
                estimated_latitude=22.0,
                estimated_longitude=88.0,
                fit_score=-0.1,  # Invalid: < 0.0
            )

    def test_research_request_requires_destinations(self) -> None:
        with pytest.raises(ValidationError):
            ResearchRequest(
                brief_summary="Kolkata trip",
                origin_name="Kolkata",
                destination_names=[],  # Invalid: min_length=1
            )

    def test_research_request_valid(self) -> None:
        req = ResearchRequest(
            brief_summary="Weekend road trip to Digha",
            origin_name="Kolkata",
            destination_names=["Digha"],
            traveler_preferences=["food", "heritage"],
            budget_tier="MODERATE",
        )
        assert req.origin_name == "Kolkata"
        assert req.destination_names == ["Digha"]
        assert req.corridor_distance_km_limit == 100.0

    def test_llm_provider_config_validation(self) -> None:
        cfg = LlmProviderConfig(
            provider="fixture",
            timeout_seconds=20.0,
            max_retries=3,
        )
        assert cfg.provider == "fixture"
        assert cfg.timeout_seconds == 20.0
        assert cfg.max_retries == 3

        with pytest.raises(ValidationError):
            LlmProviderConfig(timeout_seconds=0.5)  # Invalid: < 1.0


class TestFixtureLlmClient:
    """Verifies deterministic fixture client behavior and response contracts."""

    @pytest.fixture
    def client(self) -> FixtureLlmClient:
        return FixtureLlmClient()

    async def test_conforms_to_port(self, client: FixtureLlmClient) -> None:
        assert isinstance(client, HostedLlmClientPort)

    async def test_research_places_default_kolkata_digha(self, client: FixtureLlmClient) -> None:
        req = ResearchRequest(
            brief_summary="Weekend drive from Kolkata to Digha",
            origin_name="Kolkata",
            destination_names=["Digha"],
        )
        resp = await client.research_places(req)

        assert isinstance(resp, ResearchResponse)
        assert resp.provider == "fixture"
        assert resp.recommended_pace == "MODERATE"
        assert len(resp.candidate_places) >= 4
        assert len(resp.regional_travel_notes) > 0

        # Verify candidate place details
        first_place = resp.candidate_places[0]
        assert isinstance(first_place, PlaceProposal)
        assert first_place.name == "Kolaghat Express Highway Dhaba"
        assert first_place.category == "restaurant"
        assert first_place.is_candidate is True
        assert first_place.uncertainty == "EDITORIAL_DISCOVERY"

    async def test_research_places_food_preferences(self, client: FixtureLlmClient) -> None:
        req = ResearchRequest(
            brief_summary="Heritage culinary tour",
            origin_name="Kolkata",
            destination_names=["Kolkata"],
            traveler_preferences=["food", "dining"],
        )
        resp = await client.research_places(req)

        assert isinstance(resp, ResearchResponse)
        assert len(resp.candidate_places) == 3
        # Assert food-only categories
        allowed_food_categories = {"restaurant", "street_food", "cafe"}
        for place in resp.candidate_places:
            assert place.category in allowed_food_categories

    async def test_research_places_empty_result(self, client: FixtureLlmClient) -> None:
        req = ResearchRequest(
            brief_summary="Empty wilderness corridor with no POIs",
            origin_name="Remote Pass",
            destination_names=["Empty Valley"],
        )
        resp = await client.research_places(req)

        assert isinstance(resp, ResearchResponse)
        assert len(resp.candidate_places) == 0

    async def test_research_places_distant_candidates(self, client: FixtureLlmClient) -> None:
        req = ResearchRequest(
            brief_summary="Distant hills tour",
            origin_name="Kolkata",
            destination_names=["Darjeeling"],
        )
        resp = await client.research_places(req)

        assert isinstance(resp, ResearchResponse)
        assert len(resp.candidate_places) == 2
        # Latitudes should be in Darjeeling range (> 26.5)
        for place in resp.candidate_places:
            assert place.estimated_latitude > 26.0

    async def test_research_places_timeout_simulation(self, client: FixtureLlmClient) -> None:
        req = ResearchRequest(
            brief_summary="Timeout test request",
            origin_name="Kolkata",
            destination_names=["Digha"],
        )
        with pytest.raises(TimeoutError) as exc_info:
            await client.research_places(req)
        assert "timeout" in str(exc_info.value).lower()

    async def test_research_places_error_simulation(self, client: FixtureLlmClient) -> None:
        req = ResearchRequest(
            brief_summary="Error test request",
            origin_name="Kolkata",
            destination_names=["Digha"],
        )
        with pytest.raises(RuntimeError) as exc_info:
            await client.research_places(req)
        assert "error" in str(exc_info.value).lower()

    async def test_rank_places_sorts_by_fit_score(self, client: FixtureLlmClient) -> None:
        candidates = [
            PlaceProposal(
                name="Place Low",
                category="sight",
                estimated_latitude=22.0,
                estimated_longitude=88.0,
                fit_score=0.60,
            ),
            PlaceProposal(
                name="Place High",
                category="sight",
                estimated_latitude=22.1,
                estimated_longitude=88.1,
                fit_score=0.98,
            ),
            PlaceProposal(
                name="Place Mid",
                category="sight",
                estimated_latitude=22.2,
                estimated_longitude=88.2,
                fit_score=0.82,
            ),
        ]
        req = RankingRequest(
            candidate_places=candidates,
            max_places_to_select=2,
        )
        resp = await client.rank_places(req)

        assert isinstance(resp, RankingResponse)
        assert len(resp.ranked_places) == 2
        assert resp.ranked_places[0].place.name == "Place High"
        assert resp.ranked_places[0].rank == 1
        assert resp.ranked_places[1].place.name == "Place Mid"
        assert resp.ranked_places[1].rank == 2

        # Verify eliminated candidates
        assert len(resp.eliminated_candidates) == 1
        assert resp.eliminated_candidates[0]["name"] == "Place Low"

    async def test_rank_places_empty_candidates(self, client: FixtureLlmClient) -> None:
        req = RankingRequest(candidate_places=[])
        resp = await client.rank_places(req)
        assert len(resp.ranked_places) == 0
        assert len(resp.eliminated_candidates) == 0

    def test_fixture_directory_resolution_with_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("LLM_FIXTURE_DIR", str(tmp_path))
        client = FixtureLlmClient()
        assert client._fixture_dir == tmp_path

    def test_missing_fixture_raises_descriptive_error(self, tmp_path: Path) -> None:
        client = FixtureLlmClient(fixture_dir=tmp_path)
        with pytest.raises(FileNotFoundError) as exc_info:
            client._load_json_fixture("nonexistent.json")
        assert "nonexistent.json" in str(exc_info.value)
        assert "Ensure tests/fixtures/llm exists or configure LLM_FIXTURE_DIR" in str(
            exc_info.value
        )


class TestLlmClientFactory:
    """Verifies factory instantiation, setting resolution, and provider isolation."""

    def test_create_fixture_client_by_default(self) -> None:
        client = create_llm_client()
        assert isinstance(client, FixtureLlmClient)
        assert client.timeout_seconds == 15.0
        assert client.max_retries == 2

    def test_create_fixture_client_with_custom_settings(self) -> None:
        settings = Settings(
            LLM_PROVIDER="fixture",
            LLM_TIMEOUT_SECONDS=30.0,
            LLM_MAX_RETRIES=4,
        )
        client = create_llm_client(settings=settings)
        assert isinstance(client, FixtureLlmClient)
        assert client.timeout_seconds == 30.0
        assert client.max_retries == 4

    def test_create_fixture_client_with_parameter_overrides(self) -> None:
        client = create_llm_client(
            provider="fixture",
            timeout_seconds=25.0,
            max_retries=1,
        )
        assert isinstance(client, FixtureLlmClient)
        assert client.timeout_seconds == 25.0
        assert client.max_retries == 1

    def test_deferred_providers_raise_not_implemented(self) -> None:
        for provider in ("bedrock", "anthropic", "openai", "gemini"):
            with pytest.raises(NotImplementedError) as exc_info:
                create_llm_client(provider=provider)
            assert "deferred" in str(exc_info.value).lower()
            assert provider in str(exc_info.value).lower()

    def test_unknown_provider_raises_value_error(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            create_llm_client(provider="unknown_vendor")
        assert "unsupported llm provider" in str(exc_info.value).lower()

    def test_create_fixture_client_with_settings_fixture_dir(self, tmp_path: Path) -> None:
        settings = Settings(
            LLM_PROVIDER="fixture",
            LLM_FIXTURE_DIR=str(tmp_path),
        )
        client = create_llm_client(settings=settings)
        assert isinstance(client, FixtureLlmClient)
        assert client._fixture_dir == tmp_path
