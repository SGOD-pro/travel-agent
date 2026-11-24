"""Integration tests for Human in the Loop (HITL) pipeline execution conforming to spec 0007.

Validates:
1. Incomplete brief creates question, transitions execution to WAITING_USER, releases lease, halts dispatch.
2. Valid answer advances context_version, updates brief, and resumes planning.
3. Invalid answer (inverted dates) is rejected with clear diagnostics, leaves question PENDING, preserves state.
4. Duplicate identical answer replay succeeds idempotently without duplicate resume events.
5. Conflicting answer for answered question is rejected under concurrency.
6. Owner mismatch and stale context version are rejected with explicit errors.
7. Cancellation and deadline expiry prevent answer acceptance and execution resume.
8. Process restart in WAITING_USER preserves question and resumes cleanly when answered.
9. Resumed planning preserves already completed metered candidate evidence.
10. Complete brief bypasses question generation and executes straight through.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.question import QuestionStatus, QuestionType
from functions.api.models import TripModel, TripVersionModel
from functions.orchestrator.execution import (
    ExecutionResumeEvent,
    ExecutionStartEvent,
    GraphRunnerService,
)
from functions.orchestrator.execution.models import (
    ExecutionModel,
    JobEventModel,
    OutboxEventModel,
    TravelerQuestionModel,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    compile_trip_planning_workflow,
)
from functions.orchestrator.hitl import (
    AnswerValidationError,
    ConflictingAnswerError,
    ExecutionCancelledError,
    QuestionNotFoundError,
    QuestionOwnerMismatchError,
    StaleContextError,
    TravelerAnswerService,
)


def _make_incomplete_brief_dict(
    origin: str = "Mysuru, Karnataka",
    dest1: str = "Srirangapatna, Karnataka",
    dest2: str = "Mandya, Karnataka",
) -> dict[str, Any]:
    """Creates a brief dictionary omitting start_date and end_date."""
    return {
        "title": "Mysuru Cultural Trip",
        "origin_name": origin,
        "origin_lat": 12.2958,
        "origin_lng": 76.6394,
        "destinations": [
            {
                "name": dest1,
                "latitude": 12.4218,
                "longitude": 76.6932,
                "stay_days": 1,
                "confidence": 0.95,
            },
            {
                "name": dest2,
                "latitude": 12.5242,
                "longitude": 76.8958,
                "stay_days": 1,
                "confidence": 0.90,
            },
        ],
        "budget_limit": 12000,
        "currency": "INR",
        "budget_tier": "MODERATE",
        "preferred_modes": ["car_petrol"],
        "traveler_preferences": ["heritage", "daylight driving"],
        "dietary_preferences": ["local vegetarian"],
        "accessibility_requirements": [],
    }


def _make_complete_brief_dict(
    start_date: str = "2026-11-25",
    end_date: str = "2026-11-26",
) -> dict[str, Any]:
    """Creates a complete brief dictionary including dates."""
    base = _make_incomplete_brief_dict()
    base["start_date"] = start_date
    base["end_date"] = end_date
    return base


@pytest.mark.asyncio
async def test_incomplete_brief_creates_question_and_enters_waiting_user(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Assertion 1: Incomplete brief persists question, enters WAITING_USER, releases lease."""
    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    now = datetime.now(UTC)
    draft_brief = _make_incomplete_brief_dict()

    async with session_factory() as session:
        trip = TripModel(
            id=trip_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(trip)
        ver = TripVersionModel(
            trip_id=trip_id,
            version=1,
            brief_json=draft_brief,
            schema_version=1,
            command_id=uuid.uuid4(),
            created_at=now,
        )
        session.add(ver)

        execution = ExecutionModel(
            id=exec_id,
            owner_id=owner_id,
            trip_id=trip_id,
            workflow="TRAVEL",
            status="CREATED",
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(execution)
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(
        session_factory,
        default_graph=graph,
        workflow_graphs={"TRAVEL": graph},
    )

    start_event = ExecutionStartEvent(
        execution_id=exec_id,
        event_id=uuid.uuid4(),
        workflow="TRAVEL",
        context_version=1,
        payload={"brief": draft_brief, "trip_id": str(trip_id)},
    )
    result = await runner.handle_execution_start(event=start_event)

    assert result.status == "WAITING_USER"
    assert result.tasks_dispatched == 0

    async with session_factory() as session:
        exec_db = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        assert exec_db.status == "WAITING_USER"
        assert exec_db.lease_token is None

        q_stmt = select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
        question = (await session.execute(q_stmt)).scalar_one_or_none()
        assert question is not None
        assert question.question_key == "travel_dates"
        assert question.question_type == QuestionType.DATE_RANGE.value
        assert question.status == QuestionStatus.PENDING.value
        assert question.owner_id == owner_id

        events_stmt = (
            select(JobEventModel)
            .where(JobEventModel.execution_id == exec_id)
            .order_by(JobEventModel.sequence.asc())
        )
        events = (await session.execute(events_stmt)).scalars().all()
        event_types = [e.event_type for e in events]
        assert "QUESTION_ASKED" in event_types


@pytest.mark.asyncio
async def test_valid_answer_advances_version_and_resumes_planning(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Assertion 2: Valid answer advances context_version, updates brief, and stages resume."""
    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    now = datetime.now(UTC)
    draft_brief = _make_incomplete_brief_dict()

    async with session_factory() as session:
        trip = TripModel(
            id=trip_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(trip)
        session.add(
            TripVersionModel(
                trip_id=trip_id,
                version=1,
                brief_json=draft_brief,
                schema_version=1,
                command_id=uuid.uuid4(),
                created_at=now,
            )
        )
        session.add(
            ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                trip_id=trip_id,
                workflow="TRAVEL",
                status="CREATED",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(
        session_factory,
        default_graph=graph,
        workflow_graphs={"TRAVEL": graph},
    )

    await runner.handle_execution_start(
        event=ExecutionStartEvent(
            execution_id=exec_id,
            event_id=uuid.uuid4(),
            workflow="TRAVEL",
            context_version=1,
            payload={"brief": draft_brief, "trip_id": str(trip_id)},
        )
    )

    async with session_factory() as session:
        q = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalar_one()
        question_id = q.id

    hitl_service = TravelerAnswerService(session_factory=session_factory)
    valid_answer = {"start_date": "2026-11-25", "end_date": "2026-11-26"}

    async with session_factory() as session:
        sub_res = await hitl_service.submit_answer(
            session,
            question_id=question_id,
            caller_id=owner_id,
            answer=valid_answer,
            expected_context_version=1,
        )
        await session.commit()

    assert sub_res.status == QuestionStatus.ANSWERED
    assert sub_res.context_version == 2
    assert sub_res.is_duplicate is False

    async with session_factory() as session:
        exec_db = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        assert exec_db.context_version == 2

        q_db = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.id == question_id)
            )
        ).scalar_one()
        assert q_db.status == QuestionStatus.ANSWERED.value
        assert q_db.answer_json == valid_answer

        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == exec_id,
            OutboxEventModel.kind == "EXECUTION_RESUME",
        )
        resume_evt = (await session.execute(outbox_stmt)).scalar_one_or_none()
        assert resume_evt is not None
        assert resume_evt.payload is not None
        assert resume_evt.payload.get("action") == "resume_user_answer"

    # Resume graph execution
    resume_res = await runner.handle_execution_resume(
        event=ExecutionResumeEvent(
            execution_id=exec_id,
            event_id=resume_evt.id,
            action="resume_user_answer",
            context_version=2,
        )
    )
    assert resume_res.graph_advanced is True


@pytest.mark.asyncio
async def test_invalid_answer_rejected_and_leaves_question_pending(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Assertion 3: Inverted dates fail validation, leaving question PENDING and state clean."""
    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)
    draft_brief = _make_incomplete_brief_dict()

    async with session_factory() as session:
        session.add(
            ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                workflow="TRAVEL",
                status="CREATED",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph, workflow_graphs={"TRAVEL": graph})
    await runner.handle_execution_start(
        event=ExecutionStartEvent(
            execution_id=exec_id,
            event_id=uuid.uuid4(),
            workflow="TRAVEL",
            context_version=1,
            payload={"brief": draft_brief},
        )
    )

    async with session_factory() as session:
        q = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalar_one()
        question_id = q.id

    hitl_service = TravelerAnswerService(session_factory=session_factory)
    inverted_answer = {"start_date": "2026-11-26", "end_date": "2026-11-25"}

    with pytest.raises(AnswerValidationError, match="end_date cannot be earlier than start_date"):
        async with session_factory() as session:
            await hitl_service.submit_answer(
                session,
                question_id=question_id,
                caller_id=owner_id,
                answer=inverted_answer,
                expected_context_version=1,
                execution_id=exec_id,
            )

    async with session_factory() as session:
        q_db = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.id == question_id)
            )
        ).scalar_one()
        assert q_db.status == QuestionStatus.PENDING.value
        assert q_db.answer_json is None

        exec_db = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        assert exec_db.status == "WAITING_USER"
        assert exec_db.context_version == 1

        resume_evt = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
            )
        ).scalar_one_or_none()
        assert resume_evt is None


