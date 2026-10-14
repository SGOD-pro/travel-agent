"""Claim verification service coordinating repository persistence and policy evaluation.

Conforms to spec 0002 AC-9, AC-10, AC-11, and AC-12.
Evaluates deterministic verification states, links evidence observations,
preserves side by side contradictions, and manages claim freshness transitions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from functions.orchestrator.evidence.models import ClaimModel
from functions.orchestrator.evidence.policy import ClaimVerificationPolicy
from functions.orchestrator.evidence.repository import ClaimsRepository
from functions.orchestrator.evidence.schemas import (
    ClaimCreate,
    ClaimEvidenceCreate,
    ClaimUpdate,
    EvidenceSupportType,
    VerificationStatus,
)


class ClaimVerificationService:
    """Service orchestrating claim submission, policy evaluation, and evidence linking."""

    def __init__(
        self,
        session: AsyncSession,
        policy: ClaimVerificationPolicy | None = None,
    ) -> None:
        self._session = session
        self._repo = ClaimsRepository(session)
        self._policy = policy or ClaimVerificationPolicy()

    async def submit_claim(
        self,
        claim_data: ClaimCreate,
        observations_with_support: list[tuple[Any, EvidenceSupportType, str]] | None = None,
        as_of: datetime | None = None,
    ) -> ClaimModel:
        """Evaluates and persists an incoming claim with evidence and contradiction checks.

        If a contradiction is detected against an existing claim for the same subject and category,
        both claims are marked as CONFLICTED and mutually cross referenced.
        Otherwise, deterministic policy evaluates verification status based on domain authority.
        """
        # Serialize claim evaluations per subject under PostgreSQL to prevent parallel race conditions
        if self._session.bind and getattr(self._session.bind.dialect, "name", "") == "postgresql":
            await self._session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext(:subject_id))"),
                {"subject_id": claim_data.subject_id},
            )

        existing_claims = await self._repo.find_by_subject_and_category(
            subject_id=claim_data.subject_id,
            category=str(claim_data.category),
            place_id=claim_data.place_id,
        )

        conflicting_claims: list[tuple[ClaimModel, str]] = []
        for existing in existing_claims:
            is_conflict, reason = self._policy.detect_contradiction(existing, claim_data)
            if is_conflict and reason is not None:
                conflicting_claims.append((existing, reason))

        # Extract observation instances and find latest published timestamp
        obs_items: list[dict[str, Any]] = []
        latest_published_at: datetime | None = None

        if observations_with_support:
            for obs, support_type, _ in observations_with_support:
                obs_dict: dict[str, Any] = {
                    "support_type": support_type,
                    "canonical_url": getattr(obs, "canonical_url", ""),
                    "source_url": getattr(obs, "source_url", ""),
                    "publisher": getattr(obs, "publisher", ""),
                }
                obs_items.append(obs_dict)

                pub_at = getattr(obs, "published_at", None)
                if pub_at is not None:
                    if latest_published_at is None or pub_at > latest_published_at:
                        latest_published_at = pub_at

        now_iso = datetime.now(UTC).isoformat()

        if conflicting_claims:
            # AC-11: Conflicting claims preserve side by side as CONFLICTED
            claim_data.verification = VerificationStatus.CONFLICTED
            _, avail = self._policy.evaluate_verification(claim_data, obs_items, has_conflicts=True)
            claim_data.availability = avail
            claim_data.freshness = self._policy.evaluate_freshness(
                claim_data, latest_published_at, as_of=as_of
            )

            initial_contradictions = []
            for existing_c, reason_msg in conflicting_claims:
                initial_contradictions.append({
                    "claim_id": str(existing_c.id),
                    "reason": reason_msg,
                    "detected_at": now_iso,
                })
            claim_data.contradictions = initial_contradictions

            decision_history = list(claim_data.decision_history)
            decision_history.append({
                "event": "CLAIM_SUBMITTED_WITH_CONFLICT",
                "verification": VerificationStatus.CONFLICTED.value,
                "contradictions_count": len(conflicting_claims),
                "at": now_iso,
            })
            claim_data.decision_history = decision_history

            created_claim = await self._repo.create_claim(claim_data)

            # Update existing conflicting claims to CONFLICTED in batch (Nit 2)
            await self._repo.record_contradictions_batch(
                new_claim=created_claim,
                conflicting_claims_and_reasons=conflicting_claims,
            )
        else:
            # AC-9 & AC-10: Deterministic verification evaluation
            verif, avail = self._policy.evaluate_verification(
                claim_data,
                obs_items,
                has_conflicts=False,
            )
            claim_data.verification = verif
            claim_data.availability = avail
            claim_data.freshness = self._policy.evaluate_freshness(
                claim_data, latest_published_at, as_of=as_of
            )

            decision_history = list(claim_data.decision_history)
            decision_history.append({
                "event": "CLAIM_EVALUATED",
                "verification": verif.value,
                "availability": avail.value,
                "freshness": claim_data.freshness.value,
                "at": now_iso,
            })
            claim_data.decision_history = decision_history

            created_claim = await self._repo.create_claim(claim_data)

        # Link evidence junction records
        if observations_with_support:
            for obs, support_type, locator in observations_with_support:
                obs_id = getattr(obs, "id", None)
                if obs_id is not None:
                    evidence_dto = ClaimEvidenceCreate(
                        claim_id=created_claim.id,
                        observation_id=obs_id,
                        support_type=support_type,
                        locator=locator,
                    )
                    await self._repo.link_evidence(evidence_dto)

        return created_claim

    async def recheck_claim_freshness(
        self,
        claim_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> ClaimModel:
        """Re evaluates freshness for an existing claim without altering historical verification records.

        Conforms strictly to AC-12. If validity window elapsed, transitions freshness to STALE.
        """
        claim = await self._repo.get_by_id(claim_id)
        if claim is None:
            raise ValueError(f"Claim with id {claim_id} not found")

        # Find latest published timestamp from observations or links
        latest_published_at: datetime | None = None
        evidence_links = await self._repo.get_claim_evidence(claim_id)
        for link in evidence_links:
            if hasattr(link, "observation") and link.observation is not None:
                pub_at = getattr(link.observation, "published_at", None)
                if pub_at is not None:
                    if latest_published_at is None or pub_at > latest_published_at:
                        latest_published_at = pub_at

        if latest_published_at is None and hasattr(claim, "observations") and claim.observations:
            for obs in claim.observations:
                pub_at = getattr(obs, "published_at", None)
                if pub_at is not None:
                    if latest_published_at is None or pub_at > latest_published_at:
                        latest_published_at = pub_at

        new_freshness = self._policy.evaluate_freshness(
            claim=claim,
            published_at=latest_published_at,
            as_of=as_of,
        )

        if new_freshness.value != claim.freshness:
            now_iso = datetime.now(UTC).isoformat()
            history = list(claim.decision_history)
            history.append({
                "event": "FRESHNESS_TRANSITION",
                "from": claim.freshness,
                "to": new_freshness.value,
                "at": now_iso,
            })
            update_dto = ClaimUpdate(
                freshness=new_freshness,
                decision_history=history,
            )
            claim = await self._repo.update_claim(claim_id, update_dto)

        return claim
