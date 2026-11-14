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


def _extract_coordinates_from_dict(data: dict[str, Any] | None) -> tuple[float, float] | None:
    """Safely extracts latitude and longitude from dictionary if present."""
    if not data or not isinstance(data, dict):
        return None
    for lat_key, lng_key in (("lat", "lng"), ("latitude", "longitude")):
        if lat_key in data and lng_key in data:
            try:
                lat = float(data[lat_key])
                lng = float(data[lng_key])
                if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
                    return lat, lng
            except (ValueError, TypeError):
                pass
    return None


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
    - Matching is based on canonical name presence or spatial proximity within 1.0 km.
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

        # Find matching observations
        matched_obs: list[SourceObservationModel] = []
        for obs in valid_observations:
            excerpt_lower = (obs.excerpt_text or "").lower()
            url_lower = (obs.canonical_url or obs.source_url or "").lower()
            publisher_lower = (obs.publisher or "").lower()

            name_matched = (
                cand_name_norm in excerpt_lower
                or cand_name_norm in url_lower
                or cand_name_norm in publisher_lower
            )

            spatial_matched = False
            if candidate.point is not None:
                obs_coords = _extract_coordinates_from_dict(obs.artifact_ref)
                if obs_coords is not None:
                    dist = haversine_distance_km(
                        candidate.point.lat,
                        candidate.point.lng,
                        obs_coords[0],
                        obs_coords[1],
                    )
                    if dist <= 1.0:
                        spatial_matched = True

            if name_matched or spatial_matched:
                matched_obs.append(obs)

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
                "lat": candidate.point.lat,
                "lng": candidate.point.lng,
            }
            if candidate.point
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