@pytest.mark.asyncio
async def test_idempotent_duplicate_answer_replay_safe(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Assertion 4: Replaying identical answer succeeds idempotently with is_duplicate=True."""
    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)
    draft_brief = _make_incomplete_brief_dict()

    async with session_factory() as session:
        session.add(
            ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                workflow="TRAVEL",
                status="CREATED",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph, workflow_graphs={"TRAVEL": graph})
    await runner.handle_execution_start(
        event=ExecutionStartEvent(
            execution_id=exec_id,
            event_id=uuid.uuid4(),
            workflow="TRAVEL",
            context_version=1,
            payload={"brief": draft_brief},
        )
    )

    async with session_factory() as session:
        q = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalar_one()
        question_id = q.id

    hitl_service = TravelerAnswerService(session_factory=session_factory)
    valid_answer = {"start_date": "2026-11-25", "end_date": "2026-11-26"}

    # First submission
    async with session_factory() as session:
        res1 = await hitl_service.submit_answer(
            session,
            question_id=question_id,
            caller_id=owner_id,
            answer=valid_answer,
            expected_context_version=1,
        )
        await session.commit()
    assert res1.is_duplicate is False

    # Second submission with identical data
    async with session_factory() as session:
        res2 = await hitl_service.submit_answer(
            session,
            question_id=question_id,
            caller_id=owner_id,
            answer=valid_answer,
            expected_context_version=1,
        )
        await session.commit()

    assert res2.is_duplicate is True
    assert res2.status == QuestionStatus.ANSWERED

    async with session_factory() as session:
        resume_evts = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
            )
        ).scalars().all()
        assert len(resume_evts) == 1


@pytest.mark.asyncio
async def test_concurrent_conflicting_answer_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Assertion 5: Submitting conflicting answer to already answered question raises ConflictingAnswerError."""
    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)
    draft_brief = _make_incomplete_brief_dict()

    async with session_factory() as session:
        session.add(
            ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                workflow="TRAVEL",
                status="CREATED",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph, workflow_graphs={"TRAVEL": graph})
    await runner.handle_execution_start(
        event=ExecutionStartEvent(
            execution_id=exec_id,
            event_id=uuid.uuid4(),
            workflow="TRAVEL",
            context_version=1,
            payload={"brief": draft_brief},
        )
    )

    async with session_factory() as session:
        q = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalar_one()
        question_id = q.id

    hitl_service = TravelerAnswerService(session_factory=session_factory)
    ans1 = {"start_date": "2026-11-25", "end_date": "2026-11-26"}
    ans2 = {"start_date": "2026-12-01", "end_date": "2026-12-02"}

    # True synchronized overlapping concurrent submissions via separate database sessions
    import asyncio
    barrier = asyncio.Event()

    async def _submit(payload: dict[str, Any]) -> AnswerSubmissionResult:
        await barrier.wait()
        async with session_factory() as s:
            res = await hitl_service.submit_answer(
                s,
                question_id=question_id,
                caller_id=owner_id,
                answer=payload,
                expected_context_version=1,
                execution_id=exec_id,
            )
            await s.commit()
            return res

    t1 = asyncio.create_task(_submit(ans1))
    t2 = asyncio.create_task(_submit(ans2))
    barrier.set()
    results = await asyncio.gather(t1, t2, return_exceptions=True)

    # Exactly one succeeded and one raised ConflictingAnswerError or StaleContextError
    from contracts.question import AnswerSubmissionResult
    successes = [r for r in results if isinstance(r, AnswerSubmissionResult)]
    errors = [r for r in results if isinstance(r, (ConflictingAnswerError, StaleContextError))]
    assert len(successes) == 1
    assert len(errors) == 1

    # Exactly one durable resume outbox event survives
    async with session_factory() as session:
        resume_evts = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
            )
        ).scalars().all()
        assert len(resume_evts) == 1


