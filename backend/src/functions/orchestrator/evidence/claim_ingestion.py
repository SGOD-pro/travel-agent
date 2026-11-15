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
from functions.orchestrator.evidence.models import ClaimModel
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
        "resort",
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


def _is_place_closed_or_unavailable(obs: SourceObservationModel) -> bool:
    """Detects whether observation indicates the place is permanently closed or unavailable."""
    art_ref = getattr(obs, "artifact_ref", None)
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

    excerpt = getattr(obs, "excerpt_text", "") or ""
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


def _extract_observation_title(obs: SourceObservationModel) -> str | None:
    """Extracts structured place title or name from observation metadata or excerpt prefix."""
    art_ref = getattr(obs, "artifact_ref", None)
    if isinstance(art_ref, dict):
        # Extract strictly from page or place identity fields, never from search origin title
        for k in ("page_title", "title", "place_name", "name", "property_name"):
            val = art_ref.get(k)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
    excerpt = getattr(obs, "excerpt_text", "") or ""
    if excerpt.startswith("Place: "):
        first_segment = excerpt.split("|")[0].replace("Place: ", "").strip()
        if first_segment:
            return first_segment
    return None


def _match_observation_entity(
    obs: SourceObservationModel,
    candidate: RegisteredCandidate,
) -> tuple[bool, bool, tuple[float, float] | None]:
    """Matches an observation to candidate entity and checks active vs closed status.

    Returns a tuple of (is_entity_match, is_closed_or_unavailable, verified_coordinates).
    """
    cand_name = candidate.primary_name.strip()
    cand_norm = _normalize_name(cand_name)
    cand_tokens = cand_norm.split()
    if not cand_tokens:
        return False, False, None

    obs_title = _extract_observation_title(obs)
    obs_title_norm = _normalize_name(obs_title) if obs_title else ""
    title_tokens = obs_title_norm.split() if obs_title_norm else []

    art_ref = getattr(obs, "artifact_ref", None)
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
    if cand_cat in ("attraction", "heritage", "monument", "nature", "sightseeing"):
        if art_category in incompatible_categories or any(
            t in incompatible_categories for t in art_types
        ):
            return False, False, None

    # Determine structured entity title from observation
    entity_norm = art_title_norm or obs_title_norm
    entity_tokens = art_tokens or title_tokens

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
            # If candidate has fewer words than entity, reject unless candidate has >= 2 words
            # and ALL extra tokens in entity are allowed monument descriptors.
            if len(cand_tokens) < len(entity_tokens):
                if len(cand_tokens) < 2 or not extra_in_entity.issubset(
                    ALLOWED_MONUMENT_DESCRIPTORS
                ):
                    return False, False, None
                if set(cand_tokens).issubset(set(entity_tokens)):
                    title_matches = True

            # Candidate name has more words than entity:
            elif len(cand_tokens) > len(entity_tokens):
                if len(entity_tokens) < 2 or not extra_in_cand.issubset(
                    ALLOWED_MONUMENT_DESCRIPTORS
                ):
                    return False, False, None
                if set(entity_tokens).issubset(set(cand_tokens)):
                    title_matches = True

    excerpt = (obs.excerpt_text or "").strip()
    excerpt_lower = excerpt.lower()
    excerpt_matches = False
    if not title_matches and not entity_norm and excerpt:
        # Check text-only observations when no structured title is present
        if cand_norm in excerpt_lower or cand_name.lower() in excerpt_lower:
            if not _is_casual_mention(excerpt):
                cand_lower = cand_norm
                idx = excerpt_lower.find(cand_lower)
                trailing_text = excerpt_lower[idx + len(cand_lower) : idx + len(cand_lower) + 30]
                trailing_words = set(re.findall(r"\b\w+\b", trailing_text))
                if not (trailing_words & DISTINCT_ENTITY_MODIFIERS):
                    excerpt_matches = True

    name_matches = title_matches or excerpt_matches
    if not name_matches:
        # Metadata or title describes a different entity
        return False, False, None

    # Spatial verification against candidate location
    obs_coords = _extract_observation_coordinates(obs)
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

    # 2. Ingest existence claims for matching candidates
    for candidate in candidates:
        cand_name = candidate.primary_name.strip()
        cand_name_norm = cand_name.lower()
        if not cand_name_norm:
            continue

        # Distinguish local dishes from physical businesses
        from functions.orchestrator.evidence.place_extraction import PlaceExtractionService

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
            is_match, is_closed, coords = _match_observation_entity(obs, candidate)
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
                logger.warning("Failed submitting active claim for candidate '%s': %s", cand_name, exc)

        # Conflicting evidence: if supporting observations coexist with refuting observations,
        # ingest closed claim so policy preserves the conflict honestly as CONFLICTED
        if supporting_obs and refuting_obs and "closed" not in existing_statuses:
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

    # 3. Ingest accommodation observations, strictly preserving INDICATIVE_SEARCH
    for obs in valid_observations:
        art_ref = obs.artifact_ref or {}
        cat = str(art_ref.get("category") or "").lower() if isinstance(art_ref, dict) else ""
        is_stay = "lodging" in cat or "hotel" in cat or "stay" in cat
        if not is_stay and isinstance(art_ref, dict):
            t = str(art_ref.get("property_name") or art_ref.get("title") or "").lower()
            if any(
                w in t
                for w in ("hotel", "resort", "homestay", "lodge", "inn", "hostel")
            ):
                is_stay = True
        if is_stay:
            hotel_name = str(
                art_ref.get("property_name")
                or art_ref.get("name")
                or art_ref.get("title")
                or ""
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
            raw_price = (
                art_ref.get("price_per_night")
                if isinstance(art_ref, dict)
                else None
            )
            if raw_price is None and isinstance(art_ref, dict):
                raw_price = art_ref.get("indicative_price_inr")
            currency_val = art_ref.get("currency") if isinstance(art_ref, dict) else None
            obs_url = str(getattr(obs, "canonical_url", None) or getattr(obs, "source_url", None) or "").strip()
            has_usable_url = bool(obs_url and (obs_url.startswith("http://") or obs_url.startswith("https://")))
            has_valid_indicative_price = raw_price is not None and bool(currency_val) and has_usable_url

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
                art_ref.get("headline")
                or art_ref.get("title")
                or art_ref.get("summary")
                or ""
            ).strip()
            if isinstance(art_ref, dict)
            else ""
        )

        is_advisory = any(
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
            str(art_ref.get("advisory_type") or "").upper()
            if isinstance(art_ref, dict)
            else ""
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

        start_time_val = (
            art_ref.get("applicable_start") if isinstance(art_ref, dict) else None
        )
        end_time_val = (
            art_ref.get("applicable_end") if isinstance(art_ref, dict) else None
        )

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
            "RESTRICTION"
            if adv_type in ("ROAD_CLOSURE", "TRAVEL_RESTRICTION")
            else "WEATHER"
        )

        existing_adv_stmt = select(ClaimModel).where(
            ClaimModel.execution_id == execution_id,
            ClaimModel.subject_id == subject_id,
            ClaimModel.category == claim_category,
        )
        if (await session.execute(existing_adv_stmt)).scalar_one_or_none() is not None:
            continue

        adv_coords = (
            _extract_coordinates_from_dict(art_ref) if isinstance(art_ref, dict) else None
        )

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
                observations_with_support=[
                    (obs, EvidenceSupportType.SUPPORTS, obs.canonical_url)
                ],
                as_of=now,
            )
            ingested_claims.append(created)
        except Exception as adv_exc:
            logger.warning("Failed submitting advisory claim for '%s': %s", subject_id, adv_exc)

    return ingested_claims
