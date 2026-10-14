"""Place resolution service conforming to spec 0002 AC-7.

Resolves candidate place observations against existing canonical_places records
using spatial proximity within 100 meters and normalized name similarity.
Flags ambiguous places (e.g. same name in different localities) without destructive merging.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from functions.orchestrator.places.models import CanonicalPlaceModel
from functions.orchestrator.places.repository import CanonicalPlacesRepository
from functions.orchestrator.places.schemas import (
    AccessStatus,
    CanonicalPlaceCreate,
    CanonicalPlaceRead,
    CoordinatePoint,
)

# Standard name similarity threshold per spec 0002 invariant 7
DEFAULT_NAME_SIMILARITY_THRESHOLD: float = 0.8
# Proximity boundary in meters per AC-7
DEFAULT_SPATIAL_PROXIMITY_METERS: float = 100.0


class ResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NEW_CANDIDATE = "NEW_CANDIDATE"


def compute_name_similarity(name1: str, name2: str) -> float:
    """Computes normalized token and sequence similarity between two place names.

    Conforms to spec 0002 invariant 7: combines normalized token similarity in Python
    without requiring external database extensions. Returns score between 0.0 and 1.0.
    """
    n1 = re.sub(r"[^\w\s]", "", name1.lower()).strip()
    n2 = re.sub(r"[^\w\s]", "", name2.lower()).strip()
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0

    t1 = set(n1.split())
    t2 = set(n2.split())
    if not t1 or not t2:
        return 0.0

    matched = 0
    t2_remaining = set(t2)
    for w1 in t1:
        if w1 in t2_remaining:
            matched += 1
            t2_remaining.remove(w1)
        else:
            for w2 in list(t2_remaining):
                if difflib.SequenceMatcher(None, w1, w2).ratio() >= 0.8:
                    matched += 1
                    t2_remaining.remove(w2)
                    break

    if matched == 0:
        return 0.0

    dice = (2.0 * matched) / (len(t1) + len(t2))
    min_tokens = min(len(t1), len(t2))
    containment = matched / min_tokens

    # Only boost containment if both names have at least two tokens and Dice is reasonable
    if min_tokens >= 2 and containment >= 1.0 and dice >= 0.6:
        return max(dice, 0.85)
    return dice



@dataclass(frozen=True)
class CandidatePlaceInput:
    """Incoming place candidate for resolution."""

    primary_name: str
    locality: str
    point: CoordinatePoint
    names: list[str] = field(default_factory=list)
    access_points: list[dict[str, Any]] = field(default_factory=list)
    provider_ids: dict[str, Any] = field(default_factory=dict)
    geocoder_provenance: str | None = None
    access_status: AccessStatus = AccessStatus.UNKNOWN


@dataclass(frozen=True)
class PlaceResolutionResult:
    """Result of candidate place resolution."""

    status: ResolutionStatus
    place: CanonicalPlaceRead | None
    candidates: list[CanonicalPlaceRead] = field(default_factory=list)
    confidence: float | None = None
    distance_meters: float | None = None
    reason: str = ""


class PlaceResolutionService:
    """Service resolving place observations against canonical places (AC-7)."""

    def __init__(
        self,
        repository: CanonicalPlacesRepository,
        *,
        proximity_meters: float = DEFAULT_SPATIAL_PROXIMITY_METERS,
        similarity_threshold: float = DEFAULT_NAME_SIMILARITY_THRESHOLD,
    ) -> None:
        self._repo = repository
        self._proximity_meters = proximity_meters
        self._similarity_threshold = similarity_threshold

    async def resolve_candidate(
        self,
        candidate: CandidatePlaceInput,
        *,
        auto_create: bool = True,
    ) -> PlaceResolutionResult:
        """Resolves candidate place using spatial proximity and name similarity.

        1. Checks for authoritative external provider ID match.
        2. Queries places within 100 meters proximity.
        3. Matches name similarity against candidate name and aliases (threshold >= 0.8).
        4. Detects ambiguous places sharing identical or similar names in different localities,
           flagging for clarification without destructive entity merging (AC-7).
        5. If no matches exist, creates a new candidate place record.
        """
        # 1. Authoritative provider identifier match
        for p_key, p_val in candidate.provider_ids.items():
            if p_val:
                existing = await self._repo.find_by_provider_id(p_key, str(p_val))
                if existing:
                    read_place = CanonicalPlaceRead.model_validate(existing)
                    return PlaceResolutionResult(
                        status=ResolutionStatus.RESOLVED,
                        place=read_place,
                        confidence=1.0,
                        reason=f"Matched existing place via provider_id ({p_key}={p_val})",
                    )

        # 2. Query places within spatial proximity threshold (100m)
        nearby = await self._repo.find_candidates_by_proximity(
            candidate.point,
            max_distance_meters=self._proximity_meters,
        )

        cand_names = [candidate.primary_name] + list(candidate.names)
        spatial_matches: list[tuple[CanonicalPlaceModel, float, float]] = []

        for place, dist in nearby:
            place_names = [place.primary_name] + list(place.names)
            sim = max(
                compute_name_similarity(cn, pn)
                for cn in cand_names
                for pn in place_names
            )
            if sim >= self._similarity_threshold:
                spatial_matches.append((place, dist, sim))

        # 3. Check for ambiguous places across primary and alias names (AC-7)
        names_to_search = {candidate.primary_name.strip()} | {
            n.strip() for n in candidate.names if n.strip()
        }
        seen_cand_ids: set[Any] = {sm[0].id for sm in spatial_matches}
        name_candidates: list[CanonicalPlaceModel] = []
        for search_term in names_to_search:
            matches = await self._repo.search_by_name(search_term)
            for m in matches:
                if m.id not in seen_cand_ids:
                    seen_cand_ids.add(m.id)
                    name_candidates.append(m)

        ambiguous_name_places: list[tuple[CanonicalPlaceModel, float]] = []

        for p in name_candidates:
            p_names = [p.primary_name] + list(p.names)
            sim = max(
                compute_name_similarity(cn, pn)
                for cn in cand_names
                for pn in p_names
            )
            if sim >= self._similarity_threshold:
                ambiguous_name_places.append((p, sim))

        # 4. Evaluate outcomes
        # Case A: Multiple places within 100m matching name threshold
        if len(spatial_matches) > 1:
            return PlaceResolutionResult(
                status=ResolutionStatus.AMBIGUOUS,
                place=None,
                candidates=[CanonicalPlaceRead.model_validate(sm[0]) for sm in spatial_matches],
                confidence=max(sm[2] for sm in spatial_matches),
                reason=(
                    f"Found {len(spatial_matches)} candidate places within "
                    f"{self._proximity_meters}m matching name similarity threshold"
                ),
            )

        # Case B: Exactly one place within 100m matching name threshold
        if len(spatial_matches) == 1:
            best_place, dist, sim = spatial_matches[0]
            read_place = CanonicalPlaceRead.model_validate(best_place)
            return PlaceResolutionResult(
                status=ResolutionStatus.RESOLVED,
                place=read_place,
                confidence=sim,
                distance_meters=dist,
                reason=(
                    f"Resolved to existing canonical place within {dist:.1f}m "
                    f"(name similarity {sim:.2f})"
                ),
            )

        # Case C: Same name exists outside 100m proximity (AC-7)
        if ambiguous_name_places:
            cand_models = [ap[0] for ap in ambiguous_name_places]
            read_cands = [CanonicalPlaceRead.model_validate(p) for p in cand_models]

            new_candidate_place: CanonicalPlaceRead | None = None
            if auto_create:
                create_payload = CanonicalPlaceCreate(
                    primary_name=candidate.primary_name,
                    locality=candidate.locality,
                    point=candidate.point,
                    names=candidate.names,
                    access_points=candidate.access_points,
                    provider_ids=candidate.provider_ids,
                    geocoder_provenance=candidate.geocoder_provenance,
                    access_status=candidate.access_status,
                    is_candidate=True,
                )
                created_model = await self._repo.create_place(create_payload)
                new_candidate_place = CanonicalPlaceRead.model_validate(created_model)

            diff_localities = [p for p in cand_models if p.locality != candidate.locality]
            if diff_localities:
                loc_summary = ", ".join(repr(p.locality) for p in diff_localities)
                locality_clause = f"in different localities ({loc_summary})"
            else:
                loc_summary = ", ".join(repr(p.locality) for p in cand_models)
                locality_clause = f"outside proximity threshold ({loc_summary})"

            return PlaceResolutionResult(
                status=ResolutionStatus.AMBIGUOUS,
                place=new_candidate_place,
                candidates=read_cands,
                confidence=max(ap[1] for ap in ambiguous_name_places),
                reason=(
                    f"Ambiguous place: '{candidate.primary_name}' in '{candidate.locality}' shares name "
                    f"with existing places {locality_clause}. "
                    f"Flagged for clarification without destructive entity merging."
                ),
            )

        # Case D: New candidate place
        new_place: CanonicalPlaceRead | None = None
        if auto_create:
            create_payload = CanonicalPlaceCreate(
                primary_name=candidate.primary_name,
                locality=candidate.locality,
                point=candidate.point,
                names=candidate.names,
                access_points=candidate.access_points,
                provider_ids=candidate.provider_ids,
                geocoder_provenance=candidate.geocoder_provenance,
                access_status=candidate.access_status,
                is_candidate=True,
            )
            created_model = await self._repo.create_place(create_payload)
            new_place = CanonicalPlaceRead.model_validate(created_model)

        return PlaceResolutionResult(
            status=ResolutionStatus.NEW_CANDIDATE,
            place=new_place,
            confidence=1.0,
            reason="No matching existing place found; created new candidate place",
        )