@pytest.mark.asyncio
async def test_owner_mismatch_and_stale_context_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Assertion 6: Cross owner answer raises QuestionOwnerMismatchError; stale version raises StaleContextError."""
    owner_id = uuid.uuid4()
    stranger_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)
    draft_brief = _make_incomplete_brief_dict()

    async with session_factory() as session:
        session.add(
            ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                workflow="TRAVEL",
                status="CREATED",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph, workflow_graphs={"TRAVEL": graph})
    await runner.handle_execution_start(
        event=ExecutionStartEvent(
            execution_id=exec_id,
            event_id=uuid.uuid4(),
            workflow="TRAVEL",
            context_version=1,
            payload={"brief": draft_brief},
        )
    )

    async with session_factory() as session:
        q = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalar_one()
        question_id = q.id

    hitl_service = TravelerAnswerService(session_factory=session_factory)
    valid_answer = {"start_date": "2026-11-25", "end_date": "2026-11-26"}

    # Stranger tries to answer
    with pytest.raises(QuestionOwnerMismatchError):
        async with session_factory() as session:
            await hitl_service.submit_answer(
                session, question_id=question_id, caller_id=stranger_id, answer=valid_answer
            )

    # Stale expected context version
    with pytest.raises(StaleContextError):
        async with session_factory() as session:
            await hitl_service.submit_answer(
                session,
                question_id=question_id,
                caller_id=owner_id,
                answer=valid_answer,
                expected_context_version=99,
            )

    # Mismatched execution ID (cross job response prevention)
    other_exec_id = uuid.uuid4()
    with pytest.raises(QuestionNotFoundError):
        async with session_factory() as session:
            await hitl_service.submit_answer(
                session,
                question_id=question_id,
                caller_id=owner_id,
                answer=valid_answer,
                execution_id=other_exec_id,
            )


@pytest.mark.asyncio
async def test_cancellation_and_deadline_expiry_prevent_resume(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Assertion 7: Cancelled execution or expired deadline rejects answer and blocks resume."""
    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)
    draft_brief = _make_incomplete_brief_dict()

    async with session_factory() as session:
        session.add(
            ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                workflow="TRAVEL",
                status="CREATED",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph, workflow_graphs={"TRAVEL": graph})
    await runner.handle_execution_start(
        event=ExecutionStartEvent(
            execution_id=exec_id,
            event_id=uuid.uuid4(),
            workflow="TRAVEL",
            context_version=1,
            payload={"brief": draft_brief},
        )
    )

    async with session_factory() as session:
        q = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalar_one()
        question_id = q.id

        # Cancel the execution
        exec_db = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        exec_db.status = "CANCELLED"
        exec_db.cancelled_at = now
        await session.commit()

    hitl_service = TravelerAnswerService(session_factory=session_factory)
    valid_answer = {"start_date": "2026-11-25", "end_date": "2026-11-26"}

    with pytest.raises(ExecutionCancelledError):
        async with session_factory() as session:
            await hitl_service.submit_answer(
                session,
                question_id=question_id,
                caller_id=owner_id,
                answer=valid_answer,
                expected_context_version=1,
                execution_id=exec_id,
            )

    # Resume directly also returns CANCELLED
    res = await runner.handle_execution_resume(
        event=ExecutionResumeEvent(
            execution_id=exec_id,
            action="resume_graph",
            context_version=1,
        )
    )
    assert res.status == "CANCELLED"


