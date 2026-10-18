"""Repository for LAYA decision persistence, canonical replay digest, and reuse revalidation.

Conforms to spec 0004 Slice 4:
1. Canonical replay digest incorporating phase, context_version, policy_version,
   question_version, sorted eligible actions, and sanitized context hash.
2. Result reuse revalidation across 4 criteria:
   - Matching phase.
   - Matching context_version.
   - Matching policy_version.
   - Chosen action remaining an active eligible choice.
3. Idempotent persistence with PostgreSQL ON CONFLICT DO NOTHING handling concurrent runner conflicts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionPhase,
    LayaDecisionRequest,
)
from functions.orchestrator.decision.models import LayaDecisionModel
from functions.orchestrator.decision.policy import LayaPolicyValidator


def compute_canonical_replay_digest(
    execution_id: uuid.UUID,
    phase: DecisionPhase | str,
    context_version: int,
    policy_version: str,
    question_version: str,
    eligible_actions: list[DecisionAction],
    sanitized_context: dict[str, Any] | str,
) -> tuple[str, str]:
    """Compute phase specific canonical SHA256 request digest and idempotency key (AC-9).

    Format:
    digest_input = {execution_id}:{phase}:{context_version}:{policy_version}:{question_version}:{sorted_choices}:{context_hash}
    idempotency_key = {execution_id}:laya_decision:{phase}:{request_digest[:16]}
    """
    phase_str = phase.value if isinstance(phase, DecisionPhase) else str(phase)
    sorted_choices = ",".join(sorted(action.value for action in eligible_actions))

    if isinstance(sanitized_context, dict):
        context_json = json.dumps(sanitized_context, sort_keys=True, default=str)
        context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
    else:
        context_hash = hashlib.sha256(str(sanitized_context).encode("utf-8")).hexdigest()

    digest_input = (
        f"{execution_id}:"
        f"{phase_str}:"
        f"{context_version}:"
        f"{policy_version}:"
        f"{question_version}:"
        f"{sorted_choices}:"
        f"{context_hash}"
    )
    request_digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    idempotency_key = f"{execution_id}:laya_decision:{phase_str}:{request_digest[:16]}"
    return request_digest, idempotency_key


class LayaDecisionRepository:
    """Repository managing persistence, replay checks, and result reuse for LAYA decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_decision_id(self, decision_id: uuid.UUID) -> LayaDecisionModel | None:
        """Retrieves a decision record by unique decision UUID."""
        stmt = select(LayaDecisionModel).where(LayaDecisionModel.decision_id == decision_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_idempotency_key(
        self,
        execution_id: uuid.UUID,
        idempotency_key: str,
    ) -> LayaDecisionModel | None:
        """Retrieves a decision record by execution ID and idempotency key."""
        stmt = select(LayaDecisionModel).where(
            LayaDecisionModel.execution_id == execution_id,
            LayaDecisionModel.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_reusable_decision(
        self,
        request: LayaDecisionRequest,
        active_phase: DecisionPhase,
    ) -> DecisionEnvelope | None:
        """Checks for existing decision and revalidates 4 point conditions before reuse.

        1. Recorded phase matches active phase.
        2. Recorded context_version matches request context_version.
        3. Recorded policy_version matches active policy version.
        4. Recorded chosen_action is currently an eligible choice.

        Returns:
            DecisionEnvelope if valid for reuse, None if fresh inference required.
        """
        record = await self.get_by_idempotency_key(
            execution_id=request.execution_id,
            idempotency_key=request.idempotency_key,
        )
        if record is None:
            return None

        # Revalidation Check 1: Phase match
        if record.extract_phase() != active_phase:
            return None

        # Revalidation Check 2: Context version match
        if record.context_version != request.context_version:
            return None

        # Revalidation Check 3: Policy version match
        if record.policy_version != LayaPolicyValidator.POLICY_VERSION:
            return None

        # Revalidation Check 4: Chosen action remains eligible
        try:
            chosen = DecisionAction(record.chosen_action)
            if chosen not in request.policy_derived_eligible_choices:
                return None
        except ValueError:
            return None

        # All four revalidation checks passed: reuse stored decision cleanly
        return record.to_envelope()

    async def persist_decision(
        self,
        envelope: DecisionEnvelope,
        *,
        context_version: int = 1,
        allowed_choices: list[DecisionAction] | None = None,
    ) -> LayaDecisionModel:
        """Persists a DecisionEnvelope with conflict resolution on (execution_id, idempotency_key).

        Concurrent conflicts load existing committed row cleanly.
        """
        choices_list = (
            [c.value for c in allowed_choices]
            if allowed_choices is not None
            else [envelope.allowed_choice.value]
        )

        conf_dec = Decimal(str(envelope.confidence)) if envelope.confidence is not None else None
        ans_conf_dec = (
            Decimal(str(envelope.answer_confidence))
            if envelope.answer_confidence is not None
            else None
        )

        decision_row: dict[str, Any] = {
            "id": uuid.uuid4(),
            "decision_id": envelope.decision_id,
            "execution_id": envelope.execution_id,
            "trip_id": envelope.trip_id,
            "context_version": context_version,
            "idempotency_key": envelope.idempotency_key,
            "decision_type": envelope.decision_type.value,
            "allowed_choices": choices_list,
            "chosen_action": envelope.allowed_choice.value,
            "confidence": conf_dec,
            "answer_confidence": ans_conf_dec,
            "model_version": envelope.model_version,
            "policy_version": envelope.policy_version,
            "status": envelope.status.value,
            "rationale": envelope.rationale[:1000] if envelope.rationale is not None else None,
            "latency_ms": envelope.latency_ms,
            "request_digest": envelope.request_digest,
        }

        # Insert with ON CONFLICT DO NOTHING to handle concurrent runners cleanly
        stmt = pg_insert(LayaDecisionModel).values(decision_row)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["execution_id", "idempotency_key"]
        )
        await self._session.execute(stmt)
        await self._session.flush()

        # Query back persisted row (either newly inserted or preexisting committed row)
        lookup_stmt = select(LayaDecisionModel).where(
            LayaDecisionModel.execution_id == envelope.execution_id,
            LayaDecisionModel.idempotency_key == envelope.idempotency_key,
        )
        result = await self._session.execute(lookup_stmt)
        return result.scalar_one()

    async def get_decisions_for_execution(
        self,
        execution_id: uuid.UUID,
    ) -> list[LayaDecisionModel]:
        """Returns all decision records for an execution ordered chronologically."""
        stmt = (
            select(LayaDecisionModel)
            .where(LayaDecisionModel.execution_id == execution_id)
            .order_by(LayaDecisionModel.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
