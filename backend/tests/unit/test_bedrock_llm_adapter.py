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
    async def test_generate_search_queries(self) -> None:
        mock_boto = MagicMock()
        mock_boto.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": """
                            ```json
                            {
                              "search_queries": [
                                "Kolkata authentic street food dining restaurants",
                                "Kolkata top heritage architecture places to visit",
                                "Kolkata budget hotels affordable accommodation",
                                "Kolkata to Digha road travel advisory route conditions"
                              ]
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

        queries = await adapter.generate_search_queries(req)

        assert len(queries) == 4
        assert "street food" in queries[0]
        assert mock_boto.converse.call_count == 1

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
                              "search_queries": [
                                "Kolkata top sights attractions",
                                "Kolkata authentic local food"
                              ]
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
        assert resp.recommended_pace == "MODERATE"
        # Invariant: Bedrock does NOT invent hallucinatory places with fake coordinates
        assert len(resp.candidate_places) == 0
        assert len(resp.regional_travel_notes) == 2
        assert "Planned search query:" in resp.regional_travel_notes[0]

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
                            ```json
                            {
                              "ranked_places": [
                                {
                                  "name": "Target Attraction",
                                  "rank": 1,
                                  "fit_score": 0.95,
                                  "selection_rationale": "High heritage alignment and evidence consensus"
                                }
                              ],
                              "eliminated_candidates": [
                                {
                                  "name": "Lesser Spot",
                                  "reason": "Exceeded candidate selection limit"
                                }
                              ],
                              "overall_pacing_evaluation": "Feasible afternoon schedule"
                            }
                            ```
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
        assert resp.ranked_places[0].fit_score == 0.95
        assert "heritage alignment" in resp.ranked_places[0].selection_rationale
        assert len(resp.eliminated_candidates) == 1
        assert resp.eliminated_candidates[0]["name"] == "Lesser Spot"
        assert mock_boto.converse.call_count == 1

    @pytest.mark.asyncio
    async def test_rank_places_fallback_on_converse_error(self) -> None:
        mock_boto = MagicMock()
        mock_boto.converse.side_effect = RuntimeError("Bedrock service throttle")
        adapter = BedrockLlmClient(boto3_client=mock_boto)

        c1 = PlaceProposal(
            name="Top Consensus Spot",
            category="heritage",
            estimated_latitude=22.5,
            estimated_longitude=88.3,
            fit_score=0.92,
        )
        c2 = PlaceProposal(
            name="Lesser Spot",
            category="park",
            estimated_latitude=22.6,
            estimated_longitude=88.4,
            fit_score=0.55,
        )

        req = RankingRequest(
            candidate_places=[c1, c2],
            max_places_to_select=1,
            traveler_constraints=["Scenic photography"],
        )

        # Should fall back cleanly to evidence consensus fit_score order without crashing
        resp = await adapter.rank_places(req)
        assert len(resp.ranked_places) == 1
        assert resp.ranked_places[0].place.name == "Top Consensus Spot"
        assert resp.ranked_places[0].fit_score == 0.92
        assert "evidence consensus" in resp.ranked_places[0].selection_rationale

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
                            "text": '{"search_queries": ["Digha top sights attractions", "Digha beach sea food"]}'
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

        queries = await adapter.generate_search_queries(req)
        assert len(queries) == 2
        assert "Digha" in queries[0]
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

        queries = await adapter.generate_search_queries(req)
        assert len(queries) >= 3
        assert any("Digha" in q or "Kolkata" in q for q in queries)


@pytest.mark.asyncio
async def test_fixture_llm_client_generate_search_queries() -> None:
    from functions.orchestrator.llm.adapters.fixture_client import FixtureLlmClient

    client = FixtureLlmClient()
    req = ResearchRequest(
        brief_summary="Kolkata foodie weekend",
        origin_name="Kolkata",
        destination_names=["Kolkata"],
        traveler_preferences=["local food"],
    )
    queries = await client.generate_search_queries(req, max_queries=2)
    assert len(queries) == 2
    assert "Kolkata" in queries[0]

