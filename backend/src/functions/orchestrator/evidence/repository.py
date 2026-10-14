"""Claims repository managing database persistence and evidence linking.

Conforms to spec 0002 AC-8, AC-9, and AC-11.
Provides atomic operations for claim insertion, evidence junction linking,
and non destructive contradiction cross referencing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from functions.orchestrator.evidence.models import ClaimEvidenceModel, ClaimModel
from functions.orchestrator.evidence.schemas import (
    ClaimCreate,
    ClaimEvidenceCreate,
    ClaimUpdate,
    VerificationStatus,
)


class ClaimsRepository:
    """Repository managing claim persistence, evidence links, and contradiction records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_claim(self, data: ClaimCreate) -> ClaimModel:
        """Inserts a new claim into the database."""
        claim_id = data.id or uuid.uuid4()
        claim = ClaimModel(
            id=claim_id,
            execution_id=data.execution_id,
            subject_id=data.subject_id,
            place_id=data.place_id,
            category=str(data.category),
            typed_value=data.typed_value,
            verification=data.verification.value,
            availability=data.availability.value,
            freshness=data.freshness.value,
            valid_from=data.valid_from,
            valid_until=data.valid_until,
            geographic_scope=data.geographic_scope,
            policy_version=data.policy_version,
            confidence=data.confidence,
            contradictions=data.contradictions,
            decision_history=data.decision_history,
        )
        self._session.add(claim)
        await self._session.flush()
        return claim

    async def get_by_id(self, claim_id: uuid.UUID) -> ClaimModel | None:
        """Retrieves a claim by primary key UUID."""
        stmt = select(ClaimModel).where(ClaimModel.id == claim_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_by_subject_and_category(
        self,
        subject_id: str,
        category: str,
        place_id: uuid.UUID | None = None,
    ) -> list[ClaimModel]:
        """Finds existing claims for a subject and category, with optional place scoping."""
        stmt = (
            select(ClaimModel)
            .where(
                ClaimModel.subject_id == subject_id,
                ClaimModel.category == category,
            )
            .order_by(ClaimModel.created_at.desc())
        )
        if place_id is not None:
            stmt = stmt.where(ClaimModel.place_id == place_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_place_id(self, place_id: uuid.UUID) -> list[ClaimModel]:
        """Finds all claims associated with a canonical place."""
        stmt = (
            select(ClaimModel)
            .where(ClaimModel.place_id == place_id)
            .order_by(ClaimModel.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_execution_id(self, execution_id: uuid.UUID) -> list[ClaimModel]:
        """Finds all claims generated during an execution."""
        stmt = (
            select(ClaimModel)
            .where(ClaimModel.execution_id == execution_id)
            .order_by(ClaimModel.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_claim(self, claim_id: uuid.UUID, update: ClaimUpdate) -> ClaimModel:
        """Updates fields on an existing claim while validating validity interval bounds."""
        claim = await self.get_by_id(claim_id)
        if claim is None:
            raise ValueError(f"Claim with id {claim_id} not found")

        update.validate_with_existing(claim.valid_from, claim.valid_until)

        if update.typed_value is not None:
            claim.typed_value = update.typed_value
        if update.verification is not None:
            claim.verification = update.verification.value
        if update.availability is not None:
            claim.availability = update.availability.value
        if update.freshness is not None:
            claim.freshness = update.freshness.value
        if update.valid_from is not None:
            claim.valid_from = update.valid_from
        if update.valid_until is not None:
            claim.valid_until = update.valid_until
        if update.geographic_scope is not None:
            claim.geographic_scope = update.geographic_scope
        if update.policy_version is not None:
            claim.policy_version = update.policy_version
        if update.confidence is not None:
            claim.confidence = update.confidence
        if update.contradictions is not None:
            claim.contradictions = update.contradictions
        if update.decision_history is not None:
            claim.decision_history = update.decision_history

        await self._session.flush()
        return claim

    async def link_evidence(self, create_dto: ClaimEvidenceCreate) -> ClaimEvidenceModel:
        """Associates a claim with a source observation snapshot in the junction table."""
        link = ClaimEvidenceModel(
            claim_id=create_dto.claim_id,
            observation_id=create_dto.observation_id,
            support_type=create_dto.support_type.value,
            locator=create_dto.locator,
        )
        self._session.add(link)
        await self._session.flush()
        return link

    async def get_claim_evidence(self, claim_id: uuid.UUID) -> list[ClaimEvidenceModel]:
        """Retrieves all evidence records associated with a claim."""
        stmt = (
            select(ClaimEvidenceModel)
            .where(ClaimEvidenceModel.claim_id == claim_id)
            .order_by(ClaimEvidenceModel.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def record_contradiction(
        self,
        claim_a_id: uuid.UUID,
        claim_b_id: uuid.UUID,
        reason: str,
    ) -> tuple[ClaimModel, ClaimModel]:
        """Atomically marks two claims as CONFLICTED and cross references their identifiers.

        Preserves both claims side by side without destructive overwriting.
        """
        claim_a = await self.get_by_id(claim_a_id)
        if claim_a is None:
            raise ValueError(f"Claim with id {claim_a_id} not found")

        claim_b = await self.get_by_id(claim_b_id)
        if claim_b is None:
            raise ValueError(f"Claim with id {claim_b_id} not found")

        now_iso = datetime.now(UTC).isoformat()

        # Update claim A
        old_status_a = claim_a.verification
        claim_a.verification = VerificationStatus.CONFLICTED.value
        contra_a = list(claim_a.contradictions)
        existing_refs_a = {item.get("claim_id") for item in contra_a if isinstance(item, dict)}
        if str(claim_b.id) not in existing_refs_a:
            contra_a.append({"claim_id": str(claim_b.id), "reason": reason, "detected_at": now_iso})
            claim_a.contradictions = contra_a

        history_a = list(claim_a.decision_history)
        history_a.append({
            "event": "CONTRADICTION_FLAGGED",
            "from": old_status_a,
            "to": VerificationStatus.CONFLICTED.value,
            "conflicting_claim_id": str(claim_b.id),
            "reason": reason,
            "at": now_iso,
        })
        claim_a.decision_history = history_a

        # Update claim B
        old_status_b = claim_b.verification
        claim_b.verification = VerificationStatus.CONFLICTED.value
        contra_b = list(claim_b.contradictions)
        existing_refs_b = {item.get("claim_id") for item in contra_b if isinstance(item, dict)}
        if str(claim_a.id) not in existing_refs_b:
            contra_b.append({"claim_id": str(claim_a.id), "reason": reason, "detected_at": now_iso})
            claim_b.contradictions = contra_b

        history_b = list(claim_b.decision_history)
        history_b.append({
            "event": "CONTRADICTION_FLAGGED",
            "from": old_status_b,
            "to": VerificationStatus.CONFLICTED.value,
            "conflicting_claim_id": str(claim_a.id),
            "reason": reason,
            "at": now_iso,
        })
        claim_b.decision_history = history_b

        await self._session.flush()
        return claim_a, claim_b

    async def record_contradictions_batch(
        self,
        new_claim: ClaimModel,
        conflicting_claims_and_reasons: list[tuple[ClaimModel, str]],
    ) -> None:
        """Atomically updates multiple conflicting claims to CONFLICTED in a batch.

        Cross references existing conflicting claims with the new claim without separate
        round trips.
        """
        if not conflicting_claims_and_reasons:
            return

        now_iso = datetime.now(UTC).isoformat()
        new_claim_id_str = str(new_claim.id)

        for existing_c, reason in conflicting_claims_and_reasons:
            old_status = existing_c.verification
            existing_c.verification = VerificationStatus.CONFLICTED.value
            contra_list = list(existing_c.contradictions)
            refs = {item.get("claim_id") for item in contra_list if isinstance(item, dict)}
            if new_claim_id_str not in refs:
                contra_list.append({"claim_id": new_claim_id_str, "reason": reason, "detected_at": now_iso})
                existing_c.contradictions = contra_list

            history = list(existing_c.decision_history)
            history.append({
                "event": "CONTRADICTION_FLAGGED",
                "from": old_status,
                "to": VerificationStatus.CONFLICTED.value,
                "conflicting_claim_id": new_claim_id_str,
                "reason": reason,
                "at": now_iso,
            })
            existing_c.decision_history = history

        await self._session.flush()
