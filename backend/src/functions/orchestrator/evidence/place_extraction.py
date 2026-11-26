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


def extract_observation_purpose(artifact_ref: Any) -> str:
    """Extract normalized observation query purpose from artifact metadata."""
    if not isinstance(artifact_ref, dict):
        return ""
    return (
        str(artifact_ref.get("query_purpose") or artifact_ref.get("purpose") or "").lower().strip()
    )


def extract_observation_purposes(artifact_ref: Any) -> set[str]:
    """Extract all normalized query purposes (single, list, and purpose_destinations) from artifact metadata."""
    if not isinstance(artifact_ref, dict):
        return set()
    purposes: set[str] = set()
    primary = extract_observation_purpose(artifact_ref)
    if primary:
        purposes.add(primary)
    for key in ("purposes", "query_purposes"):
        raw_list = artifact_ref.get(key)
        if isinstance(raw_list, list):
            for item in raw_list:
                val = str(item or "").lower().strip()
                if val:
                    purposes.add(val)
    raw_pairs = artifact_ref.get("purpose_destinations")
    if isinstance(raw_pairs, list):
        for pair in raw_pairs:
            if isinstance(pair, dict):
                val = str(pair.get("purpose") or "").lower().strip()
                if val:
                    purposes.add(val)
    return purposes


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
        if lat1 is not None and lat2 is not None and lng1 is not None and lng2 is not None:
            return abs(lat1 - lat2) < 0.003 and abs(lng1 - lng2) < 0.003
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
        expected_cities: set[str] | list[str] | None = None,
    ) -> list[PlaceProposal]:
        """Extracts candidate places from observations and ranks them by multi-site consensus.

        Args:
            observations: List of SourceObservationModel or RawObservation instances.
            default_destination_lat: Optional fallback latitude for coordinate bounding.
            default_destination_lng: Optional fallback longitude for coordinate bounding.
            expected_cities: Optional set/list of expected destination city names to exclude conflicting cities.

        Returns:
            List of PlaceProposal domain contracts ordered by consensus appearance count.
        """
        accumulators: list[PlaceEvidenceAccumulator] = []
        norm_expected = (
            {c.strip().lower() for c in expected_cities if c and c.strip()}
            if expected_cities
            else None
        )

        for obs in observations:
            if norm_expected:
                from functions.orchestrator.evidence.claim_ingestion import _has_conflicting_city

                if _has_conflicting_city(obs, norm_expected):
                    continue
            publisher = (
                str(
                    (
                        obs.get("publisher")
                        if isinstance(obs, dict)
                        else getattr(obs, "publisher", "")
                    )
                    or "web"
                )
                .strip()
                .lower()
            )
            source_url = str(
                (obs.get("source_url") if isinstance(obs, dict) else getattr(obs, "source_url", ""))
                or ""
            ).strip()
            artifact_ref = (
                obs.get("artifact_ref")
                if isinstance(obs, dict)
                else getattr(obs, "artifact_ref", None)
            ) or {}
            excerpt_text = str(
                (
                    obs.get("excerpt_text")
                    if isinstance(obs, dict)
                    else getattr(obs, "excerpt_text", "")
                )
                or ""
            ).strip()
            retrieved_at = (
                obs.get("retrieved_at")
                if isinstance(obs, dict)
                else getattr(obs, "retrieved_at", None)
            )
            retrieved_str: str | None = None
            if isinstance(retrieved_at, str):
                retrieved_str = retrieved_at
            elif retrieved_at is not None and hasattr(retrieved_at, "isoformat"):
                val = retrieved_at.isoformat()
                if isinstance(val, str):
                    retrieved_str = val

            # 1. Check local place result structured metadata from SerpApi with verified GPS coordinates
            if isinstance(artifact_ref, dict):
                if "advisory" in extract_observation_purposes(artifact_ref):
                    # Advisory results must remain road disruptions/alerts and cannot become sightseeing candidates
                    continue

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
                                price_estimate_inr=int(price_val)
                                if price_val is not None
                                else None,
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
            "sweets",
            "mart",
            "bazaar",
            "joint",
            "outlet",
            "mess",
            "tiffin",
            "kitchen",
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
            "mysore pak",
            "mysuru pak",
            "masala dosa",
            "mysore masala dosa",
            "benne dosa",
            "set dosa",
            "neer dosa",
            "rava dosa",
            "thatte idli",
            "bisi bele bath",
            "bisibelebath",
            "maddur vada",
            "ragi mudde",
            "akki rotti",
            "akki roti",
            "kesari bath",
            "chow chow bath",
            "pandi curry",
            "mangalore buns",
            "jolada rotti",
            "filter coffee",
            "filter kaapi",
            "puliyogare",
            "chiroti",
        )
        return any(
            name_lower == d or name_lower.endswith(f" {d}") or f"{d} " in name_lower
            for d in dish_terms
        )

    @classmethod
    def extract_local_dishes_from_observations(
        cls,
        observations: list[Any],
        expected_cities: set[str] | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Extracts authentic regional culinary dishes from collected search and scraping observations.

        Requires each published dish to trace to an actual supporting observation.
        Does not assign regional variants or descriptions unsupported by the source.
        """
        import re

        dishes: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        norm_expected = (
            {c.strip().lower() for c in expected_cities if c and c.strip()}
            if expected_cities
            else None
        )

        dish_definitions: list[tuple[str, str, str]] = [
            ("kathi roll", "Kathi Roll", "street_food"),
            ("kati roll", "Kathi Roll", "street_food"),
            ("rosogolla", "Rosogolla", "sweet"),
            ("rasgulla", "Rosogolla", "sweet"),
            ("sandesh", "Sandesh", "sweet"),
            ("sondesh", "Sandesh", "sweet"),
            ("mishti doi", "Mishti Doi", "sweet"),
            ("misti doi", "Mishti Doi", "sweet"),
            ("macher jhol", "Macher Jhol", "main_course"),
            ("kosha mangsho", "Kosha Mangsho", "main_course"),
            ("phuchka", "Phuchka", "street_food"),
            ("puchka", "Phuchka", "street_food"),
            ("momos", "Momos", "street_food"),
            ("momo", "Momos", "street_food"),
            ("thukpa", "Thukpa", "main_course"),
            ("luchi", "Luchi", "breakfast"),
            ("alur dom", "Alur Dom", "side_dish"),
            ("cholar dal", "Cholar Dal", "side_dish"),
            ("biryani", "Biryani", "main_course"),
            ("chelo kabab", "Chelo Kabab", "specialty"),
            ("daab chingri", "Daab Chingri", "specialty"),
            ("shukto", "Shukto", "traditional"),
            ("fish fry", "Fish Fry", "snack"),
            ("ilish bhapa", "Ilish Bhapa", "specialty"),
            ("mysore pak", "Mysore Pak", "sweet"),
            ("mysuru pak", "Mysore Pak", "sweet"),
            ("mysore masala dosa", "Mysore Masala Dosa", "breakfast"),
            ("masala dosa", "Masala Dosa", "breakfast"),
            ("benne dosa", "Benne Dosa", "breakfast"),
            ("set dosa", "Set Dosa", "breakfast"),
            ("neer dosa", "Neer Dosa", "breakfast"),
            ("thatte idli", "Thatte Idli", "breakfast"),
            ("bisi bele bath", "Bisi Bele Bath", "main_course"),
            ("bisibelebath", "Bisi Bele Bath", "main_course"),
            ("maddur vada", "Maddur Vada", "snack"),
            ("ragi mudde", "Ragi Mudde", "main_course"),
            ("akki rotti", "Akki Rotti", "breakfast"),
            ("akki roti", "Akki Rotti", "breakfast"),
            ("kesari bath", "Kesari Bath", "sweet"),
            ("chow chow bath", "Chow Chow Bath", "breakfast"),
            ("pandi curry", "Pandi Curry", "main_course"),
            ("mangalore buns", "Mangalore Buns", "breakfast"),
            ("jolada rotti", "Jolada Rotti", "main_course"),
            ("filter coffee", "Filter Coffee", "beverage"),
            ("filter kaapi", "Filter Coffee", "beverage"),
        ]

        for obs in observations:
            if norm_expected:
                from functions.orchestrator.evidence.claim_ingestion import _has_conflicting_city

                if _has_conflicting_city(obs, norm_expected):
                    continue

            obs_id = (
                str(getattr(obs, "id", None) or (obs.get("id") if isinstance(obs, dict) else ""))
                or None
            )
            raw_excerpt = str(
                getattr(obs, "excerpt_text", None)
                or (obs.get("excerpt_text") if isinstance(obs, dict) else "")
                or ""
            )
            if not raw_excerpt.strip():
                continue

            text_lower = raw_excerpt.lower()
            source_url = str(
                getattr(obs, "canonical_url", None)
                or getattr(obs, "source_url", None)
                or (obs.get("canonical_url") if isinstance(obs, dict) else "")
                or (obs.get("source_url") if isinstance(obs, dict) else "")
                or ""
            ).strip()
            if not source_url:
                continue

            retrieved_at = getattr(obs, "retrieved_at", None) or (
                obs.get("retrieved_at") if isinstance(obs, dict) else None
            )
            retrieved_str: str | None = None
            if isinstance(retrieved_at, str):
                retrieved_str = retrieved_at
            elif retrieved_at is not None and hasattr(retrieved_at, "isoformat"):
                val = retrieved_at.isoformat()
                if isinstance(val, str):
                    retrieved_str = val

            sentences = [s.strip() for s in re.split(r"[.\n;]", raw_excerpt) if s.strip()]

            for term, base_name, dish_type in dish_definitions:
                if term not in text_lower:
                    continue

                dish_name = base_name
                if f"kolkata {term}" in text_lower:
                    dish_name = f"Kolkata {base_name}"
                elif f"hyderabadi {term}" in text_lower:
                    dish_name = f"Hyderabadi {base_name}"
                elif f"lucknowi {term}" in text_lower:
                    dish_name = f"Lucknowi {base_name}"
                elif f"mysore {term}" in text_lower or f"mysuru {term}" in text_lower:
                    dish_name = f"Mysore {base_name}"
                elif term == "luchi" and (
                    "luchi alur dom" in text_lower or "luchi aloo dum" in text_lower
                ):
                    dish_name = "Luchi Alur Dom"

                if dish_name.lower() in seen_names:
                    continue

                supporting_description: str | None = None
                for sent in sentences:
                    if term in sent.lower():
                        if len(sent) > len(dish_name) + 5:
                            supporting_description = sent
                        break

                seen_names.add(dish_name.lower())
                dishes.append(
                    {
                        "name": dish_name,
                        "dish_type": dish_type,
                        "description": supporting_description,
                        "is_business_venue": False,
                        "source_url": source_url,
                        "observation_id": obs_id,
                        "retrieval_time": retrieved_str,
                        "uncertainty": "EDITORIAL_DISCOVERY",
                        "paired_venues": [],
                    }
                )

        return dishes

    @classmethod
    def pair_dishes_and_venues(
        cls,
        venues: list[dict[str, Any]],
        dishes: list[dict[str, Any]],
        observations: list[Any],
        expected_cities: set[str] | list[str] | None = None,
    ) -> None:
        """Pairs dining venues and regional dishes strictly when supported by attributable source content."""
        if not venues or not dishes:
            return

        for v in venues:
            if isinstance(v, dict) and "serves_dishes" not in v:
                v["serves_dishes"] = []
        for d in dishes:
            if isinstance(d, dict) and "paired_venues" not in d:
                d["paired_venues"] = []

        if not observations:
            return

        norm_expected: set[str] | None = None
        if expected_cities:
            norm_expected = {c.strip().lower() for c in expected_cities if c and c.strip()}

        from functions.orchestrator.evidence.claim_ingestion import (
            KNOWN_CONFLICTING_CITIES,
            SNIPPET_SOURCE_METHODS,
            _has_conflicting_city,
            _obs_attr,
        )

        negation_markers = (
            "does not",
            "doesn't",
            "do not",
            "don't",
            "not ",
            "no longer",
            "never ",
        )
        historical_markers = (
            "used to ",
            "formerly ",
            "previously ",
            "once served",
            "no longer ",
        )
        loc_forbidden_tokens = frozenset(
            {
                "that",
                "which",
                "who",
                "where",
                "recommends",
                "recommend",
                "suggests",
                "suggest",
                "mentions",
                "mention",
                "serves",
                "serve",
                "not",
                "never",
                "used",
                "formerly",
                "previously",
                "is",
                "are",
                "was",
                "near",
                "beside",
                "opposite",
                "across",
                "stopped",
                "ceased",
            }
        )
        referral_cues = (
            "ask ",
            "next door",
            "shop",
            "across",
            "outside",
            "nearby",
            "another ",
        )
        status_notice_markers = (
            "sold out",
            "out of stock",
            "unavailable",
            "not available",
            "discontinued",
        )

        for obs in observations:
            method = str(_obs_attr(obs, "extraction_method") or "").strip().upper()
            if method in SNIPPET_SOURCE_METHODS:
                continue
            if norm_expected:
                if _has_conflicting_city(obs, norm_expected):
                    continue
                art_ref = _obs_attr(obs, "artifact_ref") or {}
                art_dict = art_ref if isinstance(art_ref, dict) else {}
                obs_addr = str(art_dict.get("address") or art_dict.get("location") or "").lower()
                if obs_addr and not any(
                    re.search(rf"\b{re.escape(exp)}\b", obs_addr) for exp in norm_expected
                ):
                    if any(
                        re.search(rf"\b{re.escape(c)}\b", obs_addr)
                        for c in (KNOWN_CONFLICTING_CITIES - norm_expected)
                    ):
                        continue

            raw_excerpt = str(_obs_attr(obs, "excerpt_text") or "").strip()
            if not raw_excerpt:
                continue
            obs_url = str(
                _obs_attr(obs, "canonical_url")
                or _obs_attr(obs, "source_url")
                or ""
            ).strip()
            if not obs_url.startswith(("http://", "https://")):
                continue

            raw_lines = raw_excerpt.splitlines()
            sentences = [s.strip() for s in re.split(r"[.\n;!?]+", raw_excerpt) if s.strip()]
            norm_venue_counts: dict[str, int] = {}
            for v in venues:
                if isinstance(v, dict) and v.get("name"):
                    vn = _normalize_place_name(str(v["name"])).lower()
                    if vn:
                        norm_venue_counts[vn] = norm_venue_counts.get(vn, 0) + 1

            for venue in venues:
                if not isinstance(venue, dict):
                    continue
                v_name = str(venue.get("name") or "").strip().lower()
                v_norm = _normalize_place_name(venue.get("name") or "").lower()
                if not v_name and not v_norm:
                    continue

                if "serves_dishes" not in venue or not isinstance(venue["serves_dishes"], list):
                    venue["serves_dishes"] = []
                if "dish_pairings" not in venue or not isinstance(venue["dish_pairings"], list):
                    venue["dish_pairings"] = []

                for dish in dishes:
                    if not isinstance(dish, dict):
                        continue
                    if "paired_venues" not in dish or not isinstance(dish["paired_venues"], list):
                        dish["paired_venues"] = []
                    if "venue_pairings" not in dish or not isinstance(dish["venue_pairings"], list):
                        dish["venue_pairings"] = []

                    d_name = str(dish.get("name") or "").strip().lower()
                    if not d_name:
                        continue
                    d_words = d_name.split()
                    d_fallback = " ".join(d_words[-2:]) if len(d_words) >= 2 else None

                    matched_excerpt: str | None = None

                    # 1. Check direct serving statements: <Venue> [in <loc>] <serving_verb> [adj] <Dish>
                    v_terms = list(dict.fromkeys(t for t in (v_name, v_norm) if t))
                    d_terms = list(dict.fromkeys(t for t in (d_name, d_fallback) if t))
                    verb_pat = (
                        r"(?:serves|serve|serving|offers|offer|"
                        r"is\s+(?:the\s+)?birthplace\s+of|"
                        r"is\s+(?:famous|known|renowned)\s+for|"
                        r"specializes\s+in|specialises\s+in)"
                    )
                    adj_pat = r"(?:(?:authentic|traditional|soft|pure\s+ghee|fresh|homemade|hot|crispy|famous|legendary)\s+)*"
                    for sent in sentences:
                        clauses = [
                            c.strip()
                            for c in re.split(
                                r"(?:,|\bwhile\b|\bwhereas\b|\bunlike\b|\bbut\b)",
                                sent,
                                flags=re.IGNORECASE,
                            )
                            if c.strip()
                        ]
                        for clause in clauses:
                            clause_lower = clause.lower()
                            if any(neg in clause_lower for neg in negation_markers):
                                continue
                            if any(hist in clause_lower for hist in historical_markers):
                                continue
                            for vt in v_terms:
                                for dt in d_terms:
                                    direct_pat = (
                                        rf"^(?:original\s+)?{re.escape(vt)}"
                                        rf"(?:\s+(?:in|at|on)\s+(?P<loc>[a-z0-9\s]+?))?\s+"
                                        rf"{verb_pat}\s+{adj_pat}{re.escape(dt)}"
                                        rf"(?:\s+(?:sweet|dish))?$"
                                    )
                                    m = re.match(direct_pat, clause_lower)
                                    if m:
                                        loc_span = (m.group("loc") or "").strip()
                                        if loc_span and any(
                                            tok in loc_forbidden_tokens for tok in loc_span.split()
                                        ):
                                            continue
                                        matched_excerpt = sent[:200]
                                        break
                                if matched_excerpt is not None:
                                    break
                            if matched_excerpt is not None:
                                break
                        if matched_excerpt is not None:
                            break

                    # 2. Check genuine menu items scoped under the matched venue's menu heading
                    sold_out_detected = False
                    if len(raw_lines) > 1:
                        active_heading: str | None = None
                        for raw_ln in raw_lines:
                            ln = raw_ln.strip()
                            if not ln:
                                active_heading = None
                                continue
                            ln_lower = ln.lower()
                            if "menu" in ln_lower:
                                heading_clean = re.sub(r"\([^)]*\)", "", ln_lower).strip().rstrip(":").strip()
                                heading_clean = re.sub(
                                    r"\s+(?:near|beside|opposite|across\s+from|recommended\s+by|similar\s+to|like)\s+.+$",
                                    "",
                                    heading_clean,
                                ).strip()
                                owner_match = re.match(
                                    r"^(?P<owner>.+?)(?:'s|’s)?\s*(?:[-–—:]\s*)?menu$|"
                                    r"^menu\s+(?:of|at)\s+(?P<owner2>.+?)$|"
                                    r"^menu\s*[-–—:]\s*(?P<owner3>.+?)$",
                                    heading_clean,
                                )
                                menu_owner = (
                                    (
                                        owner_match.group("owner")
                                        or owner_match.group("owner2")
                                        or owner_match.group("owner3")
                                        or ""
                                    ).strip()
                                    if owner_match
                                    else ""
                                )
                                menu_owner_norm = _normalize_place_name(menu_owner).lower() if menu_owner else ""
                                is_ambiguous = bool(
                                    menu_owner_norm and norm_venue_counts.get(menu_owner_norm, 0) > 1
                                )
                                owner_matches_venue = bool(
                                    menu_owner
                                    and not is_ambiguous
                                    and any(
                                        menu_owner == vt or (menu_owner_norm and menu_owner_norm == vt)
                                        for vt in v_terms
                                    )
                                )
                                active_heading = ln if owner_matches_venue else None
                                continue
                            if ln.endswith(":"):
                                active_heading = None
                                continue

                            if active_heading is not None:
                                d_in_ln = d_name in ln_lower or (
                                    d_fallback is not None and d_fallback in ln_lower
                                )
                                if not d_in_ln:
                                    continue
                                is_status = any(st in ln_lower for st in status_notice_markers)
                                if not is_status and any(
                                    neg in ln_lower for neg in negation_markers
                                ):
                                    continue
                                item_match = re.match(r"^(?:[-*•]\s+|\d+[.)]\s+)(.+)$", ln_lower)
                                if not item_match:
                                    if is_status:
                                        sold_out_detected = True
                                    continue
                                item_body = item_match.group(1).strip()
                                parts = re.split(r"\s*[(|\-–—:]\s*", item_body, maxsplit=1)
                                item_name = parts[0].strip()
                                item_suffix = parts[1].strip() if len(parts) > 1 else ""
                                if item_suffix and any(cue in item_suffix for cue in referral_cues):
                                    continue
                                if any(
                                    re.match(rf"^{adj_pat}{re.escape(dt)}$", item_name)
                                    for dt in d_terms
                                ):
                                    if is_status:
                                        sold_out_detected = True
                                    if matched_excerpt is None:
                                        matched_excerpt = f"{active_heading} {ln}"[:200]

                    if matched_excerpt is not None:
                        availability = "SOLD_OUT" if sold_out_detected else "UNKNOWN"
                        if dish["name"] not in venue["serves_dishes"]:
                            venue["serves_dishes"].append(dish["name"])
                            venue["dish_pairings"].append(
                                {
                                    "dish": dish["name"],
                                    "source_url": obs_url,
                                    "evidence_excerpt": matched_excerpt,
                                    "availability": availability,
                                }
                            )
                        if venue["name"] not in dish["paired_venues"]:
                            dish["paired_venues"].append(venue["name"])
                            dish["venue_pairings"].append(
                                {
                                    "venue": venue["name"],
                                    "source_url": obs_url,
                                    "evidence_excerpt": matched_excerpt,
                                    "availability": availability,
                                }
                            )

    @classmethod
    def is_listicle_title(cls, title: str) -> bool:
        """Determines whether a title is a listicle/roundup headline rather than a distinct place."""
        t = title.strip().lower()
        if not t:
            return False
        listicle_pat = (
            r"^(?:\d+|top\s+\d+|best\s+\d+|best\s+(?:hidden|offbeat|secret|unexplored)|"
            r"(?:hidden|secret|unexplored|offbeat)\s+(?:gems|places|spots)|places\s+to\s+visit|"
            r"things\s+to\s+do|guide\s+to)\b"
        )
        return bool(re.match(listicle_pat, t))

    @classmethod
    def evaluate_hidden_place_candidates(
        cls,
        observations: list[Any],
        expected_cities: set[str] | list[str] | None = None,
        excluded_names: set[str] | list[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Evaluates hidden-place candidates by merging observations per normalized place into
        verified optional stops, unverified candidates (with missing_evidence), and restricted/closed places."""
        from functions.orchestrator.evidence.claim_ingestion import (
            KNOWN_CONFLICTING_CITIES,
            SNIPPET_SOURCE_METHODS,
            _extract_coordinates_from_dict,
            _has_conflicting_city,
            _obs_attr,
        )

        norm_expected = (
            {c.strip().lower() for c in expected_cities if c and c.strip()}
            if expected_cities
            else set()
        )
        norm_excluded = (
            {_normalize_place_name(n).lower() for n in excluded_names if n and n.strip()}
            if excluded_names
            else set()
        )
        restricted_markers = (
            "private property",
            "privately owned",
            "no public access",
            "no public entry",
            "not open to the public",
            "not open to visitors",
            "restricted access",
            "permit required",
            "requires a special permit",
            "special permit",
            "permit only",
            "with permit",
            "by permit",
            "prior permission required",
            "closed to the public",
            "permanently closed",
            "temporarily closed",
            "entry prohibited",
            "access is prohibited",
            "trespassing",
        )
        public_access_markers = (
            "open to the public",
            "open to visitors",
            "public access",
            "visitors are welcome",
            "visitor entry",
            "public park",
            "public garden",
            "open daily",
            "free entry for visitors",
            "visitors can explore",
            "accessible to the public",
            "publicly accessible",
            "self-guided tours",
        )
        disqualifying_access_words = (
            "not ",
            "no ",
            "never ",
            "permit",
            "prohibited",
            "restricted",
            "closed",
            "private",
        )

        # Merge observations per normalized place name
        grouped: dict[str, dict[str, Any]] = {}

        for obs in observations:
            if norm_expected:
                if _has_conflicting_city(obs, norm_expected):
                    continue
                art_ref = _obs_attr(obs, "artifact_ref") or {}
                art_dict = art_ref if isinstance(art_ref, dict) else {}
                obs_addr = str(art_dict.get("address") or art_dict.get("location") or "").lower()
                if obs_addr and not any(
                    re.search(rf"\b{re.escape(exp)}\b", obs_addr) for exp in norm_expected
                ):
                    if any(
                        re.search(rf"\b{re.escape(c)}\b", obs_addr)
                        for c in (KNOWN_CONFLICTING_CITIES - norm_expected)
                    ):
                        continue

            art_ref = _obs_attr(obs, "artifact_ref") or {}
            art_dict = art_ref if isinstance(art_ref, dict) else {}
            purposes = extract_observation_purposes(art_dict)
            if "advisory" in purposes:
                continue
            if "hidden_places" not in purposes and bool(purposes & {"dining", "lodging"}):
                continue

            status_val = str(art_dict.get("status") or "").strip().upper()
            if status_val in ("UNAVAILABLE", "BLOCKED", "ERROR", "FAILED"):
                continue

            raw_title = str(
                art_dict.get("title")
                or art_dict.get("place_name")
                or art_dict.get("name")
                or ""
            ).strip()
            if not raw_title or cls.is_listicle_title(raw_title) or cls.is_culinary_dish(raw_title):
                continue

            norm_name = _normalize_place_name(raw_title).lower()
            if not norm_name or norm_name in norm_excluded:
                continue

            method = str(_obs_attr(obs, "extraction_method") or "").strip().upper()
            is_fetched = bool(
                method and method not in SNIPPET_SOURCE_METHODS and method != "NEEDS_BROWSER"
            )
            obs_url = str(
                _obs_attr(obs, "canonical_url") or _obs_attr(obs, "source_url") or ""
            ).strip()
            raw_excerpt = str(_obs_attr(obs, "excerpt_text") or "").strip()
            address = str(art_dict.get("address") or "").strip()

            coords_tuple = _extract_coordinates_from_dict(art_dict)
            coords: dict[str, float] | None = (
                {"lat": coords_tuple[0], "lng": coords_tuple[1]}
                if coords_tuple is not None and coords_tuple != (0.0, 0.0)
                else None
            )

            entry = grouped.setdefault(
                norm_name,
                {
                    "name": raw_title,
                    "address": address or None,
                    "coordinates": coords,
                    "source_urls": [],
                    "fetched_excerpts": [],
                    "snippet_excerpts": [],
                    "dest_tied": False,
                },
            )
            if not entry["address"] and address:
                entry["address"] = address
            if entry["coordinates"] is None and coords is not None:
                entry["coordinates"] = coords
            if obs_url.startswith(("http://", "https://")) and obs_url not in entry["source_urls"]:
                entry["source_urls"].append(obs_url)
            if raw_excerpt:
                if is_fetched:
                    entry["fetched_excerpts"].append(raw_excerpt)
                else:
                    entry["snippet_excerpts"].append(raw_excerpt)
            if not norm_expected:
                entry["dest_tied"] = True
            else:
                combined_loc = f"{raw_title} {address} {raw_excerpt}".lower()
                if any(re.search(rf"\b{re.escape(exp)}\b", combined_loc) for exp in norm_expected):
                    entry["dest_tied"] = True

        verified_optional_stops: list[dict[str, Any]] = []
        unverified_candidates: list[dict[str, Any]] = []
        restricted_or_closed: list[dict[str, Any]] = []

        for entry in grouped.values():
            fetched_text = " ".join(entry["fetched_excerpts"])
            fetched_lower = fetched_text.lower()
            has_fetched_content = len(entry["fetched_excerpts"]) > 0
            best_excerpt = (
                entry["fetched_excerpts"][0]
                if entry["fetched_excerpts"]
                else (entry["snippet_excerpts"][0] if entry["snippet_excerpts"] else "")
            )

            negated_restriction_pat = (
                r"\b(?:no|not|without|never|don't|doesn't|does\s+not|do\s+not|need\s+not|none)\s+"
                r"(?:a\s+|any\s+|special\s+)?$"
            )
            is_restricted = False
            if has_fetched_content:
                for rm in restricted_markers:
                    for rm_m in re.finditer(rf"\b{re.escape(rm)}\b", fetched_lower):
                        prefix = fetched_lower[max(0, rm_m.start() - 32) : rm_m.start()]
                        if any(sep in prefix for sep in (".", ";", "\n", "!")):
                            last_sep = max(
                                prefix.rfind("."),
                                prefix.rfind(";"),
                                prefix.rfind("\n"),
                                prefix.rfind("!"),
                            )
                            prefix = prefix[last_sep + 1 :]
                        if re.search(negated_restriction_pat, prefix):
                            continue
                        is_restricted = True
                        break
                    if is_restricted:
                        break

            has_public_access = False
            if has_fetched_content and not is_restricted:
                cleaned_for_access = re.sub(
                    r"\b(?:no|without)\s+(?:a\s+|any\s+|special\s+)?(?:permit|prior\s+permission)(?:\s+(?:is\s+)?(?:required|needed))?\b",
                    " ",
                    fetched_lower,
                )
                for sent in re.split(r"[.\n;!?]+", cleaned_for_access):
                    if any(pm in sent for pm in public_access_markers):
                        if not any(
                            re.search(rf"\b{re.escape(dw.strip())}\b", sent)
                            for dw in disqualifying_access_words
                        ):
                            has_public_access = True
                            break

            base_entry: dict[str, Any] = {
                "name": entry["name"],
                "address": entry["address"],
                "coordinates": entry["coordinates"],
                "source_urls": entry["source_urls"],
                "supporting_excerpt": best_excerpt[:200] if best_excerpt else None,
                "is_must_visit": False,
                "opening_hours": None,
                "entry_fee_inr": None,
            }

            if is_restricted:
                restricted_or_closed.append(
                    {
                        **base_entry,
                        "access_status": "RESTRICTED_OR_CLOSED",
                        "eligible_for_itinerary": False,
                        "missing_evidence": [],
                    }
                )
                continue

            missing: list[str] = []
            if not has_fetched_content:
                missing.append("fetched_page_content")
            if not entry["dest_tied"]:
                missing.append("destination_match")
            if entry["coordinates"] is None:
                missing.append("routable_coordinates")
            if not entry["source_urls"]:
                missing.append("source_url")
            if not has_public_access:
                missing.append("public_visitor_access")

            if not missing:
                verified_optional_stops.append(
                    {
                        **base_entry,
                        "access_status": "PUBLIC_ACCESS_VERIFIED",
                        "eligible_for_itinerary": True,
                        "missing_evidence": [],
                    }
                )
            else:
                unverified_candidates.append(
                    {
                        **base_entry,
                        "access_status": "UNVERIFIED",
                        "eligible_for_itinerary": False,
                        "missing_evidence": missing,
                    }
                )

        return {
            "verified_optional_stops": verified_optional_stops,
            "unverified_candidates": unverified_candidates,
            "restricted_or_closed": restricted_or_closed,
        }
