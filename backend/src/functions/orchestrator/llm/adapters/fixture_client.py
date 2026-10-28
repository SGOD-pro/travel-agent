"""Deterministic offline fixture adapter for hosted language model port.

Provides hermetic responses without network calls or cloud dependencies,
enabling fast zero cost local execution and continuous integration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from contracts.llm import (
    RankedPlace,
    RankingRequest,
    RankingResponse,
    ResearchRequest,
    ResearchResponse,
)
from functions.orchestrator.llm.port import HostedLlmClientPort


class FixtureLlmClient(HostedLlmClientPort):
    """Offline deterministic fixture adapter implementing HostedLlmClientPort."""

    def __init__(
        self,
        fixture_dir: Path | str | None = None,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        simulate_timeout: bool = False,
        simulate_error: Exception | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.simulate_timeout = simulate_timeout
        self.simulate_error = simulate_error
        self._fixture_dir = self._resolve_fixture_dir(fixture_dir)

    @classmethod
    def _resolve_fixture_dir(cls, explicit_dir: Path | str | None) -> Path:
        """Resolves the fixture directory using arguments, environment, or relative candidate paths."""
        if explicit_dir is not None:
            return Path(explicit_dir)

        env_dir = os.environ.get("LLM_FIXTURE_DIR")
        if env_dir and env_dir.strip():
            return Path(env_dir.strip())

        current_file = Path(__file__).resolve()
        candidate_paths: list[Path] = []
        if len(current_file.parents) >= 6:
            candidate_paths.append(current_file.parents[5] / "tests" / "fixtures" / "llm")
        candidate_paths.append(current_file.parent.parent / "fixtures")

        for candidate in candidate_paths:
            if candidate.is_dir():
                return candidate

        return candidate_paths[0] if candidate_paths else current_file.parent

    def _load_json_fixture(self, filename: str) -> dict[str, Any]:
        """Loads and parses a JSON fixture file."""
        file_path = self._fixture_dir / filename
        if not file_path.is_file():
            raise FileNotFoundError(
                f"Language model fixture file '{filename}' was not found in directory '{self._fixture_dir}'. "
                "Ensure tests/fixtures/llm exists or configure LLM_FIXTURE_DIR."
            )
        with open(file_path, encoding="utf-8") as f:
            return cast(dict[str, Any], json.load(f))

    async def research_places(self, request: ResearchRequest) -> ResearchResponse:
        """Returns deterministic candidate places based on research request."""
        if self.simulate_timeout:
            raise TimeoutError("Simulated LLM client socket timeout")

        if self.simulate_error is not None:
            raise self.simulate_error

        brief_text = request.brief_summary.lower()
        origin_text = request.origin_name.lower()
        dest_names_lower = [d.lower() for d in request.destination_names]
        prefs_lower = [p.lower() for p in request.traveler_preferences]

        if "timeout" in brief_text or "timeout" in origin_text:
            raise TimeoutError("Simulated LLM socket timeout for brief query")

        if "error" in brief_text or "error" in origin_text:
            raise RuntimeError("Simulated LLM provider error for brief query")

        if "empty" in brief_text or "empty" in dest_names_lower:
            data = self._load_json_fixture("research_empty.json")
            return ResearchResponse.model_validate(data)

        if "distant" in brief_text or "darjeeling" in dest_names_lower:
            data = self._load_json_fixture("research_distant.json")
            return ResearchResponse.model_validate(data)

        if any(food_kw in prefs_lower for food_kw in ("food", "dining", "cuisine", "street_food")):
            data = self._load_json_fixture("research_food_kolkata.json")
            return ResearchResponse.model_validate(data)

        # Default standard corridor response (Kolkata to Digha)
        data = self._load_json_fixture("research_kolkata_digha.json")
        return ResearchResponse.model_validate(data)

    async def rank_places(self, request: RankingRequest) -> RankingResponse:
        """Returns deterministic ranked places evaluated against constraints."""
        if self.simulate_timeout:
            raise TimeoutError("Simulated LLM client socket timeout")

        if self.simulate_error is not None:
            raise self.simulate_error

        constraints_text = " ".join(c.lower() for c in request.traveler_constraints)
        if "timeout" in constraints_text:
            raise TimeoutError("Simulated LLM socket timeout during ranking")

        if "error" in constraints_text:
            raise RuntimeError("Simulated LLM provider error during ranking")

        if not request.candidate_places:
            return RankingResponse(
                ranked_places=[],
                eliminated_candidates=[],
                overall_pacing_evaluation="No candidates provided to rank.",
                provider="fixture",
            )

        # If candidates are provided dynamically, rank them by fit_score descending
        sorted_candidates = sorted(
            request.candidate_places,
            key=lambda p: p.fit_score,
            reverse=True,
        )

        max_select = request.max_places_to_select
        selected = sorted_candidates[:max_select]
        eliminated = sorted_candidates[max_select:]

        ranked_places: list[RankedPlace] = []
        for idx, place in enumerate(selected, start=1):
            ranked_places.append(
                RankedPlace(
                    place=place,
                    rank=idx,
                    fit_score=place.fit_score,
                    selection_rationale=(
                        f"Ranked {idx} based on fit score {place.fit_score:.2f} "
                        f"and category alignment ({place.category})."
                    ),
                )
            )

        eliminated_records: list[dict[str, str]] = [
            {
                "name": p.name,
                "reason": f"Pacing limit reached (max {max_select} places); lower fit score ({p.fit_score:.2f}).",
            }
            for p in eliminated
        ]

        return RankingResponse(
            ranked_places=ranked_places,
            eliminated_candidates=eliminated_records,
            overall_pacing_evaluation=f"Selected top {len(ranked_places)} of {len(request.candidate_places)} candidates.",
            provider="fixture",
        )
