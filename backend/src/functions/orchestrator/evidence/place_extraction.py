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
    retrieval_times: list[str] = field(default_factory=list)
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
            retrieved_at = getattr(obs, "retrieved_at", None)
            retrieved_str = (
                retrieved_at.isoformat()
                if retrieved_at is not None and hasattr(retrieved_at, "isoformat")
                else None
            )

            # 1. Check local place result structured metadata from SerpApi with verified GPS coordinates
            if isinstance(artifact_ref, dict):
                gps = artifact_ref.get("gps_coordinates") or artifact_ref.get("gps") or {}
                raw_title = str(artifact_ref.get("title") or "").strip()
                if not raw_title and excerpt_text.startswith("Place: "):
                    raw_title = excerpt_text.split("|")[0].replace("Place: ", "").strip()

                if raw_title and cls.is_culinary_dish(raw_title):
                    # Culinary dish names without business venue keywords are not physical place candidates
                    continue

                if (
                    raw_title
                    and isinstance(gps, dict)
                    and gps.get("latitude")
                    and gps.get("longitude")
                ):
                    try:
                        lat = float(gps["latitude"])
                        lng = float(gps["longitude"])

                        # Find matching existing accumulator
                        matched_acc: PlaceEvidenceAccumulator | None = None
                        for existing in accumulators:
                            if _is_same_place(
                                raw_title,
                                lat,
                                lng,
                                existing.canonical_name,
                                existing.latitude,
                                existing.longitude,
                            ):
                                matched_acc = existing
                                break

                        price_val = (
                            artifact_ref.get("price_per_night")
                            or artifact_ref.get("indicative_price_inr")
                            or artifact_ref.get("price_estimate_inr")
                        )
                        if matched_acc is None:
                            matched_acc = PlaceEvidenceAccumulator(
                                canonical_name=raw_title,
                                category=cls._infer_category(raw_title, excerpt_text),
                                latitude=lat,
                                longitude=lng,
                                address=artifact_ref.get("address"),
                                description=excerpt_text or f"Discovered via search on {publisher}",
                                price_estimate_inr=int(price_val) if price_val is not None else None,
                            )
                            accumulators.append(matched_acc)
                        elif price_val is not None and matched_acc.price_estimate_inr is None:
                            try:
                                matched_acc.price_estimate_inr = int(price_val)
                            except (ValueError, TypeError):
                                pass

                        matched_acc.publishers.add(publisher)
                        matched_acc.source_urls.add(source_url)
                        if retrieved_str and retrieved_str not in matched_acc.retrieval_times:
                            matched_acc.retrieval_times.append(retrieved_str)
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
                    if acc_name_norm and (
                        acc_name_norm in excerpt_lower
                        or acc.canonical_name.lower() in excerpt_lower
                    ):
                        acc.publishers.add(publisher)
                        acc.source_urls.add(source_url)
                        if retrieved_str and retrieved_str not in acc.retrieval_times:
                            acc.retrieval_times.append(retrieved_str)
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
                source_urls=sorted(list(acc.source_urls)),
                retrieval_time=acc.retrieval_times[0] if acc.retrieval_times else None,
            )
            proposals.append(proposal)

        return proposals

    @classmethod
    def _infer_category(cls, title: str, text: str) -> str:
        """Infers domain category from title and text keywords."""
        combined = f"{title} {text}".lower()
        if any(
            w in combined
            for w in ("restaurant", "food", "cafe", "dhaba", "dining", "cuisine", "bakery", "sweet")
        ):
            return "restaurant"
        if any(w in combined for w in ("street food", "chaat", "snack", "roll", "stall", "vendor")):
            return "street_food"
        if any(w in combined for w in ("hotel", "resort", "lodge", "homestay", "inn", "hostel")):
            return "lodging"
        if any(
            w in combined
            for w in (
                "fort",
                "palace",
                "museum",
                "temple",
                "monument",
                "memorial",
                "church",
                "heritage",
            )
        ):
            return "heritage"
        if any(
            w in combined
            for w in (
                "park",
                "lake",
                "falls",
                "mountain",
                "hill",
                "garden",
                "wildlife",
                "sanctuary",
            )
        ):
            return "nature"
        return "attraction"

    @classmethod
    def is_culinary_dish(cls, name: str) -> bool:
        """Determines if a name represents a culinary dish rather than a physical business venue."""
        name_lower = name.lower().strip()
        # Physical venue indicators
        venue_indicators = (
            "restaurant",
            "cafe",
            "coffee",
            "dhaba",
            "bistro",
            "eatery",
            "kitchen",
            "canteen",
            "inn",
            "pub",
            "bar",
            "bhojanalaya",
            "stall",
            "corner",
            "center",
            "centre",
            "parlour",
            "parlor",
            "house",
            "shop",
            "hotel",
            "bakery",
            "cabin",
            "sweet shop",
            "bazaar",
            "joint",
            "outlet",
        )
        if any(v in name_lower for v in venue_indicators):
            return False

        dish_terms = (
            "kathi roll",
            "kati roll",
            "momo",
            "momos",
            "thukpa",
            "biryani",
            "rosogolla",
            "rasgulla",
            "sandesh",
            "sondesh",
            "mishti doi",
            "misti doi",
            "macher jhol",
            "kosha mangsho",
            "phuchka",
            "puchka",
            "pani puri",
            "panipuri",
            "luchi",
            "alur dom",
            "cholar dal",
            "shukto",
            "chelo kabab",
            "fish fry",
            "daab chingri",
            "ilish bhapa",
        )
        return any(
            name_lower == d or name_lower.endswith(f" {d}") or f"{d} " in name_lower
            for d in dish_terms
        )

    @classmethod
    def extract_local_dishes_from_observations(
        cls,
        observations: list[Any],
    ) -> list[dict[str, Any]]:
        """Extracts authentic regional culinary dishes from collected search and scraping observations."""
        dishes: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        regional_catalog = {
            "kathi roll": ("Kathi Roll", "street_food", "Skewer roasted kebab rolled in paratha flatbread"),
            "rosogolla": ("Rosogolla", "sweet", "Syrupy cottage cheese dumpling confection"),
            "rasgulla": ("Rosogolla", "sweet", "Syrupy cottage cheese dumpling confection"),
            "sandesh": ("Sandesh", "sweet", "Delicate artisanal milk fudge"),
            "sondesh": ("Sandesh", "sweet", "Delicate artisanal milk fudge"),
            "mishti doi": ("Mishti Doi", "sweet", "Caramelized fermented sweet curd"),
            "macher jhol": ("Macher Jhol", "main_course", "Traditional light mustard fish curry"),
            "kosha mangsho": ("Kosha Mangsho", "main_course", "Slow cooked rich spiced mutton curry"),
            "phuchka": ("Phuchka", "street_food", "Crisp hollow puri filled with spiced potato and tamarind water"),
            "puchka": ("Phuchka", "street_food", "Crisp hollow puri filled with spiced potato and tamarind water"),
            "momos": ("Momos", "street_food", "Steamed dumplings served with spicy sesame chutney"),
            "thukpa": ("Thukpa", "main_course", "Himalayan noodle soup with vegetables and herbs"),
            "luchi": ("Luchi Alur Dom", "breakfast", "Deep fried puffed flatbread with spiced potato curry"),
            "biryani": ("Kolkata Biryani", "main_course", "Fragrant saffron rice layered with meat and spiced potato"),
            "chelo kabab": ("Chelo Kabab", "heritage_dish", "Heritage Persian style buttered rice with grilled kebabs"),
            "daab chingri": ("Daab Chingri", "specialty", "Prawns cooked inside tender coconut with mustard paste"),
            "shukto": ("Shukto", "traditional", "Traditional bitter sweet mixed vegetable medley"),
        }

        for obs in observations:
            text = str(
                getattr(obs, "excerpt_text", None)
                or (obs.get("excerpt_text") if isinstance(obs, dict) else "")
                or ""
            ).lower()
            source_url = str(
                getattr(obs, "canonical_url", None)
                or getattr(obs, "source_url", None)
                or (obs.get("canonical_url") if isinstance(obs, dict) else "")
                or (obs.get("source_url") if isinstance(obs, dict) else "")
                or ""
            ).strip()
            retrieved_at = (
                getattr(obs, "retrieved_at", None)
                or (obs.get("retrieved_at") if isinstance(obs, dict) else None)
            )
            retrieved_str = (
                retrieved_at.isoformat()
                if retrieved_at is not None and hasattr(retrieved_at, "isoformat")
                else (str(retrieved_at) if retrieved_at is not None else None)
            )

            for key, (dish_name, dish_type, desc) in regional_catalog.items():
                if key in text and dish_name.lower() not in seen_names:
                    seen_names.add(dish_name.lower())
                    dishes.append(
                        {
                            "name": dish_name,
                            "dish_type": dish_type,
                            "description": desc,
                            "is_business_venue": False,
                            "source_url": source_url or None,
                            "retrieval_time": retrieved_str,
                            "uncertainty": "EDITORIAL_DISCOVERY",
                        }
                    )

        return dishes
