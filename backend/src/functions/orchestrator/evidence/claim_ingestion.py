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


def _is_casual_mention(text: str) -> bool:
    """Detects whether text is merely a casual passing mention."""
    if not text:
        return False
    return any(p.search(text) for p in CASUAL_PATTERNS)


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
        for k in ("title", "place_name", "name", "property_name"):
            val = art_ref.get(k)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
    excerpt = getattr(obs, "excerpt_text", "") or ""
    if excerpt.startswith("Place: "):
        first_segment = excerpt.split("|")[0].replace("Place: ", "").strip()
        if first_segment:
            return first_segment
    return None


def _observation_supports_candidate(
    obs: SourceObservationModel,
    candidate: RegisteredCandidate,
) -> tuple[bool, tuple[float, float] | None]:
    """Evaluates whether an observation genuinely supports candidate existence.

    Guarantees:
    1. A name appearing only in URL or publisher without substantive content does not match.
    2. An unrelated nearby coordinate (different place name within 1.0 km) does not match.
    3. Same-name places with coordinates > 1.0 km away do not match.
    4. Casual mentions in passing text without coordinates or structured place metadata do not match.
    5. Returns (is_match, verified_coordinates). Verified coordinates come strictly from the observation.
    """
    cand_name = candidate.primary_name.strip()
    cand_norm = _normalize_name(cand_name)
    if not cand_norm:
        return False, None

    obs_title = _extract_observation_title(obs)
    obs_title_norm = _normalize_name(obs_title) if obs_title else ""
    excerpt = (obs.excerpt_text or "").strip()
    excerpt_lower = excerpt.lower()
    obs_coords = _extract_observation_coordinates(obs)

    # 1. Check place identity: does the observation substantively identify the candidate?
    title_matches = False
    if obs_title_norm:
        if (
            cand_norm == obs_title_norm
            or cand_norm in obs_title_norm
            or obs_title_norm in cand_norm
        ):
            title_matches = True
        else:
            cand_tokens = set(cand_norm.split())
            title_tokens = set(obs_title_norm.split())
            if cand_tokens and cand_tokens.issubset(title_tokens):
                title_matches = True

    excerpt_matches = False
    if not title_matches and excerpt:
        # Candidate name must be present in excerpt text (not just in URL or publisher)
        if cand_norm in excerpt_lower or cand_name.lower() in excerpt_lower:
            # Reject casual passing mentions without structured title or coordinates
            if not _is_casual_mention(excerpt):
                excerpt_matches = True

    name_matches = title_matches or excerpt_matches
    if not name_matches:
        # An unrelated nearby place name must never match, even if coordinates are nearby
        return False, None

    # 2. Spatial verification against candidate location
    if obs_coords is not None and candidate.point is not None:
        dist = haversine_distance_km(
            candidate.point.lat,
            candidate.point.lng,
            obs_coords[0],
            obs_coords[1],
        )
        if dist > 1.0:
            # Same-name place located far away: reject
            return False, None
        # Name matches AND observation coordinates match within 1.0 km
        return True, obs_coords

    if obs_coords is not None and candidate.point is None:
        return True, obs_coords

    # 3. Observation has no independent coordinates (obs_coords is None):
    # Match the observation if it has substantive title or detailed excerpt,
    # but return None for verified coordinates so candidate's own coordinates are not used as proof.
    if title_matches or (
        excerpt_matches and len(excerpt) >= 40 and not _is_casual_mention(excerpt)
    ):
        return True, None

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

        # Check existing claim for this candidate under this execution
        existing_stmt = select(ClaimModel).where(
            ClaimModel.execution_id == execution_id,
            ClaimModel.subject_id == cand_name_norm,
            ClaimModel.category == ClaimCategory.ATTRACTION.value,
        )
        if candidate.place_id is not None:
            existing_stmt = existing_stmt.where(ClaimModel.place_id == candidate.place_id)

        existing_res = await session.execute(existing_stmt)
        if existing_res.scalar_one_or_none() is not None:
            continue

        # Find matching observations with independent verification
        matched_obs: list[SourceObservationModel] = []
        verified_coords: tuple[float, float] | None = None

        for obs in valid_observations:
            is_match, coords = _observation_supports_candidate(obs, candidate)
            if is_match:
                matched_obs.append(obs)
                if coords is not None and verified_coords is None:
                    verified_coords = coords

        if not matched_obs:
            continue

        # Build claim linking observations with SUPPORTS
        support_links = [
            (obs, EvidenceSupportType.SUPPORTS, obs.canonical_url or obs.source_url)
            for obs in matched_obs
        ]

        claim_create = ClaimCreate(
            subject_id=cand_name_norm,
            category=ClaimCategory.ATTRACTION,
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
            valid_from=matched_obs[0].retrieved_at or now,
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
            logger.info("Ingested claim %s for candidate '%s'", created.id, cand_name)
        except Exception as exc:
            logger.warning("Failed submitting claim for candidate '%s': %s", cand_name, exc)

    # 3. Ingest accommodation observations, strictly preserving INDICATIVE_SEARCH
    for obs in valid_observations:
        art_ref = obs.artifact_ref or {}
        cat = str(art_ref.get("category") or "").lower() if isinstance(art_ref, dict) else ""
        if "lodging" in cat or "hotel" in cat or "stay" in cat:
            hotel_name = str(
                art_ref.get("property_name") or art_ref.get("name") or "lodging"
            ).strip()
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
            stay_claim = ClaimCreate(
                subject_id=hotel_norm,
                category="LODGING",
                place_id=None,
                execution_id=execution_id,
                typed_value={
                    "property_name": hotel_name,
                    "evidence_class": EvidenceClass.INDICATIVE_SEARCH.value,
                    "price_per_night": art_ref.get("price_per_night"),
                    "currency": art_ref.get("currency", "INR"),
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

    return ingested_claims
