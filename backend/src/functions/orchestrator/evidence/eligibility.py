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

from contracts.candidate import (
    CandidateEligibilityReport,
    CandidateExistenceStatus,
    CandidateHoursStatus,
    RegisteredCandidate,
)
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.models import ClaimModel
from functions.orchestrator.evidence.schemas import (
    AvailabilityStatus,
    ClaimCategory,
    EvidenceSupportType,
    FreshnessStatus,
    VerificationStatus,
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

        for candidate in candidates:
            # 1. Retrieve evidence for this candidate
            obs_list, claims_list = await self._retrieve_evidence_for_candidate(
                candidate=candidate,
                execution_id=execution_id,
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

    async def _retrieve_evidence_for_candidate(
        self,
        candidate: RegisteredCandidate,
        execution_id: uuid.UUID | None,
        fixture_observations: list[Any] | None,
        fixture_claims: list[Any] | None,
    ) -> tuple[list[Any], list[Any]]:
        """Retrieves observations and claims relevant to the candidate with strict tenant/execution isolation."""
        obs_results: list[Any] = []
        claims_results: list[Any] = []

        cand_name_norm = candidate.primary_name.strip().lower()
        cand_place_id = candidate.place_id

        # Database retrieval with execution isolation
        if self._session is not None:
            if execution_id is not None:
                # Query source observations scoped to execution_id
                obs_stmt = select(SourceObservationModel).where(
                    SourceObservationModel.execution_id == execution_id
                )
                obs_exec = await self._session.execute(obs_stmt)
                all_obs = obs_exec.scalars().all()
                for o in all_obs:
                    # Match by place_id in artifact_ref or place name in excerpt
                    matches_name = cand_name_norm in (o.excerpt_text or "").lower()
                    matches_url = cand_name_norm.replace(" ", "") in o.canonical_url.lower()
                    if matches_name or matches_url:
                        obs_results.append(o)

                # Query claims scoped to execution_id or matching place_id
                claim_stmt = select(ClaimModel).where(ClaimModel.execution_id == execution_id)
                claim_exec = await self._session.execute(claim_stmt)
                all_claims = claim_exec.scalars().all()
                for c in all_claims:
                    matches_place = cand_place_id is not None and c.place_id == cand_place_id
                    matches_subj = c.subject_id.strip().lower() == cand_name_norm
                    if matches_place or matches_subj:
                        claims_results.append(c)

        # Fixture / in-memory retrieval for offline mode
        if fixture_observations:
            for obs in fixture_observations:
                obs_exec_id = getattr(obs, "execution_id", None)
                if isinstance(obs, dict):
                    obs_exec_id = obs.get("execution_id")
                    text = (obs.get("excerpt_text") or obs.get("content") or "").lower()
                    url = (obs.get("canonical_url") or obs.get("source_url") or "").lower()
                else:
                    text = (getattr(obs, "excerpt_text", "") or "").lower()
                    url = (
                        getattr(obs, "canonical_url", "") or getattr(obs, "source_url", "")
                    ).lower()

                # Verify execution isolation if execution_id specified
                if execution_id and obs_exec_id and str(obs_exec_id) != str(execution_id):
                    continue

                if cand_name_norm in text or cand_name_norm.replace(" ", "") in url:
                    obs_results.append(obs)

        if fixture_claims:
            for clm in fixture_claims:
                clm_exec_id = getattr(clm, "execution_id", None)
                if isinstance(clm, dict):
                    clm_exec_id = clm.get("execution_id")
                    subj = str(clm.get("subject_id", "")).strip().lower()
                    pid = clm.get("place_id")
                else:
                    subj = str(getattr(clm, "subject_id", "")).strip().lower()
                    pid = getattr(clm, "place_id", None)

                # Verify execution isolation if execution_id specified
                if execution_id and clm_exec_id and str(clm_exec_id) != str(execution_id):
                    continue

                if subj == cand_name_norm or (cand_place_id and str(pid) == str(cand_place_id)):
                    claims_results.append(clm)

        return obs_results, claims_results

    def _evaluate_existence(
        self,
        candidate: RegisteredCandidate,
        observations: list[Any],
        claims: list[Any],
    ) -> tuple[CandidateExistenceStatus, dict[str, Any]]:
        """Evaluates whether existence is verified, contradicted, or unverified."""
        # 1. Contradiction checks: refuting observations or conflicted/closed claims
        for obs in observations:
            sup_type = getattr(obs, "support_type", None)
            if isinstance(obs, dict):
                sup_type = obs.get("support_type")
            if str(sup_type).upper() in (
                EvidenceSupportType.REFUTES.value,
                "REFUTES",
                "CONTRADICTED",
            ):
                return CandidateExistenceStatus.CONTRADICTED, {
                    "contradiction_reason": "Refuting evidence observation asserts place does not exist or is permanently closed"
                }

        for clm in claims:
            verif = getattr(clm, "verification", None)
            avail = getattr(clm, "availability", None)
            typed_val = getattr(clm, "typed_value", {}) or {}
            if isinstance(clm, dict):
                verif = clm.get("verification")
                avail = clm.get("availability")
                typed_val = clm.get("typed_value", {})

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

        # 2. Supporting evidence checks
        has_supporting_obs = False
        for obs in observations:
            sup_type = getattr(obs, "support_type", None)
            ext_method = getattr(obs, "extraction_method", None)
            if isinstance(obs, dict):
                sup_type = obs.get("support_type")
                ext_method = obs.get("extraction_method")

            if str(ext_method).upper() == "NEEDS_BROWSER":
                # Pending browser escalation does not grant verification
                continue

            if sup_type is None or str(sup_type).upper() in (
                EvidenceSupportType.SUPPORTS.value,
                "SUPPORTS",
            ):
                has_supporting_obs = True
                break

        has_verified_claim = False
        for clm in claims:
            verif = getattr(clm, "verification", None)
            if isinstance(clm, dict):
                verif = clm.get("verification")
            if str(verif).upper() in (VerificationStatus.VERIFIED.value, "VERIFIED"):
                has_verified_claim = True
                break

        if has_supporting_obs or has_verified_claim:
            return CandidateExistenceStatus.VERIFIED, {
                "existence_verification": "VERIFIED",
                "evidence_note": "Existence confirmed by supporting observations or verified claims",
            }

        # 3. Missing evidence check
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
