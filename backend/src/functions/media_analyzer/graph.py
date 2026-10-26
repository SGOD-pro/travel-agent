"""Geospatial fusion and candidate ranking engine (Phase 11).

Maps multimodal clues (OCR text, ASR keywords, visual embeddings) to canonical places,
ranking candidates and generating human-auditable explanations.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from contracts.media import (
    FusedMediaClues,
    LocationCandidate,
    MediaAnalyzed,
    MediaLocationGraph,
    LocationVerificationStatus,
)

logger = logging.getLogger(__name__)


class LocationGraphBuilder:
    """Fuses visual, text, and audio clues to resolve geographical coordinates."""

    def __init__(self, gazetteer: list[dict[str, Any]] | None = None) -> None:
        # In production, this queries your canonical_places DB (Phase 4/7)
        self.gazetteer = gazetteer or self._load_india_gazetteer()

    def _load_india_gazetteer(self) -> list[dict[str, Any]]:
        """Fallback mock gazetteer for offline testing."""
        return [
            {"id": "place_1", "name": "Abbey Falls", "lat": 12.4542, "lng": 75.7196, "category": "waterfall", "state": "Karnataka"},
            {"id": "place_2", "name": "Madikeri Fort", "lat": 12.4244, "lng": 75.7382, "category": "fort", "state": "Karnataka"},
            {"id": "place_3", "name": "Ranganathittu Bird Sanctuary", "lat": 12.4239, "lng": 76.6947, "category": "nature", "state": "Karnataka"},
        ]

    def build_graph(self, analyzed: MediaAnalyzed) -> MediaLocationGraph:
        """Processes analyzed clues and outputs a structured MediaLocationGraph."""
        candidates: list[LocationCandidate] = []
        clues = analyzed.fused_clues

        # Simple fuzzy/heuristic matching against the gazetteer
        for place in self.gazetteer:
            matched_clues: list[str] = []
            confidence = 0.0
            evidence: dict[str, Any] = {}

            # Match landmarks identified in OCR / ASR
            for landmark in clues.landmarks_identified:
                if landmark.lower() in place["name"].lower() or place["name"].lower() in landmark.lower():
                    matched_clues.append(f"Landmark match: {landmark}")
                    confidence += 0.4
                    evidence["landmark_match"] = landmark

            # Match geographic tokens (e.g. Coorg, Karnataka)
            for token in clues.audio_keywords:
                if token.lower() in place["state"].lower() or token.lower() in place["name"].lower():
                    matched_clues.append(f"Geo token: {token}")
                    confidence += 0.2
                    evidence["geo_token"] = token

            # Match signage text (OCR)
            for text in clues.signage_texts:
                if any(word.lower() in text.lower() for word in place["name"].split()):
                    matched_clues.append(f"Signage text: {text}")
                    confidence += 0.3
                    evidence["ocr_text"] = text

            # Cap confidence at 1.0
            final_conf = min(confidence, 1.0)

            if final_conf > 0.1:
                match_type = "fused"
                if "ocr_text" in evidence and "landmark_match" not in evidence:
                    match_type = "ocr_text"
                elif "landmark_match" in evidence and "ocr_text" not in evidence:
                    match_type = "visual"

                explanation = self._generate_explanation(place, evidence)

                candidates.append(
                    LocationCandidate(
                        candidate_id=place["id"],
                        name=place["name"],
                        locality=place["state"],
                        state=place["state"],
                        country="India",
                        latitude=place["lat"],
                        longitude=place["lng"],
                        confidence=round(final_conf, 2),
                        match_type=match_type,  # type: ignore
                        matched_clues=matched_clues,
                        evidence=evidence,
                        explanation=explanation,
                    )
                )

        # Sort candidates by confidence score descending
        candidates.sort(key=lambda x: x.confidence, reverse=True)

        # Classify candidates based on Phase 11 verification thresholds
        verified = [c for c in candidates if c.confidence >= 0.8]
        ambiguous = [c for c in candidates if 0.4 <= c.confidence < 0.8]
        inaccessible = [c for c in candidates if c.confidence < 0.4]

        return MediaLocationGraph(
            job_id=analyzed.job_id,
            analysis_job_id=analyzed.job_id,
            candidates=candidates,
            verified_locations=verified,
            ambiguous_locations=ambiguous,
            inaccessible_locations=inaccessible,
            public_media_urls={},  # Populated with S3 presigned URLs in production
        )

    def _generate_explanation(self, place: dict[str, Any], evidence: dict[str, Any]) -> str:
        """Generates a transparent, human-readable explanation of why a place was proposed."""
        reasons = []
        if "landmark_match" in evidence:
            reasons.append(f"Visual landmark match: {evidence['landmark_match']}")
        if "ocr_text" in evidence:
            reasons.append(f"Signboard OCR detected text: '{evidence['ocr_text']}'")
        if "geo_token" in evidence:
            reasons.append(f"Geographical context match: {evidence['geo_token']}")

        return " | ".join(reasons) if reasons else f"Geospatial proximity correlation to {place['name']}."