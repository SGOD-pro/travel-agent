"""Deterministic evidence eligibility gate evaluating candidate existence and operating hours.

Conforms strictly to spec 0007 AC-3 and requirements:
1. Treat LLM output as editorial candidate proposals, never verified facts.
2. Unverified or contradicted places must not enter the route solver.
3. Candidate places without verified existence are preserved as editorial recommendations.
4. Places with verified existence but unknown opening hours use an explicitly labeled
   HOURS_ASSUMED window (09:00 to 18:00). They are never presented as verified.
5. Contradicted places (refuting observations or conflicting claims) are excluded entirely.
6. Preserves stale, conflicting, unavailable, and missing evidence states honestly.
7. Enforces strict tenant and execution isolation on evidence queries.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from contracts.candidate import (
    CandidateEligibilityReport,
    CandidateExistenceStatus,
    CandidateHoursStatus,
    RegisteredCandidate,
)
from functions.orchestrator.evidence.models import ClaimEvidenceModel, ClaimModel
from functions.orchestrator.evidence.schemas import (
    AvailabilityStatus,
    ClaimCategory,
    EvidenceSupportType,
    FreshnessStatus,
    VerificationStatus,
)
from functions.orchestrator.places.corridor import (
    haversine_distance_km,
    validate_coordinates,
)

logger = logging.getLogger(__name__)

DEFAULT_ASSUMED_WINDOW_START: time = time(9, 0)
DEFAULT_ASSUMED_WINDOW_END: time = time(18, 0)


def parse_time_str(val: Any) -> time | None:
    """Parses time string in HH:MM or HH:MM:SS format into datetime.time object."""
    if not val or not isinstance(val, str):
        return None
    val_clean = val.strip()
    try:
        parts = val_clean.split(":")
        if len(parts) >= 2:
            hour = int(parts[0])
            minute = int(parts[1])
            second = int(parts[2]) if len(parts) > 2 else 0
            if 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59:
                return time(hour=hour, minute=minute, second=second)
    except Exception:
        return None
    return None


class DeterministicEvidenceEligibilityGate:
    """Evaluates candidate places against source observations and claims to determine solver eligibility."""

    def __init__(
        self,
        session: AsyncSession | None = None,
        as_of: datetime | None = None,
    ) -> None:
        self._session = session
        self._as_of = as_of or datetime.now(UTC)

    async def evaluate_candidate_eligibility(
        self,
        candidates: list[RegisteredCandidate],
        execution_id: uuid.UUID | None = None,
        owner_id: uuid.UUID | None = None,
        fixture_observations: list[Any] | None = None,
        fixture_claims: list[Any] | None = None,
    ) -> CandidateEligibilityReport:
        """Evaluates each candidate place against evidence observations and claims.

        Partitions candidates into:
        - eligible_stops: places with verified existence, ready for route solver.
        - editorial_recommendations: unverified candidates within corridor, retained for traveler view.
        - excluded_candidates: contradicted places or out of corridor candidates, excluded entirely.
        """
        eligible_stops: list[RegisteredCandidate] = []
        editorial_recommendations: list[RegisteredCandidate] = []
        excluded_candidates: list[RegisteredCandidate] = []

        # Enforce owner-to-execution authorization at the evidence boundary
        if self._session is not None and execution_id is not None and owner_id is not None:
            from functions.orchestrator.execution.models import ExecutionModel

            exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id)
            exec_res = await self._session.execute(exec_stmt)
            execution_record = exec_res.scalar_one_or_none()
            if execution_record is not None and execution_record.owner_id != owner_id:
                raise PermissionError(
                    f"Execution authorization failed: execution {execution_id} belongs to owner {execution_record.owner_id}, caller is {owner_id}"
                )

        if execution_id is not None and owner_id is not None:
            # Check in-memory / fixture items for ownership mismatch
            for item in (fixture_observations or []) + (fixture_claims or []):
                item_exec = getattr(item, "execution_id", None) or (
                    item.get("execution_id") if isinstance(item, dict) else None
                )
                item_owner = getattr(item, "owner_id", None) or (
                    item.get("owner_id") if isinstance(item, dict) else None
                )
                if (
                    item_exec
                    and str(item_exec) == str(execution_id)
                    and item_owner
                    and str(item_owner) != str(owner_id)
                ):
                    raise PermissionError(
                        f"Execution authorization failed: fixture execution {execution_id} belongs to owner {item_owner}, caller is {owner_id}"
                    )

        for candidate in candidates:
            # 1. Retrieve evidence for this candidate
            obs_list, claims_list = await self._retrieve_evidence_for_candidate(
                candidate=candidate,
                execution_id=execution_id,
                owner_id=owner_id,
                fixture_observations=fixture_observations,
                fixture_claims=fixture_claims,
            )

            # 2. Evaluate existence status
            existence_status, existence_diag = self._evaluate_existence(
                candidate=candidate,
                observations=obs_list,
                claims=claims_list,
            )

            # 3. Partition based on existence outcome
            if existence_status == CandidateExistenceStatus.CONTRADICTED:
                updated_diag = dict(candidate.diagnostics)
                updated_diag.update(existence_diag)
                excluded_candidate = candidate.model_copy(
                    update={
                        "existence_status": CandidateExistenceStatus.CONTRADICTED,
                        "diagnostics": updated_diag,
                    }
                )
                excluded_candidates.append(excluded_candidate)
                continue

            if existence_status == CandidateExistenceStatus.UNVERIFIED:
                updated_diag = dict(candidate.diagnostics)
                updated_diag.update(existence_diag)
                editorial_cand = candidate.model_copy(
                    update={
                        "existence_status": CandidateExistenceStatus.UNVERIFIED,
                        "uncertainty": "EDITORIAL_DISCOVERY",
                        "is_candidate": True,
                        "diagnostics": updated_diag,
                    }
                )
                editorial_recommendations.append(editorial_cand)
                continue

            # 4. For VERIFIED existence places, evaluate operating hours
            hours_status, start_t, end_t, is_assumed, hours_diag = self._evaluate_hours(
                candidate=candidate,
                claims=claims_list,
            )

            updated_diag = dict(candidate.diagnostics)
            updated_diag.update(existence_diag)
            updated_diag.update(hours_diag)

            eligible_cand = candidate.model_copy(
                update={
                    "existence_status": CandidateExistenceStatus.VERIFIED,
                    "hours_status": hours_status,
                    "window_start": start_t,
                    "window_end": end_t,
                    "is_hours_assumed": is_assumed,
                    "diagnostics": updated_diag,
                }
            )
            eligible_stops.append(eligible_cand)

        summary = {
            "total_candidates": len(candidates),
            "eligible_stops": len(eligible_stops),
            "hours_assumed_count": sum(1 for c in eligible_stops if c.is_hours_assumed),
            "verified_hours_count": sum(1 for c in eligible_stops if not c.is_hours_assumed),
            "editorial_recommendations": len(editorial_recommendations),
            "excluded_candidates": len(excluded_candidates),
        }

        return CandidateEligibilityReport(
            eligible_stops=eligible_stops,
            editorial_recommendations=editorial_recommendations,
            excluded_candidates=excluded_candidates,
            summary=summary,
        )

    @staticmethod
    def _extract_coordinates_from_data(data: Any) -> tuple[float, float] | None:
        """Extracts latitude and longitude from dictionary, object, or sequence if present.

        Rejects missing, malformed, non-finite, or out-of-bounds coordinates cleanly.
        """
        if data is None:
            return None

        # Sequence of coordinates [lat, lng] or GeoJSON [lng, lat]
        if isinstance(data, (list, tuple)) and len(data) >= 2:
            try:
                if isinstance(data[0], bool) or isinstance(data[1], bool):
                    return None
                c0 = float(data[0])
                c1 = float(data[1])
                valid_lat_first, _ = validate_coordinates(c0, c1)
                if valid_lat_first:
                    return c0, c1
                valid_lng_first, _ = validate_coordinates(c1, c0)
                if valid_lng_first:
                    return c1, c0
            except (ValueError, TypeError):
                return None
            return None

        if not isinstance(data, dict):
            # Check object attributes
            lat_attr = getattr(data, "lat", None)
            if lat_attr is None:
                lat_attr = getattr(data, "latitude", None)
            lng_attr = getattr(data, "lng", None)
            if lng_attr is None:
                lng_attr = getattr(data, "longitude", None)
            if lng_attr is None:
                lng_attr = getattr(data, "lon", None)
            if lat_attr is not None and lng_attr is not None:
                try:
                    if isinstance(lat_attr, bool) or isinstance(lng_attr, bool):
                        return None
                    f_lat = float(lat_attr)
                    f_lng = float(lng_attr)
                    valid, _ = validate_coordinates(f_lat, f_lng)
                    if valid:
                        return f_lat, f_lng
                except (ValueError, TypeError):
                    pass
            return None

        # Direct dictionary lat/lng keys
        lat = data.get("lat") if data.get("lat") is not None else data.get("latitude")
        lng = (
            data.get("lng")
            if data.get("lng") is not None
            else (data.get("longitude") if data.get("longitude") is not None else data.get("lon"))
        )
        if lat is not None and lng is not None:
            try:
                if isinstance(lat, bool) or isinstance(lng, bool):
                    return None
                f_lat = float(lat)
                f_lng = float(lng)
                valid, _ = validate_coordinates(f_lat, f_lng)
                if valid:
                    return f_lat, f_lng
            except (ValueError, TypeError):
                pass

        # Sub-dictionaries: gps, location, geo, point
        for key in ("gps", "location", "geo", "point"):
            nested = data.get(key)
            if nested is not None:
                coords = DeterministicEvidenceEligibilityGate._extract_coordinates_from_data(nested)
                if coords is not None:
                    return coords

        # GeoJSON / coordinates sequence in dict
        coords_seq = data.get("coordinates")
        if isinstance(coords_seq, (list, tuple)) and len(coords_seq) >= 2:
            try:
                if isinstance(coords_seq[0], bool) or isinstance(coords_seq[1], bool):
                    return None
                c0 = float(coords_seq[0])
                c1 = float(coords_seq[1])
                # GeoJSON standard: [longitude, latitude]
                valid_geojson, _ = validate_coordinates(c1, c0)
                if valid_geojson:
                    return c1, c0
                valid_lat_first, _ = validate_coordinates(c0, c1)
                if valid_lat_first:
                    return c0, c1
            except (ValueError, TypeError):
                pass

        return None

    def _claim_matches_candidate_location(
        self,
        claim: Any,
        candidate: RegisteredCandidate,
    ) -> bool:
        """Determines whether a claim has an established identity/location match to candidate.

        Enforces:
        1. If the claim is explicitly linked to another place ID, it must never match this candidate.
        2. Direct canonical place ID match is preserved if both carry the same place ID.
        3. For name-only claims (or claims without place ID), requires an established location match within 1.0 km.
        4. Missing, malformed, non-finite, or out-of-bounds coordinates must not establish that match.
        """
        # Direct canonical place ID matching and cross-place isolation
        claim_pid = getattr(claim, "place_id", None) or (
            claim.get("place_id") if isinstance(claim, dict) else None
        )
        if claim_pid is not None:
            if candidate.place_id is not None and str(claim_pid) == str(candidate.place_id):
                return True
            # Explicitly linked to another place ID (or candidate has no place_id)
            return False

        # Name-only claim (claim_pid is None): requires candidate to have valid coordinates
        if candidate.point is None:
            return False

        valid_cand, _ = validate_coordinates(candidate.point.lat, candidate.point.lng)
        if not valid_cand:
            return False

        # Extract target coordinates from claim or its linked evidence
        target_coords: tuple[float, float] | None = None

        # 1. Geographic scope on claim
        geo = getattr(claim, "geographic_scope", None) or (
            claim.get("geographic_scope") if isinstance(claim, dict) else None
        )
        target_coords = self._extract_coordinates_from_data(geo)

        # 2. Typed value on claim
        if target_coords is None:
            typed_val = getattr(claim, "typed_value", None) or (
                claim.get("typed_value") if isinstance(claim, dict) else None
            )
            target_coords = self._extract_coordinates_from_data(typed_val)

        # 3. Linked evidence observations
        if target_coords is None:
            links = getattr(claim, "evidence_links", None) or (
                claim.get("evidence_links") if isinstance(claim, dict) else None
            )
            for link in links or []:
                obs = getattr(link, "observation", None) or (
                    link.get("observation") if isinstance(link, dict) else None
                )
                if obs is not None:
                    art_ref = getattr(obs, "artifact_ref", None) or (
                        obs.get("artifact_ref") if isinstance(obs, dict) else None
                    )
                    obs_coords = self._extract_coordinates_from_data(art_ref)
                    if obs_coords is None:
                        obs_geo = getattr(obs, "geographic_scope", None) or (
                            obs.get("geographic_scope") if isinstance(obs, dict) else None
                        )
                        obs_coords = self._extract_coordinates_from_data(obs_geo)
                    if obs_coords is None:
                        obs_coords = self._extract_coordinates_from_data(obs)
                    if obs_coords is not None:
                        target_coords = obs_coords
                        break

        # Missing, malformed, non-finite, or out-of-bounds coordinates must not establish a match
        if target_coords is None:
            return False

        dist = haversine_distance_km(
            candidate.point.lat,
            candidate.point.lng,
            target_coords[0],
            target_coords[1],
        )
        return dist <= 1.0

    async def _retrieve_evidence_for_candidate(
        self,
        candidate: RegisteredCandidate,
        execution_id: uuid.UUID | None,
        owner_id: uuid.UUID | None,
        fixture_observations: list[Any] | None,
        fixture_claims: list[Any] | None,
    ) -> tuple[list[Any], list[Any]]:
        """Retrieves observations and claims relevant to the candidate with strict tenant/execution isolation."""
        obs_results: list[Any] = []
        claims_results: list[Any] = []

        cand_name_norm = candidate.primary_name.strip().lower()
        cand_place_id = candidate.place_id

        # Database retrieval with execution isolation and explicit claim-evidence links
        if self._session is not None:
            claim_stmt = select(ClaimModel).options(
                selectinload(ClaimModel.evidence_links).selectinload(ClaimEvidenceModel.observation)
            )
            if cand_place_id is not None:
                claim_stmt = claim_stmt.where(
                    (ClaimModel.place_id == cand_place_id)
                    | (
                        ClaimModel.place_id.is_(None)
                        & (
                            (ClaimModel.subject_id == cand_name_norm)
                            | (ClaimModel.subject_id == candidate.primary_name)
                        )
                    )
                )
            else:
                claim_stmt = claim_stmt.where(
                    ClaimModel.place_id.is_(None)
                    & (
                        (ClaimModel.subject_id == cand_name_norm)
                        | (ClaimModel.subject_id == candidate.primary_name)
                    )
                )

            if execution_id is not None:
                claim_stmt = claim_stmt.where(ClaimModel.execution_id == execution_id)

            claim_exec = await self._session.execute(claim_stmt)
            raw_claims = list(claim_exec.scalars().all())

            for c in raw_claims:
                if c.place_id is not None:
                    # Place ID is explicitly set: must match this candidate's place ID
                    if cand_place_id is not None and c.place_id == cand_place_id:
                        claims_results.append(c)
                else:
                    # Name-only claim (place_id is None): require subject match AND established location match
                    if self._claim_matches_candidate_location(c, candidate):
                        claims_results.append(c)

            # Only extract observations explicitly linked to claims for this candidate
            for c in claims_results:
                for link in getattr(c, "evidence_links", []) or []:
                    if link.observation is not None and link.observation not in obs_results:
                        obs_results.append(link.observation)

        # Fixture / in-memory retrieval for offline mode
        if fixture_claims:
            for clm in fixture_claims:
                clm_exec_id = getattr(clm, "execution_id", None) or (
                    clm.get("execution_id") if isinstance(clm, dict) else None
                )
                subj = (
                    str(
                        getattr(clm, "subject_id", "")
                        if not isinstance(clm, dict)
                        else clm.get("subject_id", "")
                    )
                    .strip()
                    .lower()
                )
                pid = getattr(clm, "place_id", None) or (
                    clm.get("place_id") if isinstance(clm, dict) else None
                )

                # Verify execution isolation if execution_id specified
                if execution_id and clm_exec_id and str(clm_exec_id) != str(execution_id):
                    continue

                if pid is not None:
                    if cand_place_id and str(pid) == str(cand_place_id):
                        claims_results.append(clm)
                    continue

                # Name-only claim: require subject match AND established location match
                if subj == cand_name_norm or subj == candidate.primary_name.strip().lower():
                    if self._claim_matches_candidate_location(clm, candidate):
                        claims_results.append(clm)

        if fixture_observations:
            for obs in fixture_observations:
                obs_exec_id = getattr(obs, "execution_id", None) or (
                    obs.get("execution_id") if isinstance(obs, dict) else None
                )
                obs_pid = getattr(obs, "place_id", None) or (
                    obs.get("place_id") if isinstance(obs, dict) else None
                )

                # Verify execution isolation if execution_id specified
                if execution_id and obs_exec_id and str(obs_exec_id) != str(execution_id):
                    continue

                if obs_pid is not None:
                    if cand_place_id and str(obs_pid) == str(cand_place_id):
                        obs_results.append(obs)
                    continue

                # Name-only observation: check spatial distance if coordinates are present
                art_ref = getattr(obs, "artifact_ref", None) or (
                    obs.get("artifact_ref") if isinstance(obs, dict) else None
                )
                obs_coords = self._extract_coordinates_from_data(art_ref)
                if obs_coords is None:
                    obs_geo = getattr(obs, "geographic_scope", None) or (
                        obs.get("geographic_scope") if isinstance(obs, dict) else None
                    )
                    obs_coords = self._extract_coordinates_from_data(obs_geo)
                if obs_coords is None:
                    obs_coords = self._extract_coordinates_from_data(obs)
                if obs_coords is not None and candidate.point is not None:
                    dist = haversine_distance_km(
                        candidate.point.lat,
                        candidate.point.lng,
                        obs_coords[0],
                        obs_coords[1],
                    )
                    if dist > 1.0:
                        continue

                obs_subj = (
                    str(
                        getattr(obs, "subject_id", None)
                        or (obs.get("subject_id") if isinstance(obs, dict) else "")
                    )
                    .strip()
                    .lower()
                )
                obs_name = (
                    str(
                        getattr(obs, "place_name", None)
                        or (obs.get("place_name") if isinstance(obs, dict) else "")
                    )
                    .strip()
                    .lower()
                )
                excerpt = str(
                    getattr(obs, "excerpt_text", None)
                    or (obs.get("excerpt_text") if isinstance(obs, dict) else "")
                ).lower()

                if (
                    obs_subj == cand_name_norm
                    or obs_name == cand_name_norm
                    or cand_name_norm in excerpt
                ):
                    obs_results.append(obs)

        return obs_results, claims_results

    def _evaluate_existence(
        self,
        candidate: RegisteredCandidate,
        observations: list[Any],
        claims: list[Any],
    ) -> tuple[CandidateExistenceStatus, dict[str, Any]]:
        """Evaluates whether existence is verified, contradicted, or unverified.

        Conforms strictly to spec 0007 AC-3 and verification policy:
        1. Checks for refuting evidence or conflicted claims -> CONTRADICTED.
        2. Positive verification requires a VERIFIED, FRESH claim supporting the place's existence,
           linked via ClaimEvidenceModel with support_type == SUPPORTS to an active, valid,
           non-NEEDS_BROWSER, non-unavailable source observation.
        3. Never infers verification from a name mention, URL substring, missing support_type,
           unavailable page, NEEDS_BROWSER placeholder, stale observation, or unrelated claim.
        4. If evidence is missing, unverified, or insufficient, candidate remains UNVERIFIED.
        """
        # 1. Contradiction checks: refuting observations, conflicted claims, or closed/demolished
        for clm in claims:
            verif = getattr(clm, "verification", None) or (
                clm.get("verification") if isinstance(clm, dict) else None
            )
            avail = getattr(clm, "availability", None) or (
                clm.get("availability") if isinstance(clm, dict) else None
            )
            typed_val = (
                getattr(clm, "typed_value", {})
                or (clm.get("typed_value", {}) if isinstance(clm, dict) else {})
                or {}
            )

            if str(verif).upper() in (VerificationStatus.CONFLICTED.value, "CONFLICTED"):
                return CandidateExistenceStatus.CONTRADICTED, {
                    "contradiction_reason": "Conflicting claims detected for place identity or existence"
                }

            if str(avail).upper() in (
                AvailabilityStatus.UNAVAILABLE.value,
                "UNAVAILABLE",
                "RESTRICTED",
            ):
                status_str = str(typed_val.get("status", "")).lower()
                if any(k in status_str for k in ("permanently_closed", "demolished", "closed")):
                    return CandidateExistenceStatus.CONTRADICTED, {
                        "contradiction_reason": f"Place marked unavailable/closed: {status_str}"
                    }

            # Check evidence links on the claim
            links = (
                getattr(clm, "evidence_links", [])
                or (clm.get("evidence_links", []) if isinstance(clm, dict) else [])
                or []
            )
            for link in links:
                sup_type = getattr(link, "support_type", None) or (
                    link.get("support_type") if isinstance(link, dict) else None
                )
                if str(sup_type).upper() in (EvidenceSupportType.REFUTES.value, "REFUTES"):
                    return CandidateExistenceStatus.CONTRADICTED, {
                        "contradiction_reason": "Linked evidence observation refutes existence or asserts place is closed/demolished"
                    }

        for obs in observations:
            sup_type = getattr(obs, "support_type", None) or (
                obs.get("support_type") if isinstance(obs, dict) else None
            )
            if str(sup_type).upper() in (
                EvidenceSupportType.REFUTES.value,
                "REFUTES",
                "CONTRADICTED",
            ):
                return CandidateExistenceStatus.CONTRADICTED, {
                    "contradiction_reason": "Refuting evidence observation asserts place does not exist or is permanently closed"
                }

        # 2. Positive existence verification checks
        EXISTENCE_CATEGORIES = {
            ClaimCategory.ATTRACTION.value,
            "EXISTENCE",
            "ATTRACTION",
            "LODGING",
            "TRANSIT",
        }

        has_verified_existence = False
        for clm in claims:
            cat = str(
                getattr(clm, "category", "")
                or (clm.get("category", "") if isinstance(clm, dict) else "")
            ).upper()
            if cat not in EXISTENCE_CATEGORIES:
                # Unrelated verified claims (e.g. WEATHER, FEES) must not verify existence
                continue

            verif = str(
                getattr(clm, "verification", "")
                or (clm.get("verification", "") if isinstance(clm, dict) else "")
            ).upper()
            if verif not in (VerificationStatus.VERIFIED.value, "VERIFIED"):
                continue

            freshness = str(
                getattr(clm, "freshness", "")
                or (clm.get("freshness", "") if isinstance(clm, dict) else "")
            ).upper()
            valid_until = getattr(clm, "valid_until", None) or (
                clm.get("valid_until") if isinstance(clm, dict) else None
            )
            if freshness == FreshnessStatus.STALE.value or (
                valid_until is not None and self._as_of > valid_until
            ):
                # Stale claims do not verify existence
                continue

            # Check linked evidence
            links = (
                getattr(clm, "evidence_links", [])
                or (clm.get("evidence_links", []) if isinstance(clm, dict) else [])
                or []
            )
            obs_to_check: list[Any] = []
            for link in links:
                sup_type = getattr(link, "support_type", None) or (
                    link.get("support_type") if isinstance(link, dict) else None
                )
                # Must have explicit SUPPORTS (missing support_type rejected!)
                if sup_type is None or str(sup_type).upper() not in (
                    EvidenceSupportType.SUPPORTS.value,
                    "SUPPORTS",
                ):
                    continue
                obs_obj = getattr(link, "observation", None) or (
                    link.get("observation") if isinstance(link, dict) else None
                )
                if obs_obj is not None:
                    obs_to_check.append(obs_obj)

            if not links and observations:
                # Direct observations explicitly associated with candidate
                for obs in observations:
                    sup_type = getattr(obs, "support_type", None) or (
                        obs.get("support_type") if isinstance(obs, dict) else None
                    )
                    if sup_type is not None and str(sup_type).upper() in (
                        EvidenceSupportType.SUPPORTS.value,
                        "SUPPORTS",
                    ):
                        obs_to_check.append(obs)

            # Check if any observation is valid, non-error, non-NEEDS_BROWSER, fresh
            for obs in obs_to_check:
                ext_method = str(
                    getattr(obs, "extraction_method", "")
                    or (obs.get("extraction_method", "") if isinstance(obs, dict) else "")
                ).upper()
                if ext_method == "NEEDS_BROWSER":
                    continue

                artifact_ref = (
                    getattr(obs, "artifact_ref", None)
                    or (obs.get("artifact_ref", {}) if isinstance(obs, dict) else {})
                    or {}
                )
                if isinstance(artifact_ref, dict):
                    status_val = str(artifact_ref.get("status") or "").upper().strip()
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
                        artifact_ref.get("error")
                        or artifact_ref.get("unavailable_reason")
                        or artifact_ref.get("unavailable_sources")
                    ):
                        continue
                    if artifact_ref.get("browser_reason"):
                        continue

                excerpt = getattr(obs, "excerpt_text", None) or (
                    obs.get("excerpt_text", "") if isinstance(obs, dict) else ""
                )
                if "[DYNAMIC_PAGE: JAVASCRIPT_REQUIRED]" in (excerpt or ""):
                    continue

                # Check observation freshness if retrieved_at is available
                retrieved_at = getattr(obs, "retrieved_at", None) or (
                    obs.get("retrieved_at") if isinstance(obs, dict) else None
                )
                if retrieved_at is not None and isinstance(retrieved_at, datetime):
                    ret_dt = (
                        retrieved_at
                        if retrieved_at.tzinfo is not None
                        else retrieved_at.replace(tzinfo=UTC)
                    )
                    if (self._as_of - ret_dt).days > 365:
                        continue

                # Valid supporting observation found for this verified claim!
                has_verified_existence = True
                break

            if has_verified_existence:
                break

        if has_verified_existence:
            return CandidateExistenceStatus.VERIFIED, {
                "existence_verification": "VERIFIED",
                "evidence_note": "Existence confirmed by verified fresh claims backed by supporting evidence",
            }

        # 3. Missing or unverified evidence
        return CandidateExistenceStatus.UNVERIFIED, {
            "existence_verification": "UNVERIFIED",
            "evidence_note": (
                "No supporting source observations or verified claims exist. "
                "Candidate place preserved only as editorial recommendation."
            ),
        }

    def _evaluate_hours(
        self,
        candidate: RegisteredCandidate,
        claims: list[Any],
    ) -> tuple[CandidateHoursStatus, time, time, bool, dict[str, Any]]:
        """Evaluates operating hours for a verified existence place.

        If hours are known and verified, uses the verified window (is_assumed=False).
        If hours are unknown, missing, unverified, or stale, applies the assumed daytime window
        (09:00 to 18:00) with is_assumed=True, explicitly labeled as HOURS_ASSUMED.
        """
        hours_claims = [
            c
            for c in claims
            if str(
                getattr(c, "category", "") if not isinstance(c, dict) else c.get("category", "")
            ).upper()
            == ClaimCategory.HOURS.value
        ]

        for clm in hours_claims:
            verif = getattr(clm, "verification", None)
            freshness = getattr(clm, "freshness", None)
            typed_val = getattr(clm, "typed_value", {}) or {}
            valid_until = getattr(clm, "valid_until", None)

            if isinstance(clm, dict):
                verif = clm.get("verification")
                freshness = clm.get("freshness")
                typed_val = clm.get("typed_value", {})
                valid_until = clm.get("valid_until")

            # Check freshness bound
            is_stale = str(freshness).upper() in (FreshnessStatus.STALE.value, "STALE") or (
                valid_until is not None and self._as_of > valid_until
            )

            if is_stale:
                return (
                    CandidateHoursStatus.HOURS_ASSUMED,
                    DEFAULT_ASSUMED_WINDOW_START,
                    DEFAULT_ASSUMED_WINDOW_END,
                    True,
                    {
                        "hours_status": "HOURS_ASSUMED",
                        "hours_diagnostic": (
                            "Existing hours claim is STALE. "
                            "Assumed daytime window 09:00-18:00 applied (HOURS_ASSUMED)."
                        ),
                    },
                )

            if str(verif).upper() in (VerificationStatus.VERIFIED.value, "VERIFIED"):
                opens_str = typed_val.get("opens") or typed_val.get("open")
                closes_str = typed_val.get("closes") or typed_val.get("close")
                start_t = parse_time_str(opens_str)
                end_t = parse_time_str(closes_str)

                if start_t is not None and end_t is not None:
                    return (
                        CandidateHoursStatus.VERIFIED_HOURS,
                        start_t,
                        end_t,
                        False,
                        {
                            "hours_status": "VERIFIED_HOURS",
                            "hours_diagnostic": f"Verified operating hours applied: {start_t.strftime('%H:%M')} to {end_t.strftime('%H:%M')}",
                        },
                    )

        # Unknown or missing hours -> fallback to explicitly labeled assumed daytime window
        return (
            CandidateHoursStatus.HOURS_ASSUMED,
            DEFAULT_ASSUMED_WINDOW_START,
            DEFAULT_ASSUMED_WINDOW_END,
            True,
            {
                "hours_status": "HOURS_ASSUMED",
                "hours_diagnostic": (
                    "Operating hours unknown or unverified. "
                    "Assumed daytime window 09:00-18:00 applied (HOURS_ASSUMED)."
                ),
            },
        )
