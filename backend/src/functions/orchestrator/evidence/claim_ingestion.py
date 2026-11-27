"""Claim ingestion service deriving structured claims from collected source observations.

Conforms to spec 0007 AC-13, AC-14:
1. Ingests collected SourceObservationModel records for an execution.
2. Identifies matching candidate places and submits claims via ClaimVerificationService.
3. Links observations to claims with support_type == SUPPORTS.
4. Preserves INDICATIVE_SEARCH classification for accommodation extractions, never promoting to LIVE_OFFER.
5. Operates idempotently across multiple invocations.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.candidate import RegisteredCandidate
from contracts.evidence import EvidenceClass
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.hours_extraction import (
    extract_structured_hours_from_text,
    extract_structured_hours_with_llm,
)
from functions.orchestrator.evidence.models import ClaimModel
from functions.orchestrator.evidence.place_extraction import (
    extract_observation_purposes,
)
from functions.orchestrator.evidence.schemas import (
    ClaimCategory,
    ClaimCreate,
    EvidenceSupportType,
)
from functions.orchestrator.evidence.service import ClaimVerificationService
from functions.orchestrator.places.corridor import haversine_distance_km

logger = logging.getLogger(__name__)

CASUAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:walked|walking|drove|driving|passed|passing|pass(?:es)?)\s+(?:by|past)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:on\s+our\s+way\s+to|en\s*route\s+to|heading\s+(?:to|towards))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:stopped\s+briefly\s+near|brief\s+stop\s+near)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:generic\s+article|no\s+verified\s+facts)\b",
        re.IGNORECASE,
    ),
)

CLOSED_OR_UNAVAILABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:permanently|temporarily)\s+closed\b", re.IGNORECASE),
    re.compile(r"\bclosed\s+(?:permanently|temporarily|down)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:no\s+longer\s+(?:open|in\s+operation|operating)|ceased\s+operations|shut\s+down)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:defunct|demolished|abandoned|not\s+operational)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bunder\s+(?:renovation|construction)\s+and\s+closed\b",
        re.IGNORECASE,
    ),
)

DISTINCT_ENTITY_MODIFIERS: frozenset[str] = frozenset(
    {
        "cafe",
        "canteen",
        "restaurant",
        "food",
        "bar",
        "pub",
        "dhaba",
        "mess",
        "hotel",
        "hotels",
        "resort",
        "resorts",
        "cafes",
        "restaurants",
        "lodge",
        "motel",
        "hostel",
        "inn",
        "stay",
        "guest",
        "house",
        "shop",
        "store",
        "bookstore",
        "counter",
        "gift",
        "stall",
        "kiosk",
        "mart",
        "bazaar",
        "market",
        "bus",
        "stand",
        "stop",
        "station",
        "terminus",
        "terminal",
        "metro",
        "railway",
        "train",
        "airport",
        "depot",
        "school",
        "college",
        "university",
        "institute",
        "academy",
        "class",
        "classes",
        "hospital",
        "clinic",
        "dispensary",
        "pharmacy",
        "medical",
        "nursing",
        "police",
        "outpost",
        "thana",
        "post",
        "office",
        "bank",
        "atm",
        "branch",
        "parking",
        "garage",
        "gate",
        "quarters",
        "colony",
        "society",
        "club",
        "association",
        "complex",
        "auditorium",
        "library",
    }
)

ALLOWED_MONUMENT_DESCRIPTORS: frozenset[str] = frozenset(
    {
        "hall",
        "memorial",
        "monument",
        "temple",
        "math",
        "museum",
        "palace",
        "fort",
        "church",
        "cathedral",
        "mosque",
        "masjid",
        "gurdwara",
    }
)


def _is_casual_mention(text: str) -> bool:
    """Detects whether text is merely a casual passing mention."""
    if not text:
        return False
    return any(p.search(text) for p in CASUAL_PATTERNS)


def _is_place_closed_or_unavailable(obs: SourceObservationModel | dict[str, Any]) -> bool:
    """Detects whether observation indicates the place is permanently closed or unavailable."""
    art_ref = _obs_attr(obs, "artifact_ref")
    if isinstance(art_ref, dict):
        bus_status = str(art_ref.get("business_status") or "").upper()
        if bus_status in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY", "INACTIVE"):
            return True
        op_status = str(art_ref.get("operating_status") or "").upper()
        if op_status in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY", "INACTIVE"):
            return True
        status_val = str(art_ref.get("status") or "").upper()
        if status_val in (
            "CLOSED",
            "PERMANENTLY_CLOSED",
            "TEMPORARILY_CLOSED",
            "DEFUNCT",
            "INACTIVE",
            "UNAVAILABLE",
        ):
            return True
        if art_ref.get("permanently_closed") is True or art_ref.get("is_closed") is True:
            return True

    excerpt = _obs_attr(obs, "excerpt_text") or ""
    if any(p.search(excerpt) for p in CLOSED_OR_UNAVAILABLE_PATTERNS):
        return True

    return False


def _tokens_match_ignoring_inflection(t1: list[str], t2: list[str]) -> bool:
    """Matches two token sequences allowing only minor grammatical inflections like plural s."""
    if len(t1) != len(t2):
        return False
    for a, b in zip(t1, t2, strict=True):
        if a == b:
            continue
        if a.rstrip("s") == b.rstrip("s"):
            continue
        return False
    return True


def _normalize_name(name: str) -> str:
    """Normalizes place name by stripping punctuation and common stop words."""
    cleaned = re.sub(r"[^\w\s]", " ", name.lower())
    stop_words = {"the", "a", "an", "and", "of", "in", "at", "by", "for", "with"}
    tokens = [t for t in cleaned.split() if t not in stop_words]
    return " ".join(tokens).strip() or cleaned.strip()


def _extract_coordinates_from_dict(data: dict[str, Any] | None) -> tuple[float, float] | None:
    """Safely extracts latitude and longitude from dictionary if present.

    Inspects top-level lat/lng and nested structures: gps_coordinates, gps, location, geo, point.
    """
    if not data or not isinstance(data, dict):
        return None

    # 1. Direct lat/lng keys
    for lat_key, lng_key in (("lat", "lng"), ("latitude", "longitude")):
        if lat_key in data and lng_key in data:
            try:
                if isinstance(data[lat_key], bool) or isinstance(data[lng_key], bool):
                    continue
                lat = float(data[lat_key])
                lng = float(data[lng_key])
                if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
                    return lat, lng
            except (ValueError, TypeError):
                pass

    # 2. Nested dictionary structures
    for key in ("gps_coordinates", "gps", "location", "geo", "point"):
        nested = data.get(key)
        if isinstance(nested, dict):
            coords = _extract_coordinates_from_dict(nested)
            if coords is not None:
                return coords

    # 3. Coordinate sequences: [lat, lng] or [lng, lat]
    for key in ("coordinates", "coords"):
        seq = data.get(key)
        if isinstance(seq, (list, tuple)) and len(seq) >= 2:
            try:
                if isinstance(seq[0], bool) or isinstance(seq[1], bool):
                    continue
                c0 = float(seq[0])
                c1 = float(seq[1])
                if -90.0 <= c0 <= 90.0 and -180.0 <= c1 <= 180.0:
                    return c0, c1
                if -90.0 <= c1 <= 90.0 and -180.0 <= c0 <= 180.0:
                    return c1, c0
            except (ValueError, TypeError):
                pass

    return None


def _extract_observation_coordinates(obs: SourceObservationModel) -> tuple[float, float] | None:
    """Extracts verified GPS coordinates from observation artifact_ref or geographic_scope."""
    art_ref = getattr(obs, "artifact_ref", None)
    if isinstance(art_ref, dict):
        coords = _extract_coordinates_from_dict(art_ref)
        if coords is not None:
            return coords
    geo = getattr(obs, "geographic_scope", None)
    if isinstance(geo, dict):
        coords = _extract_coordinates_from_dict(geo)
        if coords is not None:
            return coords
    return None


KNOWN_CONFLICTING_CITIES: frozenset[str] = frozenset(
    {
        "kolkata",
        "calcutta",
        "howrah",
        "west bengal",
        "bengaluru",
        "bangalore",
        "chennai",
        "mumbai",
        "delhi",
        "new delhi",
        "hyderabad",
        "pune",
        "jaipur",
        "ahmedabad",
        "kochi",
        "cochin",
        "thiruvananthapuram",
        "shimla",
        "manali",
        "darjeeling",
        "gangtok",
        "goa",
        "panaji",
        "varanasi",
        "lucknow",
        "patna",
        "bhubaneswar",
        "guwahati",
        "shillong",
        "srinagar",
        "leh",
        "ladakh",
        "udaipur",
        "jodhpur",
        "agra",
        "amritsar",
        "chandigarh",
        "dehradun",
        "rishikesh",
        "haridwar",
        "munnar",
        "alleppey",
        "madurai",
        "coimbatore",
        "kodaikanal",
        "mysuru",
        "mysore",
        "madikeri",
        "coorg",
        "puducherry",
        "pondicherry",
        "tirupati",
        "visakhapatnam",
        "vizag",
        "raniganj",
        "asansol",
        "durgapur",
        "ooty",
        "udhagamandalam",
    }
)


def _obs_attr(obs: SourceObservationModel | dict[str, Any], field: str, default: Any = None) -> Any:
    if isinstance(obs, dict):
        return obs.get(field, default)
    return getattr(obs, field, default)


ALLOWED_VENUE_INFO_TOKENS: frozenset[str] = ALLOWED_MONUMENT_DESCRIPTORS | frozenset(
    {
        "timings",
        "timing",
        "hours",
        "hour",
        "opening",
        "visiting",
        "visit",
        "official",
        "website",
        "ticket",
        "tickets",
        "ticketing",
        "entry",
        "fee",
        "fees",
        "guide",
        "tourism",
        "tourist",
        "attractions",
        "attraction",
        "places",
        "welcome",
        "home",
        "info",
        "information",
        "portal",
        "india",
    }
)

SNIPPET_SOURCE_METHODS: frozenset[str] = frozenset(
    {"SERPAPI_SEARCH", "SERPAPI_WEB_SEARCH", "SERPAPI_LOCAL_SEARCH", "SEARCH_SNIPPET", "SEARCH_RESULT"}
)


def _has_conflicting_city(
    obs: SourceObservationModel | dict[str, Any],
    expected_cities: set[str] | None,
) -> bool:
    """Rejects observations whose explicit city or address points outside expected destination cities."""
    if not expected_cities:
        return False
    norm_expected = {c.strip().lower() for c in expected_cities if c and c.strip()}
    if not norm_expected:
        return False

    art_ref = _obs_attr(obs, "artifact_ref")
    art_dict = art_ref if isinstance(art_ref, dict) else {}
    city_hint = str(art_dict.get("city_hint") or "").strip().lower()
    address = str(art_dict.get("address") or art_dict.get("location") or "").strip().lower()
    obs_title = str(
        _obs_attr(obs, "title")
        or art_dict.get("page_title")
        or art_dict.get("title")
        or ""
    ).strip().lower()
    excerpt = str(_obs_attr(obs, "excerpt_text") or "").strip().lower()

    # Reject immediately if structured city_hint explicitly names a non expected city
    city_hint_matches_expected = bool(
        city_hint
        and any(re.search(rf"\b{re.escape(exp)}\b", city_hint) for exp in norm_expected)
    )
    conflicting_pool = KNOWN_CONFLICTING_CITIES - norm_expected
    city_hint_has_conflict = bool(
        city_hint
        and any(re.search(rf"\b{re.escape(city)}\b", city_hint) for city in conflicting_pool)
    )
    if city_hint and (city_hint_has_conflict or not city_hint_matches_expected):
        return True
    if address:
        for city in conflicting_pool:
            if re.search(rf"\b{re.escape(city)}\b", address):
                return True

    if address and any(
        re.search(rf"\b{re.escape(exp)}\b", address) for exp in norm_expected
    ):
        for city in conflicting_pool:
            if re.search(
                rf"\b(?:(?:vs\.?|versus)\s+{re.escape(city)}|{re.escape(city)}\s+(?:vs\.?|versus))\b",
                excerpt,
            ):
                return True
        return False

    if obs_title:
        for city in conflicting_pool:
            if re.search(rf"\b{re.escape(city)}\b", obs_title):
                return True

    if city_hint_matches_expected and not city_hint_has_conflict:
        for city in conflicting_pool:
            if re.search(
                rf"\b(?:(?:vs\.?|versus)\s+{re.escape(city)}|{re.escape(city)}\s+(?:vs\.?|versus))\b",
                excerpt,
            ):
                return True
        return False

    has_expected_in_doc = any(
        re.search(rf"\b{re.escape(exp)}\b", f"{city_hint} {obs_title} {excerpt}")
        for exp in norm_expected
    )
    access_transit_re = re.compile(
        r"\b(?:nearest|closest)\s+(?:airport|railway|rail|station|bus)\b"
    )
    expected_alt = "|".join(re.escape(exp) for exp in sorted(norm_expected))
    for city in conflicting_pool:
        nav_pkg_re = re.compile(
            rf"(?:\b{re.escape(city)}\s+to\s+(?:{expected_alt})\b(?:\s+\w+){{0,3}}\s*\b(?:tour|tours|trip|trips|package|packages|cab|bus|taxi)\b"
            rf"|\b(?:tour|tours|trip|trips|package|packages|cab|taxi|visit)\s+(?:from|via)\s+{re.escape(city)}\b)",
            re.IGNORECASE,
        )
        for m in re.finditer(rf"\b{re.escape(city)}\b", excerpt):
            win = excerpt[max(0, m.start() - 65) : min(len(excerpt), m.end() + 80)]
            if has_expected_in_doc and (
                access_transit_re.search(win)
                or nav_pkg_re.search(win)
                or re.search(
                    rf"\b(?:buses?|trains?|flights?|airport|highway)\s+(?:from|to|via)\s+(?:[a-z]+\s+and\s+)?{re.escape(city)}\b",
                    win,
                )
            ):
                continue
            return True
    return False


def _extract_observation_title(obs: SourceObservationModel | dict[str, Any]) -> str | None:
    """Extracts structured place title or name from observation metadata or excerpt prefix."""
    art_ref = _obs_attr(obs, "artifact_ref")
    if isinstance(art_ref, dict):
        # Extract strictly from page or place identity fields, never from search origin title
        for k in ("page_title", "title", "place_name", "name", "property_name"):
            val = art_ref.get(k)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
    obs_title = _obs_attr(obs, "title")
    if obs_title and isinstance(obs_title, str) and obs_title.strip():
        return obs_title.strip()
    excerpt = _obs_attr(obs, "excerpt_text") or ""
    if excerpt.startswith("Place: "):
        first_segment = excerpt.split("|")[0].replace("Place: ", "").strip()
        if first_segment:
            return first_segment
    return None


def _match_observation_entity(
    obs: SourceObservationModel | dict[str, Any],
    candidate: RegisteredCandidate,
    *,
    expected_cities: set[str] | None = None,
) -> tuple[bool, bool, tuple[float, float] | None]:
    """Matches an observation to candidate entity and checks active vs closed status.

    Returns a tuple of (is_entity_match, is_closed_or_unavailable, verified_coordinates).
    """
    if _has_conflicting_city(obs, expected_cities):
        return False, False, None

    cand_name = candidate.primary_name.strip()
    cand_norm = _normalize_name(cand_name)
    cand_tokens = cand_norm.split()
    if not cand_tokens:
        return False, False, None

    obs_title = _extract_observation_title(obs)
    obs_title_norm = _normalize_name(obs_title) if obs_title else ""
    title_tokens = obs_title_norm.split() if obs_title_norm else []

    art_ref = _obs_attr(obs, "artifact_ref")
    if "advisory" in extract_observation_purposes(art_ref):
        # Road advisory observations cannot match candidate place existence
        return False, False, None

    art_category = ""
    art_types: list[str] = []
    art_title_norm = ""
    art_tokens: list[str] = []
    if isinstance(art_ref, dict):
        art_title = (
            art_ref.get("page_title")
            or art_ref.get("title")
            or art_ref.get("name")
            or art_ref.get("place_name")
            or art_ref.get("property_name")
        )
        if art_title and isinstance(art_title, str):
            art_title_norm = _normalize_name(art_title)
            art_tokens = art_title_norm.split()
        art_category = str(art_ref.get("category") or "").lower()
        raw_types = art_ref.get("types") or []
        if isinstance(raw_types, list):
            art_types = [str(t).lower() for t in raw_types]

    # Incompatible entity category check
    cand_cat = (candidate.category or "").lower()
    incompatible_categories = {
        "transit",
        "transit_station",
        "bus_station",
        "train_station",
        "subway_station",
        "lodging",
        "hotel",
        "atm",
        "bank",
        "hospital",
        "police",
        "school",
    }
    if cand_cat == "destination":
        obs_purposes = extract_observation_purposes(art_ref)
        if obs_purposes & {"hours", "attractions", "hidden_places", "dining", "lodging", "food"}:
            return False, False, None
        if art_category and art_category != "destination":
            return False, False, None
        if isinstance(art_ref, dict) and (art_ref.get("hours") or art_ref.get("timings")):
            return False, False, None

    if cand_cat in ("attraction", "heritage", "monument", "nature", "sightseeing"):
        if art_category in incompatible_categories or any(
            t in incompatible_categories for t in art_types
        ):
            return False, False, None

    excerpt = (_obs_attr(obs, "excerpt_text") or "").strip()
    excerpt_lower = excerpt.lower()
    obs_ext_method = str(_obs_attr(obs, "extraction_method") or "").upper().strip()
    is_page_observation = bool(obs_ext_method and obs_ext_method not in SNIPPET_SOURCE_METHODS)

    # Determine structured entity title from observation; for web pages, strip trailing SEO suffixes
    # (e.g. "– Timings, Entry Fee, Location & Hotels Nearby") before modifier checks.
    raw_entity_title = (
        (
            art_ref.get("page_title")
            or art_ref.get("title")
            or art_ref.get("name")
            or art_ref.get("place_name")
            or art_ref.get("property_name")
        )
        if isinstance(art_ref, dict)
        else None
    ) or obs_title
    if is_page_observation and isinstance(raw_entity_title, str) and raw_entity_title.strip():
        stripped_title = re.split(
            r"\s*[-–—|:(]\s*(?:timings?|hours|entry\s+fee|entrance|ticket|20\d\d)\b",
            raw_entity_title.strip(),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        if stripped_title:
            stripped_norm = _normalize_name(stripped_title)
            entity_norm = stripped_norm
            entity_tokens = stripped_norm.split()
        else:
            entity_norm = art_title_norm or obs_title_norm
            entity_tokens = art_tokens or title_tokens
    else:
        entity_norm = art_title_norm or obs_title_norm
        entity_tokens = art_tokens or title_tokens

    # Reject multi-venue listicle/roundup page titles unless candidate itself is the leading subject
    if is_page_observation and isinstance(raw_entity_title, str) and raw_entity_title.strip():
        raw_title_lower = raw_entity_title.strip().lower()
        is_listicle_title = bool(
            re.search(
                r"\b(top\s+\d+|best\s+\d+|\d+\s+(?:best|top|places|attractions)|places\s+to\s+visit|things\s+to\s+do)\b",
                raw_title_lower,
            )
        )
        if is_listicle_title and not entity_norm.startswith(cand_norm):
            return False, False, None

    title_matches = False
    if entity_norm:
        if cand_norm == entity_norm:
            title_matches = True
        elif _tokens_match_ignoring_inflection(cand_tokens, entity_tokens):
            title_matches = True
        else:
            extra_in_entity = set(entity_tokens) - set(cand_tokens)
            extra_in_cand = set(cand_tokens) - set(entity_tokens)

            # If the entity title contains distinct entity modifiers (cafe, bus stop, school, etc.), reject
            if extra_in_entity & DISTINCT_ENTITY_MODIFIERS:
                return False, False, None
            if extra_in_cand & DISTINCT_ENTITY_MODIFIERS:
                return False, False, None

            # Shorter candidate name matching a different longer title:
            # Allow expected destination city tokens and benign info/hours words alongside monument descriptors.
            allowed_extras = set(ALLOWED_VENUE_INFO_TOKENS)
            if expected_cities:
                for ec in expected_cities:
                    allowed_extras.update(_normalize_name(ec).split())

            def _scraped_title_compatible() -> bool:
                if cand_cat == "destination":
                    return False
                cand_set = set(cand_tokens)
                tokens_to_check = extra_in_entity if len(cand_tokens) <= len(entity_tokens) else entity_tokens
                unexplained = {
                    tok
                    for tok in tokens_to_check
                    if tok not in allowed_extras
                    and not any(_tokens_match_ignoring_inflection([ct], [tok]) for ct in cand_set)
                }
                shared_count = sum(
                    1
                    for ct in cand_set
                    if any(_tokens_match_ignoring_inflection([ct], [et]) for et in entity_tokens)
                )
                min_shared = min(2, len(cand_set), len(entity_tokens))
                return not unexplained and shared_count >= min_shared

            if len(cand_tokens) < len(entity_tokens):
                if cand_cat == "destination" or len(cand_tokens) < 2 or not extra_in_entity.issubset(allowed_extras):
                    if not is_page_observation or not _scraped_title_compatible():
                        return False, False, None
                elif set(cand_tokens).issubset(set(entity_tokens)):
                    title_matches = True

            # Candidate name has more words than entity:
            elif len(cand_tokens) > len(entity_tokens):
                if cand_cat == "destination" or len(entity_tokens) < 2 or not extra_in_cand.issubset(allowed_extras):
                    if not is_page_observation or not _scraped_title_compatible():
                        return False, False, None
                elif set(entity_tokens).issubset(set(cand_tokens)):
                    title_matches = True

            # Equal token length with non-inflectional differences (e.g., "Government Rose Garden"):
            else:
                if not is_page_observation or not _scraped_title_compatible():
                    return False, False, None

    excerpt_matches = False
    if not title_matches and excerpt and cand_cat != "destination" and (not entity_norm or is_page_observation):
        # Check text observations when untitled or when matching attributable scraped page content
        if cand_norm in excerpt_lower or cand_name.lower() in excerpt_lower:
            if not _is_casual_mention(excerpt):
                cand_lower = cand_norm if cand_norm in excerpt_lower else cand_name.lower()
                idx = excerpt_lower.find(cand_lower)
                leading_text = excerpt_lower[max(0, idx - 40) : idx]
                trailing_text = excerpt_lower[idx + len(cand_lower) : idx + len(cand_lower) + 40]
                leading_words = set(re.findall(r"\b\w+\b", leading_text))
                trailing_words = set(re.findall(r"\b\w+\b", trailing_text))
                is_distance_mention = bool(
                    re.search(
                        r"\b\d+(?:\.\d+)?\s*(?:km|m|miles?)\s+(?:away\s+)?(?:from|to|near)\s+(?:the\s+|a\s+|an\s+)?$",
                        leading_text,
                    )
                    or re.match(
                        r"^\s*(?:is|are)?\s*\d+(?:\.\d+)?\s*(?:km|m|miles?)\s+(?:away\s+)?(?:from|to|near)\b",
                        trailing_text,
                    )
                )
                if not is_distance_mention and not ((leading_words | trailing_words) & DISTINCT_ENTITY_MODIFIERS):
                    excerpt_matches = True

    name_matches = title_matches or excerpt_matches
    if not name_matches:
        # Metadata or title describes a different entity
        return False, False, None

    # Spatial verification against candidate location
    obs_coords: tuple[float, float] | None = None
    art_dict = _obs_attr(obs, "artifact_ref")
    if isinstance(art_dict, dict):
        obs_coords = _extract_coordinates_from_dict(art_dict)
    if obs_coords is None:
        geo_dict = _obs_attr(obs, "geographic_scope")
        if isinstance(geo_dict, dict):
            obs_coords = _extract_coordinates_from_dict(geo_dict)
    if obs_coords is not None and candidate.point is not None:
        dist = haversine_distance_km(
            candidate.point.lat,
            candidate.point.lng,
            obs_coords[0],
            obs_coords[1],
        )
        if dist > 1.0:
            # Same-name place located far away: reject
            return False, False, None

    has_identity_evidence = (
        title_matches
        or (excerpt_matches and len(excerpt) >= 40 and not _is_casual_mention(excerpt))
        or obs_coords is not None
    )
    if not has_identity_evidence:
        return False, False, None

    is_closed = _is_place_closed_or_unavailable(obs)
    return True, is_closed, obs_coords


def _scope_page_text_to_candidate(
    page_text: str,
    cand_name: str,
    expected_cities: set[str] | None = None,
) -> str:
    """Drops sentences/clauses that attribute opening hours to a different named place."""
    if not page_text or not cand_name:
        return page_text
    cand_tokens = set(_normalize_name(cand_name).split())
    if not cand_tokens:
        return page_text
    city_tokens: set[str] = set()
    if expected_cities:
        for ec in expected_cities:
            city_tokens.update(_normalize_name(ec).split())

    segments = re.split(
        r"(?<!\b\d)(?<=[.!?])\s+|\n+|;\s+|,\s+(?=(?:which|where|while|whereas|and|but)\b)|\s+(?=while\s+|whereas\s+)",
        page_text,
    )

    conj_prefix = r"(?:(?:and|but|while|whereas|where)\s+)?"
    place_token = r"(?:[A-Z][A-Za-z0-9'&.-]*|of|the|and)"
    other_place_hours_re = re.compile(
        rf"^\s*{conj_prefix}(?:\d+[.)]\s*|[-*•]\s*)?"
        rf"([A-Z][A-Za-z0-9'&.-]*(?:\s+{place_token}){{0,5}}"
        r"(?:\s+(?:garden|gardens|park|parks|temple|temples|museum|museums|palace|fort|lake|falls|peak|hill|zoo|beach))?)"
        r"(?:\s*\([^)]+\)|,\s*[A-Z][A-Za-z0-9'&.-]*|\s+(?:in|at|near)\s+[A-Z][A-Za-z0-9'&.-]*)?"
        r"(?:\s+(?i:(?:opening|visiting|entry|general|admission)\s+)?(?i:timings?|hours))?\s*"
        r"(?:[:\-–]\s*(?:(?i:open|closed|timings?|hours)|\d)|(?i:is|are|opens?|closes?)\s+(?:(?i:generally|daily)\s+)?(?:(?i:open|closed)\b|(?:(?i:at|from)\s+)?\d)|\b(?i:timings?|hours)\s+(?i:are|is)\s+\d)",
    )
    lead_subject_re = re.compile(
        rf"^\s*{conj_prefix}(?:\d+[.)]\s*|[-*•]\s*|Visit\s+)?([A-Z][A-Za-z0-9'&.-]*(?:\s+{place_token}){{0,5}})\s+(?:is|are|nearby)\b"
    )
    distance_obj_re = re.compile(
        r"(?:\b\d+(?:\.\d+)?\s*(?:km|m|miles?)\s+(?:away\s+)?(?:from|to|near)|\b(?:is|are)?\s*(?:near|opposite|beside|adjacent\s+to))\s+(?:the\s+)?([A-Z][A-Za-z0-9'&.-]*(?:\s+[A-Z][A-Za-z0-9'&.-]*){0,4})"
    )
    pronoun_hours_re = re.compile(
        rf"^\s*{conj_prefix}(?:it|this|they|which|where)\s+(?:is|are|opens?|closes?)\b",
        re.IGNORECASE,
    )
    generic_subjects = {
        "it",
        "the",
        "this",
        "they",
        "which",
        "where",
        "garden",
        "palace",
        "museum",
        "temple",
        "fort",
        "park",
        "timings",
        "timing",
        "hours",
        "hour",
        "open",
        "opening",
        "closing",
        "daily",
        "day",
        "today",
        "ticketing",
        "ticket",
        "entry",
        "entrance",
        "admission",
        "visiting",
        "visitor",
        "visitors",
        "gate",
        "general",
        "last",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "mon",
        "tue",
        "tues",
        "wed",
        "thu",
        "thur",
        "thurs",
        "fri",
        "sat",
        "sun",
        "weekdays",
        "weekday",
        "weekends",
        "weekend",
        "holiday",
        "holidays",
        "summer",
        "winter",
        "boating",
        "illumination",
        "sound",
        "light",
        "show",
        "safari",
    }

    def _is_other_place(tokens: set[str]) -> bool:
        if not tokens or tokens <= generic_subjects:
            return False
        for tok in tokens:
            if tok in generic_subjects or tok in city_tokens:
                continue
            if not any(_tokens_match_ignoring_inflection([tok], [ct]) for ct in cand_tokens):
                return True
        return False

    kept: list[str] = []
    last_other_subject = False
    for seg in segments:
        if pronoun_hours_re.match(seg) and last_other_subject:
            continue
        m = other_place_hours_re.match(seg)
        if m:
            subj_tokens = set(_normalize_name(m.group(1).strip()).split())
            if _is_other_place(subj_tokens):
                last_other_subject = True
                continue
        dist_m = distance_obj_re.search(seg)
        if dist_m:
            obj_tokens = set(_normalize_name(dist_m.group(1).strip()).split())
            if _is_other_place(obj_tokens):
                last_other_subject = True
                kept.append(seg)
                continue
        lead_m = lead_subject_re.match(seg)
        if lead_m:
            lead_tokens = set(_normalize_name(lead_m.group(1).strip()).split())
            last_other_subject = _is_other_place(lead_tokens)
        else:
            last_other_subject = False
        kept.append(seg)
    return " ".join(kept).strip()


def _observation_supports_candidate(
    obs: SourceObservationModel,
    candidate: RegisteredCandidate,
) -> tuple[bool, tuple[float, float] | None]:
    """Evaluates whether an observation genuinely supports active candidate existence."""
    is_match, is_closed, coords = _match_observation_entity(obs, candidate)
    if is_match and not is_closed:
        return True, coords
    return False, None


async def ingest_claims_from_observations(
    session: AsyncSession,
    execution_id: uuid.UUID,
    candidates: list[RegisteredCandidate],
    owner_id: uuid.UUID | None = None,
    as_of: datetime | None = None,
    expected_cities: set[str] | None = None,
    local_timezone: str = "Asia/Kolkata",
    llm_client: Any | None = None,
    include_hours_claims: bool = False,
) -> list[ClaimModel]:
    """Derives and persists claims from genuine collected source observations for candidate places.

    Guarantees:
    - Only valid, non NEEDS_BROWSER, non error observations are used.
    - Matching requires substantive place name match and spatial corroboration.
    - An unrelated nearby coordinate or a name in a URL alone never becomes a SUPPORTS link.
    - Candidate's own coordinates are never used as independent proof.
    - Same-name places located far apart are rejected.
    - Claims are linked to source observations with explicit SUPPORTS evidence links.
    - Accommodation extractions preserve INDICATIVE_SEARCH and are never promoted to LIVE_OFFER.
    - Existing claims are preserved without duplicate creation.
    """
    now = as_of or datetime.now(UTC)

    # 1. Query source observations for this execution
    obs_stmt = (
        select(SourceObservationModel)
        .where(
            SourceObservationModel.execution_id == execution_id,
            SourceObservationModel.extraction_method != "NEEDS_BROWSER",
        )
        .order_by(SourceObservationModel.created_at.asc())
    )
    obs_res = await session.execute(obs_stmt)
    all_observations = list(obs_res.scalars().all())

    # Filter out unavailable or error observations
    valid_observations: list[SourceObservationModel] = []
    for obs in all_observations:
        art_ref = obs.artifact_ref or {}
        if isinstance(art_ref, dict):
            status_val = str(art_ref.get("status") or "").upper().strip()
            if status_val in (
                "UNAVAILABLE",
                "BLOCKED",
                "TIMED_OUT",
                "FAILED",
                "ERROR",
                "NOT_FOUND",
            ):
                continue
            if (
                art_ref.get("error")
                or art_ref.get("unavailable_reason")
                or art_ref.get("unavailable_sources")
            ):
                continue
        excerpt = obs.excerpt_text or ""
        if "[DYNAMIC_PAGE: JAVASCRIPT_REQUIRED]" in excerpt:
            continue
        valid_observations.append(obs)

    if not valid_observations:
        logger.info(
            "No valid source observations found for execution %s during claim ingestion",
            execution_id,
        )
        return []

    claim_service = ClaimVerificationService(session=session)
    ingested_claims: list[ClaimModel] = []

    from functions.orchestrator.evidence.place_extraction import (
        PlaceExtractionService,
        extract_observation_purposes,
    )

    has_any_hidden_obs = any(
        "hidden_places" in extract_observation_purposes(_obs_attr(o, "artifact_ref"))
        for o in valid_observations
    )
    hidden_eval = (
        PlaceExtractionService.evaluate_hidden_place_candidates(
            valid_observations,
            expected_cities=expected_cities,
        )
        if has_any_hidden_obs
        else {"verified_optional_stops": [], "restricted_or_closed": []}
    )
    verified_hidden_names = {
        _normalize_name(str(item.get("name") or ""))
        for item in hidden_eval.get("verified_optional_stops", [])
        if item.get("name")
    }
    restricted_hidden_names = {
        _normalize_name(str(item.get("name") or ""))
        for item in hidden_eval.get("restricted_or_closed", [])
        if item.get("name")
    }

    # 2. Ingest existence claims for matching candidates
    for candidate in candidates:
        cand_name = candidate.primary_name.strip()
        cand_name_norm = cand_name.lower()
        if not cand_name_norm:
            continue

        # Distinguish local dishes from physical businesses
        if PlaceExtractionService.is_culinary_dish(cand_name):
            # A culinary dish is a regional preparation or specialty, not a physical venue
            logger.info("Skipping candidate existence claim for culinary dish '%s'", cand_name)
            continue

        # Map candidate category to appropriate claim category
        cand_cat = candidate.category.strip().lower()
        if cand_cat in ("restaurant", "street_food", "cafe", "dining", "cuisine", "food"):
            cand_claim_category: Any = ClaimCategory.CUISINE
            claim_cat_str = ClaimCategory.CUISINE.value
        elif cand_cat in ("lodging", "hotel"):
            cand_claim_category = "LODGING"
            claim_cat_str = "LODGING"
        else:
            cand_claim_category = ClaimCategory.ATTRACTION
            claim_cat_str = ClaimCategory.ATTRACTION.value

        # Check existing claims for this candidate under this execution
        existing_stmt = select(ClaimModel).where(
            ClaimModel.execution_id == execution_id,
            ClaimModel.subject_id == cand_name_norm,
            ClaimModel.category == claim_cat_str,
        )
        if candidate.place_id is not None:
            existing_stmt = existing_stmt.where(ClaimModel.place_id == candidate.place_id)

        existing_claims = list((await session.execute(existing_stmt)).scalars().all())
        existing_statuses = {
            str((c.typed_value or {}).get("status") or "").lower() for c in existing_claims
        }

        # Find matching observations with independent verification
        supporting_obs: list[SourceObservationModel] = []
        refuting_obs: list[SourceObservationModel] = []
        verified_coords: tuple[float, float] | None = None
        refuting_coords: tuple[float, float] | None = None

        for obs in valid_observations:
            is_match, is_closed, coords = _match_observation_entity(
                obs,
                candidate,
                expected_cities=expected_cities,
            )
            if not is_match:
                continue
            if is_closed:
                refuting_obs.append(obs)
                if coords is not None and refuting_coords is None:
                    refuting_coords = coords
            else:
                supporting_obs.append(obs)
                if coords is not None and verified_coords is None:
                    verified_coords = coords

        if not supporting_obs and not refuting_obs:
            continue

        cand_norm_key = _normalize_name(cand_name)
        has_hidden_obs = any(
            "hidden_places" in extract_observation_purposes(_obs_attr(o, "artifact_ref"))
            for o in [*supporting_obs, *refuting_obs]
        )
        has_non_hidden_fetched_page = any(
            "hidden_places" not in extract_observation_purposes(_obs_attr(o, "artifact_ref"))
            and str(_obs_attr(o, "extraction_method") or "").upper().strip()
            not in SNIPPET_SOURCE_METHODS
            and str(_obs_attr(o, "extraction_method") or "").upper().strip() != "NEEDS_BROWSER"
            for o in supporting_obs
        )
        is_restricted_hidden = has_hidden_obs and cand_norm_key in restricted_hidden_names
        if is_restricted_hidden:
            refuting_obs = list(dict.fromkeys([*refuting_obs, *supporting_obs]))
            supporting_obs = []
        elif (
            has_hidden_obs
            and cand_norm_key not in verified_hidden_names
            and not has_non_hidden_fetched_page
        ):
            # Unverified hidden-place candidates (snippet-only, missing coords, or unknown public access)
            # must not receive active existence claims and must stay out of eligible_stops.
            supporting_obs = []

        # Positive active support: ingest active claim
        if supporting_obs and "active" not in existing_statuses:
            support_links = [
                (obs, EvidenceSupportType.SUPPORTS, obs.canonical_url or obs.source_url)
                for obs in supporting_obs
            ]
            claim_create = ClaimCreate(
                subject_id=cand_name_norm,
                category=cand_claim_category,
                place_id=candidate.place_id,
                execution_id=execution_id,
                typed_value={
                    "place_name": cand_name,
                    "category": candidate.category,
                    "source": "source_observation",
                    "status": "active",
                    "evidence_class": EvidenceClass.INDICATIVE_SEARCH.value,
                },
                confidence=0.9,
                valid_from=supporting_obs[0].retrieved_at or now,
                valid_until=now + timedelta(days=365),
                geographic_scope={
                    "lat": verified_coords[0],
                    "lng": verified_coords[1],
                }
                if verified_coords
                else None,
            )
            try:
                created = await claim_service.submit_claim(
                    claim_data=claim_create,
                    observations_with_support=support_links,
                    as_of=now,
                )
                ingested_claims.append(created)
                logger.info("Ingested active claim %s for candidate '%s'", created.id, cand_name)
            except Exception as exc:
                logger.warning(
                    "Failed submitting active claim for candidate '%s': %s", cand_name, exc
                )

        # Conflicting or restricted hidden-place evidence: ingest closed/refuting claim
        if (supporting_obs or is_restricted_hidden) and refuting_obs and "closed" not in existing_statuses:
            conflict_links = [
                (obs, EvidenceSupportType.REFUTES, obs.canonical_url or obs.source_url)
                for obs in refuting_obs
            ]
            target_conflict_coords = refuting_coords or verified_coords
            closed_claim = ClaimCreate(
                subject_id=cand_name_norm,
                category=cand_claim_category,
                place_id=candidate.place_id,
                execution_id=execution_id,
                typed_value={
                    "place_name": cand_name,
                    "category": candidate.category,
                    "source": "source_observation",
                    "status": "closed",
                    "evidence_class": EvidenceClass.INDICATIVE_SEARCH.value,
                },
                confidence=0.9,
                valid_from=refuting_obs[0].retrieved_at or now,
                valid_until=now + timedelta(days=365),
                geographic_scope={
                    "lat": target_conflict_coords[0],
                    "lng": target_conflict_coords[1],
                }
                if target_conflict_coords is not None
                else None,
            )
            try:
                created_closed = await claim_service.submit_claim(
                    claim_data=closed_claim,
                    observations_with_support=conflict_links,
                    as_of=now,
                )
                ingested_claims.append(created_closed)
                logger.info(
                    "Ingested conflicting closed claim %s for candidate '%s'",
                    created_closed.id,
                    cand_name,
                )
            except Exception as exc:
                logger.warning(
                    "Failed submitting conflicting closed claim for candidate '%s': %s",
                    cand_name,
                    exc,
                )

        # Ingest source backed HOURS claims strictly from non snippet page observations when enabled
        if include_hours_claims:
            existing_hours_stmt = select(ClaimModel).where(
                ClaimModel.execution_id == execution_id,
                ClaimModel.subject_id == cand_name_norm,
                ClaimModel.category == ClaimCategory.HOURS.value,
            )
            if candidate.place_id is not None:
                existing_hours_stmt = existing_hours_stmt.where(
                    ClaimModel.place_id == candidate.place_id
                )
            existing_hours_claims = list((await session.execute(existing_hours_stmt)).scalars().all())
            existing_hours_urls = {
                str((c.typed_value or {}).get("source_url") or "").strip()
                for c in existing_hours_claims
                if isinstance(c.typed_value, dict) and (c.typed_value or {}).get("source_url")
            }

            for obs in supporting_obs:
                ext_method = str(getattr(obs, "extraction_method", "") or "").upper().strip()
                obs_url = str(getattr(obs, "canonical_url", None) or getattr(obs, "source_url", None) or "").strip()
                if not obs_url or obs_url in existing_hours_urls:
                    continue

                obs_art = obs.artifact_ref if isinstance(obs.artifact_ref, dict) else {}
                raw_hours_hint = str(obs_art.get("hours") or obs_art.get("timings") or "").strip()
                raw_page_text = f"{raw_hours_hint}. {obs.excerpt_text or ''}".strip(". ")
                is_snippet_only = ext_method in SNIPPET_SOURCE_METHODS
                page_text = (
                    raw_page_text
                    if is_snippet_only
                    else _scope_page_text_to_candidate(
                        raw_page_text, cand_name, expected_cities=expected_cities
                    )
                )

                extracted = extract_structured_hours_from_text(
                    page_text,
                    source_url=obs_url,
                    local_timezone=local_timezone,
                    place_name=cand_name,
                    source_kind="SEARCH_SNIPPET" if is_snippet_only else "PAGE_CONTENT",
                )
                if (
                    extracted is None
                    and not is_snippet_only
                    and llm_client is not None
                    and any(
                        k in page_text.lower()
                        for k in ("open", "close", "hour", "timing", "entry", "ticket")
                    )
                ):
                    extracted = await extract_structured_hours_with_llm(
                        llm_client,
                        place_name=cand_name,
                        destination_name=", ".join(sorted(expected_cities)) if expected_cities else cand_name,
                        page_text=page_text,
                        source_url=obs_url,
                        local_timezone=local_timezone,
                    )

                if extracted is None or is_snippet_only:
                    continue

                existing_hours_urls.add(obs_url)
                hours_claim = ClaimCreate(
                    subject_id=cand_name_norm,
                    category=ClaimCategory.HOURS,
                    place_id=candidate.place_id,
                    execution_id=execution_id,
                    typed_value={
                        **extracted.model_dump(),
                        "extraction_method": ext_method or "SCRAPLING_HTTP",
                        "source_kind": "PAGE_CONTENT",
                    },
                    confidence=0.9,
                    valid_from=obs.retrieved_at or now,
                    valid_until=now + timedelta(days=90),
                    geographic_scope={
                        "lat": verified_coords[0],
                        "lng": verified_coords[1],
                    }
                    if verified_coords
                    else None,
                )
                try:
                    created_hours = await claim_service.submit_claim(
                        claim_data=hours_claim,
                        observations_with_support=[(obs, EvidenceSupportType.SUPPORTS, obs_url)],
                        as_of=now,
                    )
                    ingested_claims.append(created_hours)
                except Exception as hours_exc:
                    logger.warning(
                        "Failed submitting hours claim for candidate '%s': %s",
                        cand_name,
                        hours_exc,
                    )

    # 3. Ingest accommodation observations, strictly preserving INDICATIVE_SEARCH
    for obs in valid_observations:
        art_ref = obs.artifact_ref or {}
        cat = str(art_ref.get("category") or "").lower() if isinstance(art_ref, dict) else ""
        is_stay = "lodging" in cat or "hotel" in cat or "stay" in cat
        if not is_stay and isinstance(art_ref, dict):
            t = str(art_ref.get("property_name") or art_ref.get("title") or "").lower()
            if any(w in t for w in ("hotel", "resort", "homestay", "lodge", "inn", "hostel")):
                is_stay = True
        if is_stay:
            hotel_name = str(
                art_ref.get("property_name") or art_ref.get("name") or art_ref.get("title") or ""
            ).strip()
            if not hotel_name or hotel_name.lower() in ("lodging", "hotel"):
                continue
            hotel_norm = hotel_name.lower()
            existing_stay_stmt = select(ClaimModel).where(
                ClaimModel.execution_id == execution_id,
                ClaimModel.subject_id == hotel_norm,
                ClaimModel.category == "LODGING",
            )
            if (await session.execute(existing_stay_stmt)).scalar_one_or_none() is not None:
                continue

            hotel_coords = (
                _extract_coordinates_from_dict(art_ref) if isinstance(art_ref, dict) else None
            )
            raw_price = art_ref.get("price_per_night") if isinstance(art_ref, dict) else None
            if raw_price is None and isinstance(art_ref, dict):
                raw_price = art_ref.get("indicative_price_inr")
            currency_val = art_ref.get("currency") if isinstance(art_ref, dict) else None
            obs_url = str(
                getattr(obs, "canonical_url", None) or getattr(obs, "source_url", None) or ""
            ).strip()
            has_usable_url = bool(
                obs_url and (obs_url.startswith("http://") or obs_url.startswith("https://"))
            )
            has_valid_indicative_price = (
                raw_price is not None and bool(currency_val) and has_usable_url
            )

            stay_claim = ClaimCreate(
                subject_id=hotel_norm,
                category="LODGING",
                place_id=None,
                execution_id=execution_id,
                typed_value={
                    "property_name": hotel_name,
                    "observation_id": str(obs.id) if getattr(obs, "id", None) else None,
                    "source_url": obs_url if has_usable_url else None,
                    "evidence_class": (
                        EvidenceClass.INDICATIVE_SEARCH.value
                        if has_valid_indicative_price
                        else None
                    ),
                    "price_per_night": raw_price if has_valid_indicative_price else None,
                    "currency": currency_val if has_valid_indicative_price else None,
                    "is_indicative_search": has_valid_indicative_price,
                    "is_verified_offer": False,
                    "booking_confirmed": False,
                    "availability": "UNKNOWN",
                },
                confidence=0.85,
                valid_from=obs.retrieved_at or now,
                valid_until=now + timedelta(days=90),
                geographic_scope={
                    "lat": hotel_coords[0],
                    "lng": hotel_coords[1],
                }
                if hotel_coords
                else None,
            )
            try:
                created = await claim_service.submit_claim(
                    claim_data=stay_claim,
                    observations_with_support=[
                        (obs, EvidenceSupportType.SUPPORTS, obs.canonical_url)
                    ],
                    as_of=now,
                )
                ingested_claims.append(created)
            except Exception as stay_exc:
                logger.warning("Failed submitting lodging claim for '%s': %s", hotel_name, stay_exc)

    # 4. Ingest travel advisory, road hazard, and restriction observations
    for obs in valid_observations:
        art_ref = obs.artifact_ref or {}
        cat = str(art_ref.get("category") or "").lower() if isinstance(art_ref, dict) else ""
        excerpt = str(getattr(obs, "excerpt_text", "") or "")
        headline = (
            str(
                art_ref.get("headline") or art_ref.get("title") or art_ref.get("summary") or ""
            ).strip()
            if isinstance(art_ref, dict)
            else ""
        )

        obs_purposes = extract_observation_purposes(art_ref)
        is_advisory = "advisory" in obs_purposes or any(
            k in cat
            for k in (
                "advisory",
                "hazard",
                "road_closure",
                "closure",
                "restriction",
                "weather_warning",
                "disaster",
                "weather_alert",
            )
        )
        if not is_advisory and (
            any(
                k in (cat + " " + headline + " " + excerpt).lower()
                for k in (
                    "road closure",
                    "road closed",
                    "highway closed",
                    "traffic diversion",
                    "cyclone warning",
                    "cyclone alert",
                    "landslide warning",
                    "flood warning",
                    "flooding alert",
                    "travel advisory",
                    "travel restriction",
                )
            )
        ):
            is_advisory = True

        if not is_advisory:
            continue

        if not headline and excerpt:
            first_sent = excerpt.split(".")[0].strip()
            headline = first_sent[:120] if first_sent else "Travel Advisory"

        affected_area = (
            str(
                art_ref.get("affected_area")
                or art_ref.get("location")
                or art_ref.get("route")
                or ""
            ).strip()
            if isinstance(art_ref, dict)
            else ""
        )

        adv_type = (
            str(art_ref.get("advisory_type") or "").upper() if isinstance(art_ref, dict) else ""
        )
        combined_text = (cat + " " + headline + " " + excerpt).lower()
        if not adv_type:
            if "cyclone" in combined_text:
                adv_type = "CYCLONE"
            elif "flood" in combined_text or "waterlog" in combined_text:
                adv_type = "FLOOD"
            elif "landslide" in combined_text:
                adv_type = "LANDSLIDE"
            elif any(w in combined_text for w in ("closure", "closed", "blocked", "diversion")):
                adv_type = "ROAD_CLOSURE"
            elif "restriction" in combined_text or "curfew" in combined_text:
                adv_type = "TRAVEL_RESTRICTION"
            elif "rain" in combined_text or "storm" in combined_text:
                adv_type = "WEATHER_WARNING"
            else:
                adv_type = "GENERAL_ADVISORY"

        from functions.orchestrator.evidence.policy import (
            is_verified_authority_domain,
            safe_parse_datetime,
        )

        severity = (
            str(art_ref.get("severity") or "WARNING").upper()
            if isinstance(art_ref, dict)
            else "WARNING"
        )
        raw_url = getattr(obs, "canonical_url", None) or getattr(obs, "source_url", None) or ""
        obs_url = str(raw_url).strip()
        has_usable_url = bool(
            obs_url and (obs_url.startswith("http://") or obs_url.startswith("https://"))
        )

        # 1. Authority validation against explicit policy (no substring matching, no untrusted flags)
        is_auth = is_verified_authority_domain(obs_url)

        # 2. Extraction method check: search snippets remain unverified unless scraped content supports it
        ext_method = str(getattr(obs, "extraction_method", "") or "").upper()
        is_search_snippet = ext_method in ("SERPAPI_SEARCH", "SEARCH_SNIPPET", "SEARCH_RESULT")
        is_confirmed = is_auth and not is_search_snippet

        status_val = (
            str(art_ref.get("status") or "ACTIVE").upper()
            if isinstance(art_ref, dict)
            else "ACTIVE"
        )
        if any(
            w in combined_text
            for w in ("reopened", "lifted", "cleared", "normal traffic", "road opened")
        ):
            status_val = "LIFTED"

        start_time_val = art_ref.get("applicable_start") if isinstance(art_ref, dict) else None
        end_time_val = art_ref.get("applicable_end") if isinstance(art_ref, dict) else None

        # 3. Time and freshness validation
        parsed_start = safe_parse_datetime(start_time_val)
        parsed_end = safe_parse_datetime(end_time_val)
        is_malformed = (start_time_val is not None and parsed_start is None) or (
            end_time_val is not None and parsed_end is None
        )
        is_undated = parsed_start is None and parsed_end is None

        ret_dt = getattr(obs, "retrieved_at", None)
        pub_dt = getattr(obs, "published_at", None)
        obs_dt = pub_dt or ret_dt
        is_stale = False
        if obs_dt is not None:
            dt_with_tz = obs_dt if obs_dt.tzinfo else obs_dt.replace(tzinfo=UTC)
            age_hours = (now - dt_with_tz).total_seconds() / 3600.0
            if age_hours > 24.0 and parsed_end is None:
                is_stale = True

        is_expired = False
        if parsed_end is not None and parsed_end.date() < now.date():
            is_expired = True

        if is_malformed:
            status_val = "MALFORMED_DATES"
            uncertainty_val = "MALFORMED_DATES"
        elif is_expired:
            status_val = "EXPIRED"
            uncertainty_val = "EXPIRED"
        elif is_undated:
            status_val = "UNDATED"
            uncertainty_val = "UNVERIFIED_UNDATED"
        elif is_stale:
            status_val = "STALE"
            uncertainty_val = "STALE"
        elif status_val == "LIFTED":
            uncertainty_val = "LIFTED"
        elif is_confirmed and status_val == "ACTIVE":
            uncertainty_val = "CONFIRMED_AUTHORITATIVE"
        else:
            uncertainty_val = "UNVERIFIED_SEARCH"

        subject_key = _normalize_name(affected_area or headline)[:64]
        if not subject_key:
            subject_key = "general"
        subject_id = f"advisory:{adv_type.lower()}:{subject_key}"

        claim_category = (
            "RESTRICTION" if adv_type in ("ROAD_CLOSURE", "TRAVEL_RESTRICTION") else "WEATHER"
        )

        existing_adv_stmt = select(ClaimModel).where(
            ClaimModel.execution_id == execution_id,
            ClaimModel.subject_id == subject_id,
            ClaimModel.category == claim_category,
        )
        if (await session.execute(existing_adv_stmt)).scalar_one_or_none() is not None:
            continue

        adv_coords = _extract_coordinates_from_dict(art_ref) if isinstance(art_ref, dict) else None

        advisory_claim = ClaimCreate(
            subject_id=subject_id,
            category=claim_category,
            place_id=None,
            execution_id=execution_id,
            typed_value={
                "advisory_type": adv_type,
                "severity": severity,
                "headline": headline,
                "affected_area": affected_area,
                "applicable_start": str(start_time_val) if start_time_val is not None else None,
                "applicable_end": str(end_time_val) if end_time_val is not None else None,
                "is_authoritative": is_auth,
                "status": status_val,
                "uncertainty": uncertainty_val,
                "observation_id": str(obs.id) if getattr(obs, "id", None) else None,
                "source_url": obs_url if has_usable_url else None,
                "evidence_class": EvidenceClass.INDICATIVE_SEARCH.value,
            },
            confidence=0.95 if is_auth else 0.75,
            valid_from=obs.retrieved_at or now,
            valid_until=now + timedelta(days=30),
            geographic_scope={
                "lat": adv_coords[0],
                "lng": adv_coords[1],
            }
            if adv_coords
            else None,
        )
        try:
            created = await claim_service.submit_claim(
                claim_data=advisory_claim,
                observations_with_support=[(obs, EvidenceSupportType.SUPPORTS, obs.canonical_url)],
                as_of=now,
            )
            ingested_claims.append(created)
        except Exception as adv_exc:
            logger.warning("Failed submitting advisory claim for '%s': %s", subject_id, adv_exc)

    return ingested_claims
