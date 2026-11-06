"""Unit tests for AWS Bedrock LLM adapter, converse invocation, and contracts."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from contracts.llm import (
    PlaceProposal,
    RankingRequest,
    RankingResponse,
    ResearchRequest,
    ResearchResponse,
)
from functions.orchestrator.llm.adapters.bedrock import BedrockLlmClient, _extract_json_payload
from functions.orchestrator.llm.port import HostedLlmClientPort


class TestBedrockJsonExtraction:
    """Verifies markdown codeblock and JSON text extraction logic."""

    def test_extract_plain_json(self) -> None:
        text = '{"candidate_places": [], "regional_travel_notes": []}'
        payload = _extract_json_payload(text)
        assert payload["candidate_places"] == []

    def test_extract_markdown_json_fence(self) -> None:
        text = (
            "Here is the result:\n"
            "```json\n"
            "{\n"
            '  "candidate_places": [{"name": "Test Place", "category": "restaurant"}],\n'
            '  "regional_travel_notes": ["Drive carefully"]\n'
            "}\n"
            "```\n"
            "Hope this helps!"
        )
        payload = _extract_json_payload(text)
        assert len(payload["candidate_places"]) == 1
        assert payload["candidate_places"][0]["name"] == "Test Place"

    def test_extract_invalid_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            _extract_json_payload("not valid json at all")
        assert "Failed to parse valid JSON" in str(exc_info.value)


class TestBedrockLlmClient:
    """Verifies Bedrock adapter execution, typed contract mappings, and error handling."""

    def test_conforms_to_port(self) -> None:
        mock_client = MagicMock()
        adapter = BedrockLlmClient(boto3_client=mock_client)
        assert isinstance(adapter, HostedLlmClientPort)
        assert adapter.timeout_seconds == 15.0
        assert adapter.max_retries == 2

    @pytest.mark.asyncio
    async def test_research_places_successful_mapping(self) -> None:
        mock_boto = MagicMock()
        mock_boto.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": """
                            ```json
                            {
                              "candidate_places": [
                                {
                                  "name": "Kolaghat Highway Hub",
                                  "category": "restaurant",
                                  "estimated_latitude": 22.4285,
                                  "estimated_longitude": 87.8712,
                                  "description": "Popular stop for snacks",
                                  "fit_score": 0.95,
                                  "suggested_stay_minutes": 45,
                                  "price_estimate_inr": 300.0,
                                  "opening_hours": "07:00 - 23:00",
                                  "address": "NH16 Kolaghat"
                                }
                              ],
                              "regional_travel_notes": ["Monsoon caution on NH16"],
                              "recommended_pace": "RELAXED"
                            }
                            ```
                            """
                        }
                    ]
                }
            }
        }

        adapter = BedrockLlmClient(
            model_id="amazon.nova-lite-v1:0",
            region="ap-south-1",
            timeout_seconds=5.0,
            boto3_client=mock_boto,
        )

        req = ResearchRequest(
            brief_summary="Kolkata to Digha coastal journey",
            origin_name="Kolkata",
            destination_names=["Digha"],
            traveler_preferences=["food"],
        )

        resp = await adapter.research_places(req)

        assert isinstance(resp, ResearchResponse)
        assert resp.provider == "bedrock"
        assert resp.recommended_pace == "RELAXED"
        assert len(resp.candidate_places) == 1
        assert resp.regional_travel_notes == ["Monsoon caution on NH16"]

        place = resp.candidate_places[0]
        assert isinstance(place, PlaceProposal)
        assert place.name == "Kolaghat Highway Hub"
        assert place.category == "restaurant"
        assert place.estimated_latitude == 22.4285
        assert place.estimated_longitude == 87.8712
        assert place.fit_score == 0.95
        assert place.suggested_stay_minutes == 45
        assert place.price_estimate_inr == 300.0
        # Crucial invariant: must strictly remain unverified candidate
        assert place.uncertainty == "EDITORIAL_DISCOVERY"
        assert place.is_candidate is True

        assert mock_boto.converse.call_count == 1
        call_kwargs = mock_boto.converse.call_args[1]
        assert call_kwargs["modelId"] == "amazon.nova-lite-v1:0"

    @pytest.mark.asyncio
    async def test_rank_places_successful_mapping(self) -> None:
        mock_boto = MagicMock()
        mock_boto.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": """
                            {
                              "ranked_places": [
                                {
                                  "name": "Target Attraction",
                                  "rank": 1,
                                  "fit_score": 0.99,
                                  "selection_rationale": "Strongest brief alignment"
                                }
                              ],
                              "eliminated_candidates": [
                                {
                                  "name": "Lesser Spot",
                                  "reason": "Exceeded daily pace limit"
                                }
                              ],
                              "overall_pacing_evaluation": "Feasible pace for 1-day trip."
                            }
                            """
                        }
                    ]
                }
            }
        }

        adapter = BedrockLlmClient(boto3_client=mock_boto)

        c1 = PlaceProposal(
            name="Target Attraction",
            category="heritage",
            estimated_latitude=22.5,
            estimated_longitude=88.3,
            fit_score=0.90,
        )
        c2 = PlaceProposal(
            name="Lesser Spot",
            category="park",
            estimated_latitude=22.6,
            estimated_longitude=88.4,
            fit_score=0.60,
        )

        req = RankingRequest(
            candidate_places=[c1, c2],
            max_places_to_select=1,
            traveler_constraints=["Must finish by sunset"],
        )

        resp = await adapter.rank_places(req)

        assert isinstance(resp, RankingResponse)
        assert resp.provider == "bedrock"
        assert len(resp.ranked_places) == 1
        assert resp.ranked_places[0].place.name == "Target Attraction"
        assert resp.ranked_places[0].rank == 1
        assert resp.ranked_places[0].fit_score == 0.99
        assert resp.ranked_places[0].selection_rationale == "Strongest brief alignment"
        assert len(resp.eliminated_candidates) == 1
        assert resp.eliminated_candidates[0]["name"] == "Lesser Spot"

    @pytest.mark.asyncio
    async def test_rank_places_empty_input(self) -> None:
        mock_boto = MagicMock()
        adapter = BedrockLlmClient(boto3_client=mock_boto)
        resp = await adapter.rank_places(RankingRequest(candidate_places=[]))
        assert len(resp.ranked_places) == 0
        assert mock_boto.converse.call_count == 0

    @pytest.mark.asyncio
    async def test_timeout_handling(self) -> None:
        mock_boto = MagicMock()

        def slow_converse(**kwargs: object) -> object:
            import time
            time.sleep(0.3)
            return {"output": {"message": {"content": [{"text": "{}"}]}}}

        mock_boto.converse.side_effect = slow_converse

        adapter = BedrockLlmClient(
            timeout_seconds=0.05,
            boto3_client=mock_boto,
        )

        req = ResearchRequest(
            brief_summary="Timeout test",
            origin_name="Kolkata",
            destination_names=["Digha"],
        )

        with pytest.raises(TimeoutError) as exc_info:
            await adapter.research_places(req)
        assert "timed out" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_inference_profile_auto_retry(self) -> None:
        mock_boto = MagicMock()
        # First call fails with ValidationException requiring inference profile
        error_response = {
            "Error": {
                "Code": "ValidationException",
                "Message": "Invocation of model ID amazon.nova-lite-v1:0 with on-demand throughput isn’t supported. Retry your request with the ID or ARN of an inference profile that contains this model.",
            }
        }
        client_error = ClientError(error_response, "Converse")
        success_response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": '{"candidate_places": [{"name": "Retry Place", "category": "nature", "estimated_latitude": 22.0, "estimated_longitude": 88.0}]}'
                        }
                    ]
                }
            }
        }

        mock_boto.converse.side_effect = [client_error, success_response]

        adapter = BedrockLlmClient(
            model_id="amazon.nova-lite-v1:0",
            boto3_client=mock_boto,
        )

        req = ResearchRequest(
            brief_summary="Test retry",
            origin_name="Kolkata",
            destination_names=["Digha"],
        )

        resp = await adapter.research_places(req)
        assert len(resp.candidate_places) == 1
        assert resp.candidate_places[0].name == "Retry Place"
        assert mock_boto.converse.call_count == 2
        # Second call should have prefixed with apac.
        assert mock_boto.converse.call_args_list[1][1]["modelId"] == "apac.amazon.nova-lite-v1:0"

    @pytest.mark.asyncio
    async def test_live_bedrock_connected_smoke(self) -> None:
        import os
        if not os.environ.get("RUN_CONNECTED_LLM_TESTS"):
            pytest.skip("RUN_CONNECTED_LLM_TESTS not set; skipping connected Bedrock live test")

        adapter = BedrockLlmClient(
            model_id="apac.amazon.nova-lite-v1:0",
            region="ap-south-1",
            timeout_seconds=20.0,
        )

        req = ResearchRequest(
            brief_summary="Day drive from Kolkata to Digha",
            origin_name="Kolkata",
            destination_names=["Digha"],
            traveler_preferences=["food", "heritage"],
        )

        resp = await adapter.research_places(req)
        assert isinstance(resp, ResearchResponse)
        assert resp.provider == "bedrock"
        assert len(resp.candidate_places) >= 1
        for p in resp.candidate_places:
            assert p.uncertainty == "EDITORIAL_DISCOVERY"
            assert p.is_candidate is True