@pytest.mark.asyncio
async def test_process_restart_preserves_question_and_resumes(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Assertion 8: Separate subprocesses with parent enforced timeouts.
    Process A persists WAITING_USER and exits. Process B reads question from PostgreSQL, answers, and resumes.
    """
    import os
    import subprocess
    import sys

    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    draft_brief = _make_incomplete_brief_dict()

    env = os.environ.copy()
    env["PYTHONPATH"] = "src"

    # Process A: Runs in separate process, creates execution, pauses at WAITING_USER, flushes checkpoint, and exits
    script_a = f"""
import asyncio, uuid, os, sys
from datetime import datetime, UTC, timedelta
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.execution import ExecutionStartEvent, GraphRunnerService
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.graphs.trip_planner.graph import compile_trip_planning_workflow
from acceptance_database import get_acceptance_session_factory

async def main():
    sf = get_acceptance_session_factory()
    exec_id = uuid.UUID({str(exec_id)!r})
    owner_id = uuid.UUID({str(owner_id)!r})
    now = datetime.now(UTC)
    async with sf() as session:
        session.add(ExecutionModel(
            id=exec_id,
            owner_id=owner_id,
            workflow="TRAVEL",
            status="CREATED",
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        ))
        await session.commit()
    checkpointer = TransactionBoundCheckpointer(session_factory=sf)
    graph = compile_trip_planning_workflow(checkpointer=checkpointer)
    runner = GraphRunnerService(sf, default_graph=graph, workflow_graphs={{"TRAVEL": graph}})
    draft_brief = {draft_brief!r}
    res = await runner.handle_execution_start(event=ExecutionStartEvent(
        execution_id=exec_id,
        event_id=uuid.uuid4(),
        workflow="TRAVEL",
        context_version=1,
        payload={{"brief": draft_brief}},
    ))
    assert res.status == "WAITING_USER"
    print("PROCESS_A_WAITING_USER_SUCCESS")

asyncio.run(main())
"""
    p_a = subprocess.run(
        [sys.executable, "-c", script_a],
        cwd=backend_dir,
        env=env,
        timeout=30,
        capture_output=True,
        text=True,
    )
    assert p_a.returncode == 0, f"Process A failed: {p_a.stderr}"
    assert "PROCESS_A_WAITING_USER_SUCCESS" in p_a.stdout

    # Process B: Starts in fresh independent process, reads question from PostgreSQL, answers, and resumes
    script_b = f"""
import asyncio, uuid, os, sys
from functions.orchestrator.hitl import TravelerAnswerService
from functions.orchestrator.execution import ExecutionResumeEvent, GraphRunnerService
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.graphs.trip_planner.graph import compile_trip_planning_workflow
from acceptance_database import get_acceptance_session_factory

async def main():
    sf = get_acceptance_session_factory()
    exec_id = uuid.UUID({str(exec_id)!r})
    owner_id = uuid.UUID({str(owner_id)!r})
    hitl = TravelerAnswerService(session_factory=sf)
    async with sf() as session:
        qs = await hitl.get_pending_questions(session, execution_id=exec_id)
        assert len(qs) == 1
        q_item = qs[0]
        res = await hitl.submit_answer(
            session,
            question_id=q_item.id,
            caller_id=owner_id,
            answer={{"start_date": "2026-11-25", "end_date": "2026-11-26"}},
            expected_context_version=1,
            execution_id=exec_id,
        )
        await session.commit()
        assert res.status == "ANSWERED"
    checkpointer = TransactionBoundCheckpointer(session_factory=sf)
    graph = compile_trip_planning_workflow(checkpointer=checkpointer)
    runner = GraphRunnerService(sf, default_graph=graph, workflow_graphs={{"TRAVEL": graph}})
    resume_res = await runner.handle_execution_resume(event=ExecutionResumeEvent(
        execution_id=exec_id,
        action="resume_user_answer",
        context_version=2,
    ))
    assert resume_res.graph_advanced is True
    print("PROCESS_B_RESUME_SUCCESS")

asyncio.run(main())
"""
    p_b = subprocess.run(
        [sys.executable, "-c", script_b],
        cwd=backend_dir,
        env=env,
        timeout=30,
        capture_output=True,
        text=True,
    )
    assert p_b.returncode == 0, f"Process B failed: {p_b.stderr}"
    assert "PROCESS_B_RESUME_SUCCESS" in p_b.stdout

    # Parent process confirms final database state in PostgreSQL
    async with session_factory() as session:
        exec_db = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        assert exec_db.context_version == 2

        q_db = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalar_one()
        assert q_db.status == "ANSWERED"


@pytest.mark.asyncio
async def test_resumed_planning_preserves_completed_work(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Assertion 9: Resumed execution preserves candidate evidence already present in state."""
    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)
    draft_brief = _make_incomplete_brief_dict()

    async with session_factory() as session:
        session.add(
            ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                workflow="TRAVEL",
                status="CREATED",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph, workflow_graphs={"TRAVEL": graph})
    await runner.handle_execution_start(
        event=ExecutionStartEvent(
            execution_id=exec_id,
            event_id=uuid.uuid4(),
            workflow="TRAVEL",
            context_version=1,
            payload={
                "brief": draft_brief,
                "discovered_places": [
                    {"name": "Daria Daulat Bagh", "category": "heritage", "confidence": 0.9}
                ],
            },
        )
    )

    async with session_factory() as session:
        q = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalar_one()
        question_id = q.id

    hitl_service = TravelerAnswerService(session_factory=session_factory)
    async with session_factory() as session:
        await hitl_service.submit_answer(
            session,
            question_id=question_id,
            caller_id=owner_id,
            answer={"start_date": "2026-11-25", "end_date": "2026-11-26"},
            expected_context_version=1,
            execution_id=exec_id,
        )
        await session.commit()

    # Check state checkpoint retains discovered places
    chk_tuple = await postgres_checkpointer.aget_tuple(
        {"configurable": {"thread_id": str(exec_id)}}
    )
    assert chk_tuple is not None
    channel_vals = chk_tuple.checkpoint.get("channel_values", {})
    discovered = channel_vals.get("discovered_places", [])
    assert any(d.get("name") == "Daria Daulat Bagh" for d in discovered)


