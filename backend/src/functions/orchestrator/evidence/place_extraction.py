"""Service for extracting candidate places from collected web search and scraping evidence.

Conforms to evidence-backed discovery principles:
1. Places are discovered from real web search and scraping observations (SerpAPI + Scrapling).
2. Located with real latitude and longitude from verified GPS data in search results or geocoding.
3. Ranked by consensus appearance frequency across distinct websites and publishers.
4. Generates PlaceProposal domain records without hallucinatory coordinate estimation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from contracts.llm import PlaceProposal

logger = logging.getLogger(__name__)


@dataclass
class PlaceEvidenceAccumulator:
    """Internal accumulator tracking appearance across distinct web sources."""

    canonical_name: str
    category: str = "attraction"
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    description: str = ""
    opening_hours: str | None = None
    price_estimate_inr: float | None = None
    publishers: set[str] = field(default_factory=set)
    source_urls: set[str] = field(default_factory=set)
    excerpts: list[str] = field(default_factory=list)


def _normalize_place_name(name: str) -> str:
    """Normalizes place name for robust deduplication across websites."""
    cleaned = re.sub(r"[^\w\s]", " ", name.lower())
    stop_words = ("the", "a", "an", "and", "of", "in", "at", "by", "for", "with")
    tokens = [t for t in cleaned.split() if t not in stop_words]
    return " ".join(tokens).strip() or cleaned.strip()


def _is_same_place(
    name1: str,
    lat1: float | None,
    lng1: float | None,
    name2: str,
    lat2: float | None,
    lng2: float | None,
) -> bool:
    """Checks whether two place candidate sightings refer to the same physical venue."""
    n1 = _normalize_place_name(name1)
    n2 = _normalize_place_name(name2)
    if n1 == n2:
        return True

    # Check spatial proximity if coordinates are present (within ~0.003 degrees, approx 300m)
    if lat1 is not None and lat2 is not None and lng1 is not None and lng2 is not None:
        if abs(lat1 - lat2) < 0.003 and abs(lng1 - lng2) < 0.003:
            tokens1 = set(n1.split())
            tokens2 = set(n2.split())
            union = tokens1 | tokens2
            if union and (len(tokens1 & tokens2) / len(union)) >= 0.5:
                return True
            if (n1 in n2 or n2 in n1) and min(len(tokens1), len(tokens2)) >= 1:
                return True
    return False


class PlaceExtractionService:
    """Extracts, geolocates, and ranks candidate places from source observations."""

    @classmethod
    def extract_candidates_from_observations(
        cls,
        observations: list[Any],
        default_destination_lat: float | None = None,
        default_destination_lng: float | None = None,
    ) -> list[PlaceProposal]:
        """Extracts candidate places from observations and ranks them by multi-site consensus.

        Args:
            observations: List of SourceObservationModel or RawObservation instances.
            default_destination_lat: Optional fallback latitude for coordinate bounding.
            default_destination_lng: Optional fallback longitude for coordinate bounding.

        Returns:
            List of PlaceProposal domain contracts ordered by consensus appearance count.
        """
        accumulators: list[PlaceEvidenceAccumulator] = []

        for obs in observations:
            publisher = str(getattr(obs, "publisher", "") or "web").strip().lower()
            source_url = str(getattr(obs, "source_url", "") or "").strip()
            artifact_ref = getattr(obs, "artifact_ref", None) or {}
            excerpt_text = str(getattr(obs, "excerpt_text", "") or "").strip()

            # 1. Check local place result structured metadata from SerpApi with verified GPS coordinates
            if isinstance(artifact_ref, dict):
                gps = artifact_ref.get("gps_coordinates") or artifact_ref.get("gps") or {}
                raw_title = str(artifact_ref.get("title") or "").strip()
                if not raw_title and excerpt_text.startswith("Place: "):
                    raw_title = excerpt_text.split("|")[0].replace("Place: ", "").strip()

                if raw_title and isinstance(gps, dict) and gps.get("latitude") and gps.get("longitude"):
                    try:
                        lat = float(gps["latitude"])
                        lng = float(gps["longitude"])

                        # Find matching existing accumulator
                        matched_acc: PlaceEvidenceAccumulator | None = None
                        for existing in accumulators:
                            if _is_same_place(raw_title, lat, lng, existing.canonical_name, existing.latitude, existing.longitude):
                                matched_acc = existing
                                break

                        if matched_acc is None:
                            matched_acc = PlaceEvidenceAccumulator(
                                canonical_name=raw_title,
                                category=cls._infer_category(raw_title, excerpt_text),
                                latitude=lat,
                                longitude=lng,
                                address=artifact_ref.get("address"),
                                description=excerpt_text or f"Discovered via search on {publisher}",
                            )
                            accumulators.append(matched_acc)

                        matched_acc.publishers.add(publisher)
                        matched_acc.source_urls.add(source_url)
                        if excerpt_text:
                            matched_acc.excerpts.append(excerpt_text)
                        continue
                    except (ValueError, TypeError):
                        pass

            # 2. Check text mentions in scraped pages to record consensus sightings for geolocated places
            if excerpt_text and accumulators:
                excerpt_lower = excerpt_text.lower()
                for acc in accumulators:
                    acc_name_norm = _normalize_place_name(acc.canonical_name)
                    if acc_name_norm and (acc_name_norm in excerpt_lower or acc.canonical_name.lower() in excerpt_lower):
                        acc.publishers.add(publisher)
                        acc.source_urls.add(source_url)
                        acc.excerpts.append(excerpt_text)

        # 3. Consensus ranking by distinct publisher appearance count
        sorted_accumulators = sorted(
            accumulators,
            key=lambda a: (len(a.publishers), len(a.source_urls)),
            reverse=True,
        )

        proposals: list[PlaceProposal] = []
        for acc in sorted_accumulators:
            appearance_count = len(acc.publishers)
            # Base consensus score: minimum 0.50, scaling with number of independent websites
            fit_score = min(1.0, 0.40 + (appearance_count * 0.20))

            # Only accept places with valid, verified GPS coordinates
            if acc.latitude is None or acc.longitude is None:
                continue

            proposal = PlaceProposal(
                name=acc.canonical_name,
                category=acc.category,
                estimated_latitude=acc.latitude,
                estimated_longitude=acc.longitude,
                description=acc.description or f"Mentioned across {appearance_count} web sources.",
                fit_score=fit_score,
                suggested_stay_minutes=60,
                price_estimate_inr=acc.price_estimate_inr,
                uncertainty="EDITORIAL_DISCOVERY",
                is_candidate=True,
                opening_hours=acc.opening_hours,
                address=acc.address,
            )
            proposals.append(proposal)

        return proposals

    @classmethod
    def _infer_category(cls, title: str, text: str) -> str:
        """Infers domain category from title and text keywords."""
        combined = f"{title} {text}".lower()
        if any(w in combined for w in ("restaurant", "food", "cafe", "dhaba", "dining", "cuisine", "bakery", "sweet")):
            return "restaurant"
        if any(w in combined for w in ("street food", "chaat", "snack", "roll", "stall", "vendor")):
            return "street_food"
        if any(w in combined for w in ("hotel", "resort", "lodge", "homestay", "inn", "hostel")):
            return "lodging"
        if any(w in combined for w in ("fort", "palace", "museum", "temple", "monument", "memorial", "church", "heritage")):
            return "heritage"
        if any(w in combined for w in ("park", "lake", "falls", "mountain", "hill", "garden", "wildlife", "sanctuary")):
            return "nature"
        return "attraction"
