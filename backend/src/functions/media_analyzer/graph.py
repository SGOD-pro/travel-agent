"""Evidence-backed location candidate resolution for analyzed media."""

from __future__ import annotations

import logging
import re
from typing import Any

from config.config import get_settings
from contracts.media import (
    LocationCandidate,
    LocationVerificationStatus,
    MediaAnalyzed,
    MediaLocationGraph,
    VerifiedPlace,
)
from functions.media_analyzer.geocoding import NominatimGeocoder

logger = logging.getLogger(__name__)


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", value.casefold()).split())


def _text_match(query: str, candidate_name: str, display_name: str) -> float:
    normalized_query = _normalize(query)
    normalized_name = _normalize(candidate_name)
    normalized_display = _normalize(display_name)
    if normalized_query and (
        normalized_query in normalized_name
        or normalized_name in normalized_query
        or normalized_query in normalized_display
    ):
        return 1.0
    query_tokens = set(normalized_query.split())
    candidate_tokens = set(f"{normalized_name} {normalized_display}".split())
    if not query_tokens:
        return 0.0
    overlap = len(query_tokens & candidate_tokens) / len(query_tokens)
    return overlap if overlap >= 0.8 else 0.0


class LocationGraphBuilder:
    """Uses geographic lookup only to corroborate observed media clues."""

    def __init__(self, geocoder: Any | None = None) -> None:
        self._geocoder = geocoder

    async def build_graph(self, analyzed: MediaAnalyzed) -> MediaLocationGraph:
        if get_settings().MEDIA_GEOCODER_PROVIDER == "none":
            return MediaLocationGraph(
                job_id=analyzed.job_id,
                analysis_job_id=analyzed.job_id,
            )
        geocoder = self._geocoder or NominatimGeocoder()
        clues_by_query: dict[str, list[Any]] = {}
        for clue in analyzed.fused_clues.clues:
            query = clue.text_content.strip()
            normalized = _normalize(query)
            if 3 <= len(normalized) <= 100:
                clues_by_query.setdefault(normalized, []).append(clue)

        candidates_by_id: dict[str, dict[str, Any]] = {}
        ranked_queries = sorted(
            clues_by_query.items(),
            key=lambda item: (len(item[0].split()), len(item[0])),
            reverse=True,
        )
        for query, source_clues in ranked_queries[:5]:
            results = await geocoder.search(query)
            for result in results:
                name = str(result.get("name") or "").strip()
                display_name = str(result.get("display_name") or name).strip()
                match_score = _text_match(query, name, display_name)
                if match_score == 0:
                    continue
                osm_id = result.get("osm_id")
                osm_type = result.get("osm_type", "place")
                candidate_id = f"{osm_type}:{osm_id}" if osm_id else (
                    f"{result.get('lat')}:{result.get('lon')}"
                )
                entry = candidates_by_id.setdefault(
                    candidate_id,
                    {
                        "result": result,
                        "signals": {},
                        "queries": [],
                    },
                )
                for clue in source_clues:
                    entry["signals"][clue.clue_type] = max(
                        float(clue.confidence),
                        entry["signals"].get(clue.clue_type, 0.0),
                    )
                entry["queries"].append((query, match_score))

        candidates: list[LocationCandidate] = []
        verified_places: list[VerifiedPlace] = []
        for candidate_id, entry in candidates_by_id.items():
            result = entry["result"]
            address = result.get("address", {})
            signals: dict[str, float] = entry["signals"]
            match_score = max(score for _, score in entry["queries"])
            confidence = max(signals.values(), default=0.0) * match_score
            independent_modalities = len(signals)
            if independent_modalities >= 2:
                confidence = min(0.99, confidence + 0.12)
            candidate_name = str(result.get("name") or result.get("display_name") or "Unknown place")
            locality = str(
                address.get("city")
                or address.get("town")
                or address.get("village")
                or address.get("municipality")
                or address.get("state")
                or "India"
            )
            signal_names = sorted(signals)
            candidate = LocationCandidate(
                candidate_id=candidate_id,
                name=candidate_name,
                locality=locality,
                state=address.get("state"),
                latitude=float(result["lat"]),
                longitude=float(result["lon"]),
                confidence=round(confidence, 2),
                match_type=(
                    "fused"
                    if independent_modalities >= 2
                    else "ocr_text"
                    if "text_ocr" in signals
                    else "asr_text"
                ),
                matched_clues=[query for query, _ in entry["queries"]],
                evidence={
                    "geocoder": "nominatim",
                    "osm_type": result.get("osm_type"),
                    "osm_id": result.get("osm_id"),
                    "display_name": result.get("display_name"),
                    "independent_modalities": signal_names,
                },
                explanation=(
                    f"Nominatim matched observed media text; corroborated by "
                    f"{', '.join(signal_names) or 'no independent signals'}."
                ),
            )
            candidates.append(candidate)
            if independent_modalities >= 2 and confidence >= 0.8:
                verified_places.append(
                    VerifiedPlace(
                        primary_name=candidate_name,
                        locality=locality,
                        latitude=candidate.latitude,
                        longitude=candidate.longitude,
                        confidence=round(confidence, 2),
                        verification_status=LocationVerificationStatus.VERIFIED,
                        corroborating_evidence=candidate.matched_clues,
                        candidates_reviewed=[candidate],
                    )
                )

        candidates.sort(key=lambda candidate: candidate.confidence, reverse=True)
        verified_places.sort(key=lambda place: place.confidence, reverse=True)
        verified_ids = {place.primary_name for place in verified_places}
        verified_candidates = [
            candidate for candidate in candidates if candidate.name in verified_ids
        ]
        ambiguous = [
            candidate for candidate in candidates if candidate.name not in verified_ids
        ]
        return MediaLocationGraph(
            job_id=analyzed.job_id,
            analysis_job_id=analyzed.job_id,
            candidates=candidates,
            verified_locations=verified_candidates,
            ambiguous_locations=ambiguous,
            inaccessible_locations=[],
        )