@pytest.mark.asyncio
async def test_complete_brief_bypasses_await_user_input(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Assertion 10: Complete brief bypasses question generation and routes directly to DISCOVER_CANDIDATES."""
    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)
    complete_brief = _make_complete_brief_dict()

    async with session_factory() as session:
        session.add(
            ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                workflow="TRAVEL",
                status="CREATED",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph, workflow_graphs={"TRAVEL": graph})

    start_event = ExecutionStartEvent(
        execution_id=exec_id,
        event_id=uuid.uuid4(),
        workflow="TRAVEL",
        context_version=1,
        payload={"brief": complete_brief},
    )
    result = await runner.handle_execution_start(event=start_event)

    # Should NOT be WAITING_USER
    assert result.status != "WAITING_USER"

    async with session_factory() as session:
        q_count = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalars().all()
        assert len(q_count) == 0

        events = (
            await session.execute(
                select(JobEventModel).where(JobEventModel.execution_id == exec_id)
            )
        ).scalars().all()
        event_types = [e.event_type for e in events]
        assert "QUESTION_ASKED" not in event_types


@pytest.mark.asyncio
async def test_stale_answer_rejected_when_trip_brief_updated_independently(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Regression 1: Independent trip brief update while paused makes old question stale.
    Answer submission must be rejected with StaleContextError, preserving newer brief and version.
    """
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)
    draft_brief = _make_incomplete_brief_dict()

    async with session_factory() as session:
        # Trip starts at version 1 with incomplete brief
        session.add(
            TripModel(
                id=trip_id,
                owner_id=owner_id,
                current_version=1,
                state="draft",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            TripVersionModel(
                trip_id=trip_id,
                version=1,
                brief_json=draft_brief,
                schema_version=1,
                command_id=uuid.uuid4(),
                created_at=now,
            )
        )
        session.add(
            ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                trip_id=trip_id,
                workflow="TRAVEL",
                status="CREATED",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph, workflow_graphs={"TRAVEL": graph})
    await runner.handle_execution_start(
        event=ExecutionStartEvent(
            execution_id=exec_id,
            event_id=uuid.uuid4(),
            workflow="TRAVEL",
            context_version=1,
            payload={"trip_id": str(trip_id), "version": 1, "brief": draft_brief},
        )
    )

    async with session_factory() as session:
        q = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalar_one()
        question_id = q.id

        # Traveler independently updates trip brief to version 2
        trip = (await session.execute(select(TripModel).where(TripModel.id == trip_id))).scalar_one()
        trip.current_version = 2
        trip.updated_at = datetime.now(UTC)
        updated_brief = dict(draft_brief)
        updated_brief["title"] = "Updated Mysuru Cultural Trip V2"
        session.add(
            TripVersionModel(
                trip_id=trip_id,
                version=2,
                brief_json=updated_brief,
                schema_version=1,
                command_id=uuid.uuid4(),
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()

    hitl_service = TravelerAnswerService(session_factory=session_factory)
    valid_answer = {"start_date": "2026-11-25", "end_date": "2026-11-26"}

    with pytest.raises(StaleContextError):
        async with session_factory() as session:
            await hitl_service.submit_answer(
                session,
                question_id=question_id,
                caller_id=owner_id,
                answer=valid_answer,
                expected_context_version=1,
                execution_id=exec_id,
            )

    # Verify no state mutation on newer brief or version
    async with session_factory() as session:
        trip = (await session.execute(select(TripModel).where(TripModel.id == trip_id))).scalar_one()
        assert trip.current_version == 2
        ver2 = (
            await session.execute(
                select(TripVersionModel).where(
                    TripVersionModel.trip_id == trip_id,
                    TripVersionModel.version == 2,
                )
            )
        ).scalar_one()
        assert ver2.brief_json["title"] == "Updated Mysuru Cultural Trip V2"

        # Assert no version 3 exists
        ver3 = (
            await session.execute(
                select(TripVersionModel).where(
                    TripVersionModel.trip_id == trip_id,
                    TripVersionModel.version == 3,
                )
            )
        ).scalar_one_or_none()
        assert ver3 is None

        # Question remains PENDING
        q_db = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.id == question_id)
            )
        ).scalar_one()
        assert q_db.status == "PENDING"

        # No EXECUTION_RESUME event
        resume_evt = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
            )
        ).scalar_one_or_none()
        assert resume_evt is None


