"""Shared application service for trip planning operations conforming to spec 0007.

Provides direct Python service methods used by both API route handlers
and the direct pipeline runner without HTTP or ASGI indirection.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.question import AnswerSubmissionResult
from contracts.trips import TripBrief, TripBriefDraft
from functions.api.idempotency import IdempotencyRepository
from functions.api.models import TripModel, TripVersionModel
from functions.orchestrator.evidence.query_planner import QueryPlanningService
from functions.orchestrator.execution.budget import ensure_execution_budget_structure
from functions.orchestrator.execution.job_events import get_job_events
from functions.orchestrator.execution.models import ExecutionModel, TravelerQuestionModel
from functions.orchestrator.hitl.service import TravelerAnswerService

logger = logging.getLogger(__name__)


class TripNotFoundError(Exception):
    """Raised when trip cannot be found or does not belong to owner."""


class TripPlanningService:
    """Application service coordinating trip planning workflows."""

    def __init__(self, answer_service: TravelerAnswerService | None = None) -> None:
        self._answer_service = answer_service or TravelerAnswerService()

    async def create_draft_trip(
        self,
        session: AsyncSession,
        *,
        owner_id: uuid.UUID,
        brief: dict[str, Any] | TripBrief | TripBriefDraft,
        trip_id: uuid.UUID | None = None,
    ) -> tuple[TripModel, TripVersionModel]:
        """Creates a Trip aggregate and commits its initial brief snapshot."""
        now = datetime.now(UTC)
        target_trip_id = trip_id or uuid.uuid4()

        brief_json: dict[str, Any]
        if isinstance(brief, (TripBrief, TripBriefDraft)):
            brief_json = brief.model_dump(mode="json")
        else:
            brief_json = dict(brief)

        trip = TripModel(
            id=target_trip_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(trip)

        version = TripVersionModel(
            trip_id=target_trip_id,
            version=1,
            brief_json=brief_json,
            schema_version=1,
            command_id=uuid.uuid4(),
            created_at=now,
        )
        session.add(version)
        await session.flush()

        return trip, version

    async def start_planning(
        self,
        session: AsyncSession,
        *,
        trip_id: uuid.UUID,
        owner_id: uuid.UUID,
        idempotency_key: str,
        workflow: str = "TRAVEL",
        deadline_seconds: int = 3600,
        planning_options: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validates trip and brief, computes budget, and atomically accepts a planning job."""
        if workflow != "TRAVEL":
            raise ValueError(f"Unsupported workflow '{workflow}'. Only 'TRAVEL' is supported.")

        trip_stmt = select(TripModel).where(TripModel.id == trip_id)
        trip = (await session.execute(trip_stmt)).scalar_one_or_none()
        if not trip or trip.owner_id != owner_id:
            raise TripNotFoundError(f"Trip {trip_id} not found")

        version_stmt = select(TripVersionModel).where(
            TripVersionModel.trip_id == trip.id,
            TripVersionModel.version == trip.current_version,
        )
        trip_version = (await session.execute(version_stmt)).scalar_one_or_none()
        if trip_version is None:
            raise ValueError(
                f"Trip {trip_id} has no valid brief snapshot for version {trip.current_version}"
            )

        try:
            validated_brief = TripBrief.model_validate(trip_version.brief_json)
            brief_snapshot = validated_brief.model_dump(mode="json")
            planned_queries = QueryPlanningService.plan_destination_queries(validated_brief)
            collector_budget = QueryPlanningService.calculate_collector_budget(planned_queries)
            execution_budget = ensure_execution_budget_structure(collector_budget)
        except (ValidationError, ValueError, TypeError) as exc:
            try:
                draft_brief = TripBriefDraft.model_validate(trip_version.brief_json)
                brief_snapshot = draft_brief.model_dump(mode="json")
                execution_budget = ensure_execution_budget_structure({})
            except Exception as draft_exc:
                raise ValueError(
                    f"Trip {trip_id} version {trip_version.version} has an invalid brief snapshot: {exc}; {draft_exc}"
                ) from exc

        start_payload: dict[str, Any] = {
            "trip_id": str(trip.id),
            "owner_id": str(owner_id),
            "version": trip_version.version,
            "brief": brief_snapshot,
            "workflow": workflow,
            "context_version": 1,
        }

        idempotency_repo = IdempotencyRepository(session)
        payload_dict: dict[str, Any] = {
            "trip_id": str(trip_id),
            "workflow": workflow,
        }
        if planning_options is not None:
            payload_dict["planning_options"] = planning_options
        if payload is not None:
            payload_dict.update(payload)

        response_json, status_code, is_cached = await idempotency_repo.accept_job(
            owner_id=owner_id,
            operation=f"start_trip_plan:{trip_id}",
            idempotency_key=idempotency_key.strip(),
            payload=payload_dict,
            trip_id=trip_id,
            workflow=workflow,
            deadline_seconds=deadline_seconds,
            start_payload=start_payload,
            execution_budget=execution_budget,
        )

        return response_json

    async def get_job_state(
        self,
        session: AsyncSession,
        *,
        job_id: uuid.UUID,
        owner_id: uuid.UUID,
        cursor: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Reads execution status, timeline events, and pending questions."""
        exec_stmt = select(ExecutionModel).where(ExecutionModel.id == job_id)
        execution = (await session.execute(exec_stmt)).scalar_one_or_none()
        if execution is None or execution.owner_id != owner_id:
            raise TripNotFoundError(f"Job {job_id} not found")

        events, next_seq = await get_job_events(
            session, execution_id=job_id, cursor=cursor, limit=limit
        )

        # Pending questions
        q_stmt = (
            select(TravelerQuestionModel)
            .where(
                TravelerQuestionModel.execution_id == job_id,
                TravelerQuestionModel.status == "PENDING",
            )
            .order_by(TravelerQuestionModel.created_at.asc())
        )
        pending_q_rows = (await session.execute(q_stmt)).scalars().all()
        pending_questions = [
            {
                "id": str(q.id),
                "question_id": str(q.id),
                "question_key": q.question_key,
                "question_type": q.question_type,
                "prompt": q.prompt,
                "validation_rules": dict(q.validation_rules) if q.validation_rules else {},
                "context_version": q.context_version,
                "created_at": q.created_at.isoformat(),
            }
            for q in pending_q_rows
        ]

        return {
            "job_id": str(execution.id),
            "status": execution.status,
            "context_version": execution.context_version,
            "missing_inputs": [q.question_key for q in pending_q_rows],
            "pending_questions": pending_questions,
            "timeline": [
                {
                    "id": str(e.id),
                    "sequence": e.sequence,
                    "event_type": e.event_type,
                    "safe_payload": e.safe_payload,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in events
            ],
            "next_cursor": str(next_seq) if next_seq is not None else None,
        }

    async def submit_answer(
        self,
        session: AsyncSession,
        *,
        job_id: uuid.UUID,
        question_id: uuid.UUID,
        owner_id: uuid.UUID,
        answer: dict[str, Any],
        expected_context_version: int,
    ) -> AnswerSubmissionResult:
        """Applies traveler answer through TravelerAnswerService."""
        return await self._answer_service.submit_answer(
            session,
            question_id=question_id,
            caller_id=owner_id,
            answer=answer,
            expected_context_version=expected_context_version,
            execution_id=job_id,
        )

    async def get_itinerary(
        self,
        session: AsyncSession,
        *,
        trip_id: uuid.UUID,
        owner_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Retrieves finalized itinerary proposal for a trip."""
        trip_stmt = select(TripModel).where(TripModel.id == trip_id)
        trip = (await session.execute(trip_stmt)).scalar_one_or_none()
        if not trip or trip.owner_id != owner_id:
            raise TripNotFoundError(f"Trip {trip_id} not found")

        return trip.itinerary_json
