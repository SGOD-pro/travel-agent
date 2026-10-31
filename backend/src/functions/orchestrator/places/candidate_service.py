"""Candidate registration service coordinating spatial corridor filtering and canonical place resolution.

Conforms to spec 0007 AC-3:
- Registers LLM proposals as candidate places in canonical_places table with is_candidate=True.
- Labels provenance with uncertainty EDITORIAL_DISCOVERY.
- Enforces 100km corridor boundary filtering, discarding out of bounds or malformed proposals.
- Resolves candidates against existing canonical places via provider_id and spatial name proximity,
  guaranteeing idempotency and deduplication on workflow replays.
- Invariant: Candidate existence remains strictly UNVERIFIED upon registration.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from contracts.candidate import (
    CandidateCorridorResult,
    CandidateExistenceStatus,
    CandidateHoursStatus,
    CorridorValidationStatus,
    RegisteredCandidate,
)
from contracts.llm import PlaceProposal
from functions.orchestrator.places.corridor import (
    DEFAULT_MAX_CORRIDOR_DISTANCE_KM,
    validate_candidate_corridor,
)
from functions.orchestrator.places.repository import CanonicalPlacesRepository
from functions.orchestrator.places.schemas import (
    AccessStatus,
    CanonicalPlaceCreate,
    CoordinatePoint,
)
from functions.orchestrator.places.service import compute_name_similarity

OFFLINE_NAMESPACE_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")


class CandidateRegistrationService:
    """Service handling candidate corridor validation, deduplication, and canonical persistence."""

    def __init__(
        self,
        session: AsyncSession | None = None,
        max_corridor_distance_km: float = DEFAULT_MAX_CORRIDOR_DISTANCE_KM,
    ) -> None:
        self._session = session
        self._repo = CanonicalPlacesRepository(session) if session is not None else None
        self._max_distance_km = max_corridor_distance_km
        self._memory_cache: dict[str, RegisteredCandidate] = {}

    async def register_candidate_proposals(
        self,
        proposals: list[PlaceProposal],
        corridor_points: list[CoordinatePoint],
        execution_id: uuid.UUID | None = None,
        owner_id: uuid.UUID | None = None,
    ) -> tuple[list[RegisteredCandidate], list[RegisteredCandidate]]:
        """Validates proposals against corridor and registers eligible candidates in canonical places.

        Returns a tuple of:
        - valid_candidates: registered candidates within the corridor (existence remains UNVERIFIED).
        - excluded_candidates: proposals rejected due to malformed coordinates or exceeding 100km corridor.
        """
        valid_candidates: list[RegisteredCandidate] = []
        excluded_candidates: list[RegisteredCandidate] = []

        for proposal in proposals:
            corridor_result = validate_candidate_corridor(
                proposal,
                corridor_points,
                max_distance_km=self._max_distance_km,
            )

            if corridor_result.status != CorridorValidationStatus.WITHIN_CORRIDOR:
                excluded_candidate = self._create_excluded_candidate(
                    proposal, corridor_result, execution_id, owner_id
                )
                excluded_candidates.append(excluded_candidate)
                continue

            registered = await self._register_or_resolve_candidate(
                proposal, corridor_result, execution_id, owner_id
            )
            valid_candidates.append(registered)

        return valid_candidates, excluded_candidates

    async def _register_or_resolve_candidate(
        self,
        proposal: PlaceProposal,
        corridor_result: CandidateCorridorResult,
        execution_id: uuid.UUID | None,
        owner_id: uuid.UUID | None,
    ) -> RegisteredCandidate:
        """Resolves proposal against existing canonical place or creates a new candidate record."""
        point = CoordinatePoint(
            lat=proposal.estimated_latitude,
            lng=proposal.estimated_longitude,
        )

        resolved_place_id: uuid.UUID | None = None

        if self._repo is not None and self._session is not None:
            # 1. Spatial proximity and name similarity match within 100m
            # Places with the same name far apart remain distinct;
            # Replay of the same candidate within 100m resolves to the existing record.
            existing_place = None
            nearby = await self._repo.find_candidates_by_proximity(
                point,
                max_distance_meters=100.0,
            )
            for candidate_model, _ in nearby:
                cand_names = [candidate_model.primary_name] + list(candidate_model.names or [])
                sim = max(compute_name_similarity(proposal.name, cn) for cn in cand_names)
                if sim >= 0.8:
                    existing_place = candidate_model
                    break

            if existing_place is not None:
                resolved_place_id = existing_place.id
            else:
                # 2. Create new canonical candidate place
                new_id = uuid.uuid4()
                create_data = CanonicalPlaceCreate(
                    id=new_id,
                    primary_name=proposal.name,
                    locality=proposal.address or "Corridor Destination",
                    point=point,
                    names=[proposal.name],
                    access_status=AccessStatus.UNKNOWN,
                    is_candidate=True,
                    geocoder_provenance="EDITORIAL_DISCOVERY",
                    provider_ids={
                        "category": proposal.category,
                    },
                )
                created_model = await self._repo.create_place(create_data)
                resolved_place_id = created_model.id
        else:
            # In-memory deterministic resolution for offline execution
            cache_key = f"{proposal.name.strip().lower()}_{point.lat:.4f}_{point.lng:.4f}"
            if cache_key in self._memory_cache:
                return self._memory_cache[cache_key]

            resolved_place_id = uuid.uuid5(
                OFFLINE_NAMESPACE_UUID,
                f"{proposal.name}_{point.lat:.4f}_{point.lng:.4f}",
            )

        registered = RegisteredCandidate(
            place_id=resolved_place_id,
            primary_name=proposal.name,
            category=proposal.category,
            point=point,
            uncertainty="EDITORIAL_DISCOVERY",
            is_candidate=True,
            corridor_distance_km=corridor_result.distance_to_corridor_km,
            existence_status=CandidateExistenceStatus.UNVERIFIED,
            hours_status=CandidateHoursStatus.UNKNOWN,
            suggested_stay_minutes=proposal.suggested_stay_minutes,
            price_estimate_inr=proposal.price_estimate_inr,
            diagnostics={
                "corridor_status": corridor_result.status.value,
                "corridor_distance_km": corridor_result.distance_to_corridor_km,
                "corridor_note": corridor_result.diagnostic,
                "provenance_note": "Candidate registered with editorial uncertainty; existence requires corroborating evidence",
            },
            provenance={
                "source": "llm_proposal",
                "execution_id": str(execution_id) if execution_id else None,
                "owner_id": str(owner_id) if owner_id else None,
                "brief_corridor_validated": True,
            },
        )

        if self._repo is None:
            cache_key = f"{proposal.name.strip().lower()}_{point.lat:.4f}_{point.lng:.4f}"
            self._memory_cache[cache_key] = registered

        return registered

    def _create_excluded_candidate(
        self,
        proposal: PlaceProposal,
        corridor_result: CandidateCorridorResult,
        execution_id: uuid.UUID | None,
        owner_id: uuid.UUID | None,
    ) -> RegisteredCandidate:
        """Constructs an excluded candidate record with explanatory diagnostics."""
        lat = proposal.estimated_latitude if -90.0 <= proposal.estimated_latitude <= 90.0 else 0.0
        lng = (
            proposal.estimated_longitude if -180.0 <= proposal.estimated_longitude <= 180.0 else 0.0
        )

        return RegisteredCandidate(
            place_id=None,
            primary_name=proposal.name,
            category=proposal.category,
            point=CoordinatePoint(lat=lat, lng=lng),
            uncertainty="EDITORIAL_DISCOVERY",
            is_candidate=True,
            corridor_distance_km=corridor_result.distance_to_corridor_km,
            existence_status=CandidateExistenceStatus.UNVERIFIED,
            hours_status=CandidateHoursStatus.UNKNOWN,
            suggested_stay_minutes=proposal.suggested_stay_minutes,
            price_estimate_inr=proposal.price_estimate_inr,
            diagnostics={
                "corridor_status": corridor_result.status.value,
                "rejection_reason": corridor_result.diagnostic,
            },
            provenance={
                "source": "llm_proposal",
                "execution_id": str(execution_id) if execution_id else None,
                "owner_id": str(owner_id) if owner_id else None,
                "brief_corridor_validated": False,
            },
        )