@pytest.mark.asyncio
async def test_deadline_rechecked_after_lock_wait(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: Any,
) -> None:
    """Regression 2: An answer waiting behind a row lock until deadline expires must be rejected.
    Asserts no answer, context version, checkpoint, or resume mutation survives.
    """
    import asyncio

    from functions.orchestrator.hitl import ExecutionDeadlineExceededError

    owner_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)
    draft_brief = _make_incomplete_brief_dict()

    async with session_factory() as session:
        session.add(
            ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                workflow="TRAVEL",
                status="CREATED",
                context_version=1,
                # Tight deadline 1 second in the future
                deadline_at=now + timedelta(seconds=1),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph, workflow_graphs={"TRAVEL": graph})
    await runner.handle_execution_start(
        event=ExecutionStartEvent(
            execution_id=exec_id,
            event_id=uuid.uuid4(),
            workflow="TRAVEL",
            context_version=1,
            payload={"brief": draft_brief},
        )
    )

    async with session_factory() as session:
        q = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.execution_id == exec_id)
            )
        ).scalar_one()
        question_id = q.id

    hitl_service = TravelerAnswerService(session_factory=session_factory)
    valid_answer = {"start_date": "2026-11-25", "end_date": "2026-11-26"}

    # Session 1 acquires lock on question and execution, holding it while deadline expires
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()

    async def _holder() -> None:
        async with session_factory() as s1:
            await s1.execute(
                select(TravelerQuestionModel)
                .where(TravelerQuestionModel.id == question_id)
                .with_for_update()
            )
            lock_acquired.set()
            await release_lock.wait()
            await s1.rollback()

    holder_task = asyncio.create_task(_holder())
    await lock_acquired.wait()

    # While Session 1 holds lock, start submit_answer in Session 2 (blocks waiting for lock)
    answer_task_result: list[Any] = []

    async def _submitter() -> None:
        try:
            async with session_factory() as s2:
                res = await hitl_service.submit_answer(
                    s2,
                    question_id=question_id,
                    caller_id=owner_id,
                    answer=valid_answer,
                    expected_context_version=1,
                    execution_id=exec_id,
                )
                await s2.commit()
                answer_task_result.append(res)
        except Exception as exc:
            answer_task_result.append(exc)

    submitter_task = asyncio.create_task(_submitter())

    # Wait 1.3 seconds so deadline (1s) expires
    await asyncio.sleep(1.3)
    release_lock.set()
    await holder_task
    await submitter_task

    assert len(answer_task_result) == 1
    assert isinstance(answer_task_result[0], ExecutionDeadlineExceededError)

    # Verify no state mutation survived
    async with session_factory() as session:
        q_db = (
            await session.execute(
                select(TravelerQuestionModel).where(TravelerQuestionModel.id == question_id)
            )
        ).scalar_one()
        assert q_db.status == "PENDING"
        assert q_db.answer_json is None

        exec_db = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one()
        assert exec_db.context_version == 1

        resume_evt = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == exec_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
            )
        ).scalar_one_or_none()
        assert resume_evt is None

