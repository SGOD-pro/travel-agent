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
    PlaceProposal,
    RankedPlace,
    RankingRequest,
    RankingResponse,
    ResearchRequest,
    ResearchResponse,
)
from functions.orchestrator.llm.port import HostedLlmClientPort

logger = logging.getLogger(__name__)


def _extract_json_payload(text: str) -> dict[str, Any]:
    """Extracts and parses a JSON object from raw LLM text or markdown code fence."""
    cleaned = text.strip()
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
        raise ValueError(f"Failed to parse valid JSON payload from Bedrock response: {exc}") from exc


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
            max_retries
            if max_retries is not None
            else effective_settings.LLM_MAX_RETRIES
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
                logger.info("Retrying Bedrock converse call with inference profile %s", prefixed_model_id)
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
            raise TimeoutError(f"Bedrock LLM request timed out after {self.timeout_seconds}s") from exc

    async def research_places(self, request: ResearchRequest) -> ResearchResponse:
        """Discovers candidate points of interest along the destination corridor using Bedrock."""
        system_prompt = (
            "You are an expert India travel intelligence engine. "
            "You suggest authentic points of interest, dining spots, cultural attractions, and rest hubs along travel corridors. "
            "You must return ONLY a single valid JSON object strictly matching the requested schema. "
            "Do not include conversational preamble or markdown commentary outside the JSON block."
        )

        user_content = (
            f"Please propose candidate points of interest for the following travel corridor:\n"
            f"- Origin: {request.origin_name}\n"
            f"- Destinations: {', '.join(request.destination_names)}\n"
            f"- Brief Summary: {request.brief_summary}\n"
            f"- Traveler Preferences: {', '.join(request.traveler_preferences) if request.traveler_preferences else 'General sights, scenic stops'}\n"
            f"- Budget Tier: {request.budget_tier}\n"
            f"- Transport Mode: {request.transport_mode}\n"
            f"- Corridor Max Distance: {request.corridor_distance_km_limit} km\n\n"
            "Return JSON matching this exact structure:\n"
            "{\n"
            '  "candidate_places": [\n'
            "    {\n"
            '      "name": "Place Name",\n'
            '      "category": "restaurant | street_food | heritage | nature | museum | attraction",\n'
            '      "estimated_latitude": 22.5726,\n'
            '      "estimated_longitude": 88.3639,\n'
            '      "description": "Short highlight",\n'
            '      "fit_score": 0.95,\n'
            '      "suggested_stay_minutes": 60,\n'
            '      "price_estimate_inr": 250.0,\n'
            '      "opening_hours": "09:00 - 18:00",\n'
            '      "address": "Approximate landmark or street"\n'
            "    }\n"
            "  ],\n"
            '  "regional_travel_notes": ["Notable driving or weather tip"],\n'
            '  "recommended_pace": "MODERATE"\n'
            "}"
        )

        messages = [{"role": "user", "content": [{"text": user_content}]}]
        system = [{"text": system_prompt}]

        raw_response = await self._call_converse_async(messages, system)
        parsed = _extract_json_payload(raw_response)

        raw_candidates = parsed.get("candidate_places", [])
        proposals: list[PlaceProposal] = []
        for raw_p in raw_candidates:
            if not isinstance(raw_p, dict) or not raw_p.get("name"):
                continue

            try:
                raw_lat = raw_p.get("estimated_latitude", raw_p.get("lat"))
                raw_lng = raw_p.get("estimated_longitude", raw_p.get("lng"))
                lat_val = float(raw_lat) if raw_lat is not None else 0.0
                lng_val = float(raw_lng) if raw_lng is not None else 0.0
                fit_score = float(raw_p.get("fit_score", 1.0))
                stay_mins = int(raw_p.get("suggested_stay_minutes", 60))
                price_val = raw_p.get("price_estimate_inr")

                # Guarantee invariant: generative proposals are strictly EDITORIAL_DISCOVERY candidates
                proposal = PlaceProposal(
                    name=str(raw_p["name"]).strip(),
                    category=str(raw_p.get("category", "attraction")).strip().lower(),
                    estimated_latitude=lat_val,
                    estimated_longitude=lng_val,
                    description=str(raw_p.get("description", "")).strip(),
                    fit_score=max(0.0, min(1.0, fit_score)),
                    suggested_stay_minutes=max(5, min(1440, stay_mins)),
                    price_estimate_inr=float(price_val) if price_val is not None else None,
                    uncertainty="EDITORIAL_DISCOVERY",
                    is_candidate=True,
                    opening_hours=raw_p.get("opening_hours"),
                    address=raw_p.get("address"),
                )
                proposals.append(proposal)
            except Exception as exc:
                logger.debug("Skipping unparseable place proposal %s: %s", raw_p, type(exc).__name__)

        notes = [str(n) for n in parsed.get("regional_travel_notes", []) if isinstance(n, (str, int, float))]
        pace = str(parsed.get("recommended_pace", "MODERATE")).strip().upper()

        return ResearchResponse(
            candidate_places=proposals,
            regional_travel_notes=notes,
            recommended_pace=pace,
            provider="bedrock",
        )

    async def rank_places(self, request: RankingRequest) -> RankingResponse:
        """Evaluates and orders candidate places by brief alignment and feasibility using Bedrock."""
        if not request.candidate_places:
            return RankingResponse(
                ranked_places=[],
                eliminated_candidates=[],
                overall_pacing_evaluation="No candidates provided to rank.",
                provider="bedrock",
            )

        system_prompt = (
            "You are a travel itinerary constraint evaluator. "
            "You evaluate candidate places against traveler constraints and pace preferences. "
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

        raw_response = await self._call_converse_async(messages, system)
        parsed = _extract_json_payload(raw_response)

        candidate_map = {p.name.strip().lower(): p for p in request.candidate_places}
        ranked_list: list[RankedPlace] = []
        ranked_names_lower: set[str] = set()

        raw_ranked = parsed.get("ranked_places", [])
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

        eliminated_list: list[dict[str, str]] = []
        raw_eliminated = parsed.get("eliminated_candidates", [])
        for item in raw_eliminated:
            if isinstance(item, dict) and item.get("name"):
                eliminated_list.append(
                    {
                        "name": str(item["name"]).strip(),
                        "reason": str(item.get("reason", "Excluded by pacing or budget limits")).strip(),
                    }
                )

        # Include any remaining candidates that were neither ranked nor listed as eliminated
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
            parsed.get("overall_pacing_evaluation", f"Selected {len(ranked_list)} candidate places.")
        ).strip()

        return RankingResponse(
            ranked_places=ranked_list,
            eliminated_candidates=eliminated_list,
            overall_pacing_evaluation=pacing_eval,
            provider="bedrock",
        )
