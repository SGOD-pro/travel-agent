"""AWS Bedrock runtime client adapter implementing HostedLlmClientPort.

Invokes foundation models hosted on AWS Bedrock via boto3 bedrock-runtime.
Uses converse API with configurable model ID, region, timeout, and bounded retries.
Safely offloads blocking boto3 operations to a worker thread for async execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, cast

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from config.config import Settings, get_settings
from contracts.llm import (
    RankedPlace,
    RankingRequest,
    RankingResponse,
    ResearchRequest,
    ResearchResponse,
)
from functions.orchestrator.llm.port import HostedLlmClientPort

logger = logging.getLogger(__name__)


def _extract_json_payload(text: str | dict[str, Any]) -> dict[str, Any]:
    """Extracts and parses a JSON object from raw LLM text, markdown code fence, or dictionary."""
    if isinstance(text, dict):
        return text
    cleaned = str(text).strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if match:
        cleaned = match.group(1).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start : end + 1]

    try:
        return cast(dict[str, Any], json.loads(cleaned))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Failed to parse valid JSON payload from Bedrock response: {exc}"
        ) from exc


class BedrockLlmClient(HostedLlmClientPort):
    """Hosted language model client adapter using AWS Bedrock runtime."""

    def __init__(
        self,
        model_id: str | None = None,
        region: str | None = None,
        profile_name: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        boto3_client: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        effective_settings = settings or get_settings()
        self.model_id = (
            model_id
            if model_id is not None
            else getattr(effective_settings, "BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
        )
        self.region = (
            region
            if region is not None
            else getattr(effective_settings, "AWS_REGION", "ap-south-1")
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else effective_settings.LLM_TIMEOUT_SECONDS
        )
        self.max_retries = (
            max_retries if max_retries is not None else effective_settings.LLM_MAX_RETRIES
        )

        if boto3_client is not None:
            self._client = boto3_client
        else:
            self._client = self._create_bedrock_client(
                region=self.region,
                profile_name=profile_name,
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
                settings=effective_settings,
            )

    @classmethod
    def _create_bedrock_client(
        cls,
        region: str,
        profile_name: str | None,
        timeout_seconds: float,
        max_retries: int,
        settings: Settings,
    ) -> Any:
        """Constructs configured boto3 bedrock-runtime client with credential chain."""
        resolved_profile = profile_name
        if resolved_profile is None:
            env_profile = os.environ.get("AWS_PROFILE")
            if env_profile:
                resolved_profile = env_profile
            elif getattr(settings, "AWS_PROFILE", None):
                resolved_profile = settings.AWS_PROFILE
            elif getattr(settings, "ENVIRONMENT", "local") == "local":
                try:
                    default_session = boto3.Session()
                    if "aws" in default_session.available_profiles:
                        resolved_profile = "aws"
                except Exception as exc:
                    logger.debug("Failed inspecting local boto3 profiles: %s", type(exc).__name__)

        client_config = Config(
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            retries={"max_attempts": max_retries + 1, "mode": "standard"},
        )

        if resolved_profile:
            session = boto3.Session(profile_name=resolved_profile)
        else:
            session = boto3.Session()

        return session.client("bedrock-runtime", region_name=region, config=client_config)

    def _sync_invoke_converse(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        system: list[dict[str, Any]] | None = None,
    ) -> str:
        """Synchronously invokes the Bedrock converse API with automatic inference profile fallback."""
        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        try:
            response = self._client.converse(**kwargs)
            text_val = response["output"]["message"]["content"][0]["text"]
            return str(text_val)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            error_msg = exc.response.get("Error", {}).get("Message", "")
            # Retry with APAC inference profile prefix if model ID requires cross region routing
            if (
                "ValidationException" in error_code
                and "inference profile" in error_msg.lower()
                and not model_id.startswith(("apac.", "us.", "eu.", "global."))
            ):
                prefixed_model_id = f"apac.{model_id}"
                logger.info(
                    "Retrying Bedrock converse call with inference profile %s", prefixed_model_id
                )
                kwargs["modelId"] = prefixed_model_id
                response = self._client.converse(**kwargs)
                text_val = response["output"]["message"]["content"][0]["text"]
                return str(text_val)
            raise

    async def _call_converse_async(
        self,
        messages: list[dict[str, Any]],
        system: list[dict[str, Any]] | None = None,
    ) -> str:
        """Safely offloads blocking converse call to a worker thread with timeout bounds."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._sync_invoke_converse,
                    self.model_id,
                    messages,
                    system,
                ),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            logger.warning("Bedrock LLM request timed out after %.1f seconds", self.timeout_seconds)
            raise TimeoutError(
                f"Bedrock LLM request timed out after {self.timeout_seconds}s"
            ) from exc

    async def generate_search_queries(
        self,
        request: ResearchRequest,
        max_queries: int = 4,
    ) -> list[Any]:
        """Formulates targeted search queries for web evidence discovery using Bedrock."""
        system_prompt = (
            "You are an expert India travel research query planner. "
            "You formulate targeted search engine queries to discover authentic points of interest, "
            "regional dining spots, cultural attractions, and travel advisories from real web sources. "
            "You must return ONLY a single valid JSON object strictly matching this schema: "
            '{"search_queries": [{"query": "search query text", "purpose": "attractions|dining|lodging|advisory", "destination": "city name"}]}. '
            "Do not include conversational preamble or markdown commentary outside the JSON block."
        )

        user_content = (
            f"Formulate 3 to 4 high-signal search queries for this travel corridor:\n"
            f"- Origin: {request.origin_name}\n"
            f"- Destinations: {', '.join(request.destination_names)}\n"
            f"- Summary: {request.brief_summary}\n"
            f"- Preferences: {', '.join(request.traveler_preferences) if request.traveler_preferences else 'General sights, scenic stops'}\n"
            f"- Budget Tier: {request.budget_tier}\n"
            f"- Transport Mode: {request.transport_mode}\n\n"
            "Requirements:\n"
            f"1. Distribute queries across destinations: {', '.join(request.destination_names)} so each destination has research coverage.\n"
            "2. Include 1 road travel advisory query covering route conditions, weather warnings, or road closures.\n"
            "3. Each item in search_queries must be an object with query, purpose (attractions, dining, lodging, or advisory), and destination fields.\n"
            'Return JSON matching: {"search_queries": [{"query": "...", "purpose": "attractions|dining|lodging|advisory", "destination": "..."}]}'
        )

        messages = [{"role": "user", "content": [{"text": user_content}]}]
        system = [{"text": system_prompt}]

        try:
            raw_response = await self._call_converse_async(messages, system)
            parsed = _extract_json_payload(raw_response)
            raw_items = parsed.get("search_queries") or parsed.get("queries") or []
            queries: list[Any] = []
            if isinstance(raw_items, list):
                for item in raw_items:
                    if isinstance(item, dict):
                        q_str = str(item.get("query") or item.get("q") or "").strip()
                        if q_str:
                            queries.append(item)
                    elif isinstance(item, str) and item.strip():
                        # Legacy plain string response from Bedrock
                        queries.append(item.strip())
        except TimeoutError:
            raise
        except Exception as exc:
            logger.warning("Bedrock query planning converse failed: %s", exc)
            queries = []

        from functions.orchestrator.evidence.query_planner import QueryPlanningService

        if queries:
            return QueryPlanningService.structure_and_validate_queries(
                queries=queries,
                destinations=request.destination_names,
                origin=request.origin_name,
                max_queries=max_queries,
            )

        # Deterministic fallback respecting destination coverage and purpose constraints
        return QueryPlanningService.plan_destination_queries(
            brief={
                "origin_name": request.origin_name,
                "destinations": [{"name": d} for d in request.destination_names],
                "traveler_preferences": request.traveler_preferences,
                "budget_tier": request.budget_tier,
            },
            max_queries=max_queries,
        )

    async def research_places(self, request: ResearchRequest) -> ResearchResponse:
        """Formulates web discovery queries and regional notes for search-backed discovery."""
        queries = await self.generate_search_queries(request)
        notes = [f"Planned search query: {q}" for q in queries]
        return ResearchResponse(
            candidate_places=[],
            regional_travel_notes=notes,
            recommended_pace="MODERATE",
            provider="bedrock",
            planned_queries=queries,
            covered_destinations=getattr(queries, "covered_destinations", []),
            uncovered_destinations=getattr(queries, "uncovered_destinations", []),
            coverage_reason=getattr(queries, "coverage_reason", None),
        )

    async def rank_places(self, request: RankingRequest) -> RankingResponse:
        """Evaluates and orders candidate places by traveler constraints, pacing, and multi-site consensus."""
        if not request.candidate_places:
            return RankingResponse(
                ranked_places=[],
                eliminated_candidates=[],
                overall_pacing_evaluation="No candidates provided to rank.",
                provider="bedrock",
            )

        # Baseline consensus sort fallback
        fallback_candidates = sorted(
            request.candidate_places,
            key=lambda p: (p.fit_score, -(p.suggested_stay_minutes)),
            reverse=True,
        )

        def _build_fallback_response(reason: str) -> RankingResponse:
            max_sel = min(len(fallback_candidates), request.max_places_to_select)
            sel = fallback_candidates[:max_sel]
            elim = fallback_candidates[max_sel:]
            ranked = [
                RankedPlace(
                    place=p,
                    rank=idx,
                    fit_score=p.fit_score,
                    selection_rationale=f"Ranked #{idx} based on multi-site evidence consensus (fit: {p.fit_score:.2f})",
                )
                for idx, p in enumerate(sel, start=1)
            ]
            eliminated = [
                {"name": p.name, "reason": "Exceeded maximum candidate selection limit"}
                for p in elim
            ]
            return RankingResponse(
                ranked_places=ranked,
                eliminated_candidates=eliminated,
                overall_pacing_evaluation=f"Selected {len(ranked)} places using evidence consensus ({reason}).",
                provider="bedrock",
            )

        system_prompt = (
            "You are a travel itinerary constraint evaluator. "
            "You evaluate candidate places against traveler constraints and pace preferences, "
            "weighting multi-site consensus appearance fit_scores. "
            "You must return ONLY a single valid JSON object strictly matching the requested schema."
        )

        candidates_summary = [
            {
                "name": p.name,
                "category": p.category,
                "fit_score": p.fit_score,
                "suggested_stay_minutes": p.suggested_stay_minutes,
                "description": p.description,
            }
            for p in request.candidate_places
        ]

        user_content = (
            f"Please rank the following candidates up to a maximum of {request.max_places_to_select} places:\n"
            f"- Candidates: {json.dumps(candidates_summary, indent=2)}\n"
            f"- Traveler Constraints: {', '.join(request.traveler_constraints) if request.traveler_constraints else 'None'}\n\n"
            "Return JSON matching this exact structure:\n"
            "{\n"
            '  "ranked_places": [\n'
            "    {\n"
            '      "name": "Exact candidate name",\n'
            '      "rank": 1,\n'
            '      "fit_score": 0.98,\n'
            '      "selection_rationale": "Why chosen"\n'
            "    }\n"
            "  ],\n"
            '  "eliminated_candidates": [\n'
            "    {\n"
            '      "name": "Candidate name",\n'
            '      "reason": "Why omitted"\n'
            "    }\n"
            "  ],\n"
            '  "overall_pacing_evaluation": "Pacing note"\n'
            "}"
        )

        messages = [{"role": "user", "content": [{"text": user_content}]}]
        system = [{"text": system_prompt}]

        try:
            raw_response = await self._call_converse_async(messages, system)
            parsed = _extract_json_payload(raw_response)
        except TimeoutError:
            raise
        except Exception as exc:
            logger.warning(
                "Bedrock candidate ranking converse failed: %s; falling back to consensus order",
                exc,
            )
            return _build_fallback_response("inference fallback")

        candidate_map = {p.name.strip().lower(): p for p in request.candidate_places}
        ranked_list: list[RankedPlace] = []
        ranked_names_lower: set[str] = set()

        raw_ranked = parsed.get("ranked_places", [])
        if not isinstance(raw_ranked, list) or not raw_ranked:
            return _build_fallback_response("empty model ranking")

        for idx, item in enumerate(raw_ranked, start=1):
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("name", "")).strip()
            item_key = item_name.lower()
            if item_key in candidate_map and item_key not in ranked_names_lower:
                original_place = candidate_map[item_key]
                fit_val = float(item.get("fit_score", original_place.fit_score))
                rationale_val = str(item.get("selection_rationale", "")).strip()
                ranked_place = RankedPlace(
                    place=original_place,
                    rank=idx,
                    fit_score=max(0.0, min(1.0, fit_val)),
                    selection_rationale=rationale_val or f"Ranked #{idx} for brief alignment",
                )
                ranked_list.append(ranked_place)
                ranked_names_lower.add(item_key)

        if not ranked_list:
            return _build_fallback_response("unmatched candidate names")

        eliminated_list: list[dict[str, str]] = []
        raw_eliminated = parsed.get("eliminated_candidates", [])
        if isinstance(raw_eliminated, list):
            for item in raw_eliminated:
                if isinstance(item, dict) and item.get("name"):
                    eliminated_list.append(
                        {
                            "name": str(item["name"]).strip(),
                            "reason": str(
                                item.get("reason", "Excluded by pacing or budget limits")
                            ).strip(),
                        }
                    )

        # Include remaining candidates that were neither ranked nor eliminated
        for cand in request.candidate_places:
            cand_key = cand.name.strip().lower()
            if cand_key not in ranked_names_lower and not any(
                e["name"].strip().lower() == cand_key for e in eliminated_list
            ):
                eliminated_list.append(
                    {
                        "name": cand.name,
                        "reason": "Exceeded maximum stop selection threshold",
                    }
                )

        pacing_eval = str(
            parsed.get(
                "overall_pacing_evaluation", f"Selected {len(ranked_list)} candidate places."
            )
        ).strip()

        return RankingResponse(
            ranked_places=ranked_list,
            eliminated_candidates=eliminated_list,
            overall_pacing_evaluation=pacing_eval,
            provider="bedrock",
        )
