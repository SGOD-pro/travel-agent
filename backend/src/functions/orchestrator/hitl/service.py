"""Domain service managing traveler questions and answers."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.question import (
    AnswerSubmissionResult,
    QuestionStatus,
    QuestionType,
    TravelerQuestion,
)
from contracts.trips import TripBrief
from functions.api.models import TripModel, TripVersionModel
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import (
    ExecutionModel,
    OutboxEventModel,
    TravelerQuestionModel,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    compile_trip_planning_workflow,
)

logger = logging.getLogger(__name__)


class QuestionNotFoundError(Exception):
    """Raised when requested question cannot be found."""


class QuestionOwnerMismatchError(Exception):
    """Raised when caller is not the owner of the question."""


class ExecutionCancelledError(Exception):
    """Raised when answering question for cancelled execution."""


class ExecutionDeadlineExceededError(Exception):
    """Raised when execution deadline has passed or execution failed."""


class QuestionNotPendingError(Exception):
    """Raised when question is not in pending status."""


class StaleContextError(Exception):
    """Raised when expected context version does not match current version."""


class ConflictingAnswerError(Exception):
    """Raised when conflicting answer is submitted for answered question."""


class AnswerValidationError(Exception):
    """Raised when submitted answer fails domain validation."""


class TravelerAnswerService:
    """Service governing question queries and atomic answer submission."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory

    async def get_pending_questions(
        self,
        session: AsyncSession,
        *,
        execution_id: uuid.UUID | None = None,
        owner_id: uuid.UUID | None = None,
        trip_id: uuid.UUID | None = None,
    ) -> list[TravelerQuestion]:
        """Queries pending questions matching filters."""
        stmt = (
            select(TravelerQuestionModel)
            .where(TravelerQuestionModel.status == QuestionStatus.PENDING.value)
            .order_by(TravelerQuestionModel.created_at.asc())
        )
        if execution_id is not None:
            stmt = stmt.where(TravelerQuestionModel.execution_id == execution_id)
        if owner_id is not None:
            stmt = stmt.where(TravelerQuestionModel.owner_id == owner_id)
        if trip_id is not None:
            stmt = stmt.where(TravelerQuestionModel.trip_id == trip_id)

        rows = (await session.execute(stmt)).scalars().all()
        return [
            TravelerQuestion(
                id=q.id,
                execution_id=q.execution_id,
                trip_id=q.trip_id,
                owner_id=q.owner_id,
                context_version=q.context_version,
                question_key=q.question_key,
                question_type=QuestionType(q.question_type),
                prompt=q.prompt,
                validation_rules=dict(q.validation_rules) if q.validation_rules else {},
                status=QuestionStatus(q.status),
                answer_json=dict(q.answer_json) if q.answer_json else None,
                created_at=q.created_at,
                answered_at=q.answered_at,
            )
            for q in rows
        ]

    async def submit_answer(
        self,
        session: AsyncSession,
        *,
        question_id: uuid.UUID,
        caller_id: uuid.UUID,
        answer: dict[str, Any],
        expected_context_version: int | None = None,
        execution_id: uuid.UUID | None = None,
    ) -> AnswerSubmissionResult:
        """Validates answer, advances version atomically, and stages execution resume."""
        now = datetime.now(UTC)

        # 1. Lock question row
        q_stmt = (
            select(TravelerQuestionModel)
            .where(TravelerQuestionModel.id == question_id)
            .with_for_update()
        )
        question = (await session.execute(q_stmt)).scalar_one_or_none()
        if question is None:
            raise QuestionNotFoundError(f"Question {question_id} not found")

        # 2. Caller ownership check
        if question.owner_id != caller_id:
            raise QuestionOwnerMismatchError(
                f"Caller {caller_id} is not owner of question {question_id}"
            )

        if execution_id is not None and question.execution_id != execution_id:
            raise QuestionNotFoundError(
                f"Question {question_id} not found for job {execution_id}"
            )

        # 3. Lock execution row
        exec_stmt = (
            select(ExecutionModel)
            .where(ExecutionModel.id == question.execution_id)
            .with_for_update()
        )
        execution = (await session.execute(exec_stmt)).scalar_one_or_none()
        if execution is None:
            raise QuestionNotFoundError(
                f"Execution {question.execution_id} not found for question {question_id}"
            )

        # 4. Barriers on execution
        if execution.status == "CANCELLED" or execution.cancelled_at is not None:
            raise ExecutionCancelledError(f"Execution {execution.id} is cancelled")
        if (execution.deadline_at and execution.deadline_at <= now) or execution.status == "FAILED":
            raise ExecutionDeadlineExceededError(
                f"Execution {execution.id} deadline exceeded or failed"
            )

        # 5. Replay and idempotency handling
        if question.status == QuestionStatus.ANSWERED.value:
            if question.answer_json == answer:
                return AnswerSubmissionResult(
                    question_id=question.id,
                    execution_id=execution.id,
                    status=QuestionStatus.ANSWERED.value,
                    new_context_version=execution.context_version,
                    context_version=execution.context_version,
                    is_duplicate=True,
                    message="Answer already recorded with identical content",
                )
            raise ConflictingAnswerError(
                f"Question {question_id} already answered with different content"
            )

        if question.status != QuestionStatus.PENDING.value:
            raise QuestionNotPendingError(
                f"Question {question_id} is in status {question.status}, cannot be answered"
            )

        # 6. Stale context version checks
        if (
            expected_context_version is not None
            and expected_context_version != question.context_version
        ):
            raise StaleContextError(
                f"Expected context version {expected_context_version} does not match question version {question.context_version}"
            )
        if question.context_version != execution.context_version:
            raise StaleContextError(
                f"Question context version {question.context_version} does not match execution version {execution.context_version}"
            )

        # 7. Validate answer content against rules
        self._validate_answer_content(
            question_type=question.question_type,
            validation_rules=question.validation_rules or {},
            answer=answer,
        )

        # 8. Mark question answered
        question.status = QuestionStatus.ANSWERED.value
        question.answer_json = answer
        question.answered_at = now

        # 9. Advance context version
        execution.context_version += 1
        execution.updated_at = now
        question.context_version = execution.context_version

        # 10. Update trip and trip versions if trip associated
        complete_brief_dict: dict[str, Any] = {}
        if execution.trip_id is not None:
            trip_stmt = (
                select(TripModel).where(TripModel.id == execution.trip_id).with_for_update()
            )
            trip = (await session.execute(trip_stmt)).scalar_one_or_none()
            if trip is not None:
                trip.current_version += 1
                trip.updated_at = now

                ver_stmt = (
                    select(TripVersionModel)
                    .where(TripVersionModel.trip_id == trip.id)
                    .order_by(TripVersionModel.version.desc())
                )
                prev_version = (await session.execute(ver_stmt)).scalars().first()
                base_brief_json = dict(prev_version.brief_json) if prev_version else {}
                if question.question_key == "travel_dates":
                    base_brief_json["start_date"] = str(answer["start_date"])
                    base_brief_json["end_date"] = str(answer["end_date"])

                validated_brief = TripBrief.model_validate(base_brief_json)
                complete_brief_dict = validated_brief.model_dump(mode="json")

                new_ver = TripVersionModel(
                    trip_id=trip.id,
                    version=trip.current_version,
                    brief_json=complete_brief_dict,
                    schema_version=1,
                    command_id=uuid.uuid4(),
                    created_at=now,
                )
                session.add(new_ver)

        # 11. Update LangGraph checkpoint state thread
        factory = self._session_factory
        if factory is None and session.bind is not None:
            factory = async_sessionmaker(
                bind=session.bind, expire_on_commit=False, class_=AsyncSession
            )

        if factory is not None:
            checkpointer = TransactionBoundCheckpointer(session_factory=factory)
            graph = compile_trip_planning_workflow(checkpointer=checkpointer)
            config = {
                "configurable": {
                    "thread_id": str(execution.id),
                    "session_factory": factory,
                }
            }

            if not complete_brief_dict:
                current_chk = await checkpointer.aget_tuple(config)
                if current_chk and current_chk.checkpoint:
                    channel_values = current_chk.checkpoint.get("channel_values", {})
                    base_brief_json = dict(channel_values.get("brief", {}))
                    if question.question_key == "travel_dates":
                        base_brief_json["start_date"] = str(answer["start_date"])
                        base_brief_json["end_date"] = str(answer["end_date"])
                    validated_brief = TripBrief.model_validate(base_brief_json)
                    complete_brief_dict = validated_brief.model_dump(mode="json")

            if complete_brief_dict:
                update_values: dict[str, Any] = {
                    "brief": complete_brief_dict,
                    "pending_question": None,
                }
                if execution.trip_id is not None and trip is not None:
                    update_values["version"] = trip.current_version
                await graph.aupdate_state(
                    config,
                    update_values,
                )
                await checkpointer.flush(session, str(execution.id))

        # 12. Record job event
        await record_job_event(
            session,
            execution_id=execution.id,
            event_type="QUESTION_ANSWERED",
            safe_payload={
                "question_id": str(question.id),
                "question_key": question.question_key,
                "context_version": execution.context_version,
            },
        )

        # 13. Stage outbox event for durable resume
        now_outbox = datetime.now(UTC)
        session.add(
            OutboxEventModel(
                id=uuid.uuid4(),
                execution_id=execution.id,
                aggregate_id=execution.id,
                kind="EXECUTION_RESUME",
                event_type="EXECUTION_RESUME",
                payload_ref={
                    "execution_id": str(execution.id),
                    "action": "resume_user_answer",
                    "question_id": str(question.id),
                    "context_version": execution.context_version,
                },
                payload={
                    "execution_id": str(execution.id),
                    "action": "resume_user_answer",
                    "question_id": str(question.id),
                    "context_version": execution.context_version,
                },
                due_at=now_outbox,
                attempts=0,
                created_at=now_outbox,
            )
        )

        return AnswerSubmissionResult(
            question_id=question.id,
            execution_id=execution.id,
            status=QuestionStatus.ANSWERED.value,
            new_context_version=execution.context_version,
            context_version=execution.context_version,
            is_duplicate=False,
            message="Answer recorded and execution resume scheduled",
        )

    def _validate_answer_content(
        self,
        *,
        question_type: str,
        validation_rules: dict[str, Any],
        answer: dict[str, Any],
    ) -> None:
        """Validates answer payload against question rules."""
        if not isinstance(answer, dict):
            raise AnswerValidationError("Answer payload must be a JSON dictionary")

        if question_type == QuestionType.DATE_RANGE.value:
            start_date_raw = answer.get("start_date")
            end_date_raw = answer.get("end_date")
            if not start_date_raw or not end_date_raw:
                raise AnswerValidationError(
                    "Missing required date range fields: start_date and end_date"
                )

            try:
                start_dt = date.fromisoformat(str(start_date_raw))
                end_dt = date.fromisoformat(str(end_date_raw))
            except ValueError as exc:
                raise AnswerValidationError(
                    f"Dates must be in ISO format YYYY-MM-DD: {exc}"
                ) from exc

            if end_dt < start_dt:
                raise AnswerValidationError("end_date cannot be earlier than start_date")

            max_stay = validation_rules.get("max_stay_days")
            if max_stay is not None:
                stay_days = (end_dt - start_dt).days + 1
                if stay_days > int(max_stay):
                    raise AnswerValidationError(
                        f"Trip duration {stay_days} days exceeds maximum allowed {max_stay} days"
                    )

        elif question_type == QuestionType.CHOICE.value:
            selected = answer.get("selected")
            if selected is None:
                raise AnswerValidationError("Missing selected choice in answer")
            choices = validation_rules.get("choices", [])
            if choices and selected not in choices:
                raise AnswerValidationError(
                    f"Selected value '{selected}' is not in allowed choices: {choices}"
                )

        elif question_type == QuestionType.TEXT.value:
            text_val = answer.get("text")
            if not text_val or not str(text_val).strip():
                raise AnswerValidationError("Answer text cannot be empty")
