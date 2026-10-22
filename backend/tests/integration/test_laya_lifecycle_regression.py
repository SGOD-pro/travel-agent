"""Connected PostgreSQL lifecycle regression test suite conforming to spec 0004.

Proves the complete lifecycle:
acceptance -> collect dispatch -> result reconciliation -> optional browser decision -> resume,
guaranteeing:
1. No premature scheduling (solve_schedule runs only after verified evidence).
2. Deterministic policy gating and atomic persistence of decisions, tasks, outbox, and checkpoint.
3. Accurate collector task action ('collect') and validated queries/budget payload.
4. Pending task preservation across graph transitions.
5. Re-evaluation of evidence phase on resume after task completion.
6. Durable decision reuse via PostgreSQL repository.
7. Zero duplicate tasks across start or resume replays.
"""

from __future__ import annotations

import hashlib
import re
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.config import get_settings
from contracts.browser import (
    BrowserExtractionPort,
    BrowserRenderResult,
    BrowserRenderStatus,
    BrowserResourceUsage,
    BrowserTaskPayload,
)
from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionPhase,
    DecisionStatus,
    DecisionType,
    LayaDecisionPort,
    LayaDecisionRequest,
)
from functions.browser.consumer import process_record as browser_process_record
from functions.evidence_collector.consumer import process_record as evidence_process_record
from functions.evidence_collector.fixtures import (
    FixtureSearchAdapter,
    FixtureWebExtractionAdapter,
    RawObservation,
    compute_content_hash,
)
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.decision.models import LayaDecisionModel
from functions.orchestrator.decision.policy import LayaPolicyValidator
from functions.orchestrator.decision.repository import (
    LayaDecisionRepository,
    compute_canonical_replay_digest,
)
from functions.orchestrator.execution import (
    CompletionReconciliationService,
    ExecutionModel,
    ExecutionResumeEvent,
    ExecutionStartEvent,
    GraphRunnerService,
    OutboxEventModel,
    TaskCompletionEvent,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.graphs.trip_planner import (
    compile_trip_planning_workflow,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    route_evidence_decision_node,
)


class MockLifecycleLayaAdapter(LayaDecisionPort):
    """Controllable adapter returning deterministic responses for lifecycle testing."""

    def __init__(self, action: DecisionAction = DecisionAction.DISPATCH_BROWSER_ESCALATION) -> None:
        self.action = action
        self.call_count = 0
        self.queried_requests: list[LayaDecisionRequest] = []

    async def query_decision(self, request: LayaDecisionRequest) -> DecisionEnvelope:
        self.call_count += 1
        self.queried_requests.append(request)
        return DecisionEnvelope(
            decision_id=request.decision_id or uuid.uuid4(),
            decision_type=request.decision_type,
            phase=request.phase,
            execution_id=request.execution_id,
            trip_id=request.trip_id,
            idempotency_key=request.idempotency_key,
            allowed_choice=self.action,
            status=DecisionStatus.VALIDATED,
            confidence=0.88,
            answer_confidence=0.88,
            model_version="mock-lifecycle-laya",
            policy_version=LayaPolicyValidator.POLICY_VERSION,
            rationale=f"Mock decision for {self.action.value}",
            latency_ms=10,
            request_digest=request.context.get("request_digest", "digest-test"),
            created_at=datetime.now(UTC).isoformat(),
        )

    async def check_health(self) -> bool:
        return True


@pytest.fixture
async def session_factory() -> Any:
    """Provides a session factory prioritizing local PostgreSQL on localhost:5432."""
    s = socket.socket()
    s.settimeout(0.5)
    is_local = s.connect_ex(("127.0.0.1", 5432)) == 0
    s.close()

    settings = get_settings()
    url = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/travel_planning"
        if is_local
        else settings.DATABASE_URL
    )
    connect_args: dict[str, Any] = {"command_timeout": 60, "timeout": 30}
    if (
        "sslmode=require" in url
        or "ssl=require" in url
        or "aivencloud.com" in url
        or settings.DB_SSL
    ):
        connect_args["ssl"] = "require"
    if "sslmode=" in url:
        url = re.sub(r"[?&]sslmode=[^&]+", "", url)
        if "?" not in url and "&" in url:
            url = url.replace("&", "?", 1)

    test_engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await test_engine.dispose()


async def _create_test_execution(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    brief: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper creating a test execution record in PostgreSQL."""
    exec_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BYTEA NOT NULL,
                metadata BYTEA NOT NULL,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
        """))
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS checkpoint_writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BYTEA NOT NULL,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            )
        """))
        await session.commit()

    async with session_factory() as session:
        async with session.begin():
            execution = ExecutionModel(
                id=exec_id,
                owner_id=uuid.uuid4(),
                trip_id=None,
                workflow="TRAVEL",
                status="ACCEPTED",
                context_version=1,
                lease_token=None,
                lease_expiry=None,
                checkpoint_id=None,
                deadline_at=now + timedelta(hours=2),
                budget={
                    "remaining_tool_calls": 5,
                    "remaining_cost_minor": 500,
                    "currency": "INR",
                },
                created_at=now,
                updated_at=now,
            )
            session.add(execution)

    return exec_id, trip_id


@pytest.mark.asyncio
async def test_lifecycle_acceptance_to_collect_dispatch_and_no_premature_scheduling(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies acceptance -> collect dispatch with no premature scheduling and no duplicate tasks."""
    exec_id, trip_id = await _create_test_execution(session_factory)

    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    graph = compile_trip_planning_workflow(checkpointer=checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph)

    brief_data = {
        "origin_name": "Kolkata",
        "origin_lat": 22.5726,
        "origin_lng": 88.3639,
        "destinations": [
            {
                "name": "Digha",
                "latitude": 21.6266,
                "longitude": 87.5074,
                "stay_days": 2,
                "confidence": 0.95,
            }
        ],
        "start_date": "2026-11-01",
        "end_date": "2026-11-03",
        "travelers": 2,
        "rooms": 1,
        "budget": {"amount": "25000.00", "currency": "INR"},
    }

    start_event = ExecutionStartEvent(
        execution_id=exec_id,
        payload={
            "trip_id": str(trip_id),
            "brief": brief_data,
        },
    )

    # 1. Execute initial start
    result = await runner.handle_execution_start(event=start_event, graph=graph)

    assert result.status == "WAITING_TASKS"
    assert result.tasks_dispatched == 1
    assert len(result.task_ids) == 1
    collect_task_id = result.task_ids[0]

    # 2. Verify PostgreSQL persistence: task, outbox, and decision persisted atomically
    async with session_factory() as session:
        # Verify task
        task_stmt = select(TaskModel).where(TaskModel.id == collect_task_id)
        task = (await session.execute(task_stmt)).scalar_one_or_none()
        assert task is not None
        assert task.worker == "EVIDENCE"
        assert task.action == "collect"
        assert "queries" in task.payload_ref
        assert len(task.payload_ref["queries"]) >= 1
        assert task.payload_ref["budget"]["cost_per_call_minor"] == 50
        assert task.payload_ref["destination"] == "Digha"
        assert task.status == "PENDING"

        # Verify outbox event
        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.task_id == collect_task_id,
            OutboxEventModel.kind == "TASK_DISPATCH",
        )
        outbox_event = (await session.execute(outbox_stmt)).scalar_one_or_none()
        assert outbox_event is not None
        assert outbox_event.payload_ref["action"] == "collect"

        # Verify LAYA decision record in laya_decisions table
        decision_stmt = select(LayaDecisionModel).where(LayaDecisionModel.execution_id == exec_id)
        decision = (await session.execute(decision_stmt)).scalar_one_or_none()
        assert decision is not None
        assert decision.extract_phase() == DecisionPhase.SEARCH_COMPLETION
        assert decision.chosen_action == DecisionAction.DISPATCH_EVIDENCE_SEARCH.value

    # 3. Verify NO premature scheduling
    config = {"configurable": {"thread_id": str(exec_id)}}
    state_snap = await graph.aget_state(config=config)
    assert state_snap is not None
    assert state_snap.values.get("current_step") == "evidence_search_routed"
    assert "optimized_schedule" not in state_snap.values
    assert "proposal" not in state_snap.values

    # 4. Verify duplicate start idempotency (no duplicate tasks created)
    dup_result = await runner.handle_execution_start(event=start_event, graph=graph)
    assert dup_result.status == "WAITING_TASKS"
    assert dup_result.tasks_dispatched == 0

    async with session_factory() as session:
        count_stmt = select(sa.func.count(TaskModel.id)).where(TaskModel.execution_id == exec_id)
        task_count = (await session.execute(count_stmt)).scalar()
        assert task_count == 1


@pytest.mark.asyncio
async def test_lifecycle_reconciliation_to_direct_resolution_resume(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies collect task execution -> result reconciliation -> resume to proposal without browser escalation."""
    exec_id, trip_id = await _create_test_execution(session_factory)

    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    graph = compile_trip_planning_workflow(checkpointer=checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph)

    brief_data = {
        "origin_name": "Kolkata",
        "origin_lat": 22.5726,
        "origin_lng": 88.3639,
        "destinations": [
            {
                "name": "Digha",
                "latitude": 21.6266,
                "longitude": 87.5074,
                "stay_days": 2,
                "confidence": 0.95,
            }
        ],
        "start_date": "2026-11-01",
        "end_date": "2026-11-03",
        "travelers": 2,
        "rooms": 1,
        "budget": {"amount": "25000.00", "currency": "INR"},
    }

    start_event = ExecutionStartEvent(
        execution_id=exec_id,
        payload={"trip_id": str(trip_id), "brief": brief_data},
    )
    start_result = await runner.handle_execution_start(event=start_event, graph=graph)
    collect_task_id = start_result.task_ids[0]

    # 1. Complete collect task by executing the real evidence collector consumer
    async with session_factory() as session:
        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == exec_id,
            OutboxEventModel.task_id == collect_task_id,
            OutboxEventModel.kind == "TASK_DISPATCH",
        )
        dispatch_event = (await session.execute(outbox_stmt)).scalars().first()
    assert dispatch_event is not None

    digha_fixture = RawObservation.create(
        source_url="https://example.com/digha-guide",
        publisher="TravelGuide",
        excerpt_text="Digha beach attractions, lodging and hotels guide",
        excerpt_locator="p:1",
        extraction_method="SCRAPLING_HTTP",
        artifact_ref={"gps": {"lat": 21.6266, "lng": 87.5074}},
    )
    search_adapter = FixtureSearchAdapter(fixtures=[digha_fixture])
    extraction_adapter = FixtureWebExtractionAdapter({
        "https://example.com/digha-guide": digha_fixture,
    })

    await evidence_process_record(
        record={
            "body": {
                "kind": "TASK_DISPATCH",
                "event_id": str(dispatch_event.id),
                "execution_id": str(exec_id),
                "task_id": str(collect_task_id),
                "payload": dispatch_event.payload,
            }
        },
        session_factory=session_factory,
        adapter=search_adapter,
        extraction_adapter=extraction_adapter,
    )

    # Verify collector consumer committed observation and completed task under fencing
    async with session_factory() as session:
        res_stmt = select(WorkerResultModel).where(WorkerResultModel.task_id == collect_task_id)
        worker_res = (await session.execute(res_stmt)).scalar_one()
        result_id = worker_res.id

        obs_stmt = select(SourceObservationModel).where(SourceObservationModel.execution_id == exec_id)
        persisted_obs = (await session.execute(obs_stmt)).scalars().all()
        assert len(persisted_obs) >= 1
        assert "digha" in persisted_obs[0].canonical_url.lower()

    # 2. Reconcile task completion
    reconciler = CompletionReconciliationService(session_factory, default_graph=graph)
    reconcile_event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=collect_task_id,
        execution_id=exec_id,
        attempt=worker_res.attempt,
        result_id=result_id,
    )
    rec_result = await reconciler.reconcile_task_event(event=reconcile_event, graph=graph)
    assert rec_result.checkpoint_updated is True
    assert rec_result.graph_resumed is True

    # Verify EXECUTION_RESUME event emitted into outbox
    async with session_factory() as session:
        resume_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == exec_id,
            OutboxEventModel.kind == "EXECUTION_RESUME",
        )
        resume_event_row = (await session.execute(resume_stmt)).scalar_one_or_none()
        assert resume_event_row is not None

    # 3. Resume execution
    resume_event = ExecutionResumeEvent(
        execution_id=exec_id,
        triggered_by_task_id=collect_task_id,
    )
    resume_result = await runner.handle_execution_resume(event=resume_event, graph=graph)

    # 4. Verify terminal success: graph solved schedule, computed budget, and synthesized proposal
    assert resume_result.status == "SUCCEEDED"
    assert resume_result.tasks_dispatched == 0

    async with session_factory() as session:
        exec_stmt = select(ExecutionModel).where(ExecutionModel.id == exec_id)
        exec_model = (await session.execute(exec_stmt)).scalar_one()
        assert exec_model.status == "SUCCEEDED"

        # Verify Phase 3 EVALUATION decision persisted
        dec_stmt = select(LayaDecisionModel).where(
            LayaDecisionModel.execution_id == exec_id,
            LayaDecisionModel.chosen_action == DecisionAction.PROCEED_DETERMINISTIC_EVALUATION.value,
        )
        eval_dec = (await session.execute(dec_stmt)).scalar_one_or_none()
        assert eval_dec is not None


@pytest.mark.asyncio
async def test_lifecycle_reconciliation_to_browser_escalation_and_resume(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies collect task with browser requests -> browser escalation task -> second resume -> proposal."""
    exec_id, trip_id = await _create_test_execution(session_factory)

    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    graph = compile_trip_planning_workflow(checkpointer=checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph)

    mock_adapter = MockLifecycleLayaAdapter(action=DecisionAction.DISPATCH_BROWSER_ESCALATION)

    brief_data = {
        "origin_name": "Kolkata",
        "origin_lat": 22.5726,
        "origin_lng": 88.3639,
        "destinations": [
            {
                "name": "Digha",
                "latitude": 21.6266,
                "longitude": 87.5074,
                "stay_days": 2,
                "confidence": 0.95,
            }
        ],
        "start_date": "2026-11-01",
        "end_date": "2026-11-03",
        "travelers": 2,
        "rooms": 1,
        "budget": {"amount": "25000.00", "currency": "INR"},
    }

    start_event = ExecutionStartEvent(
        execution_id=exec_id,
        payload={
            "trip_id": str(trip_id),
            "brief": brief_data,
        },
    )

    with patch(
        "functions.decision_router.client.RemoteLayaDecisionAdapter.query_decision",
        side_effect=mock_adapter.query_decision,
    ):
        start_result = await runner.handle_execution_start(event=start_event, graph=graph)
        collect_task_id = start_result.task_ids[0]

        # 1. Complete collect task with browser_requests via real evidence collector consumer
        async with session_factory() as session:
            t_stmt = select(TaskModel).where(TaskModel.id == collect_task_id)
            t_row = (await session.execute(t_stmt)).scalar_one()
            p_dict = dict(t_row.payload_ref)
            p_dict["urls"] = ["https://hotel.example.com/rates"]
            t_row.payload_ref = p_dict
            await session.commit()

        async with session_factory() as session:
            outbox_stmt = select(OutboxEventModel).where(
                OutboxEventModel.execution_id == exec_id,
                OutboxEventModel.task_id == collect_task_id,
                OutboxEventModel.kind == "TASK_DISPATCH",
            )
            dispatch_event = (await session.execute(outbox_stmt)).scalars().first()
        assert dispatch_event is not None
        payload_container = dict(dispatch_event.payload or {})
        payload_container["payload"]["urls"] = ["https://hotel.example.com/rates"]

        class BrowserRequestExtractionAdapter:
            async def extract_page(
                self, page_url: str, category: str = "general"
            ) -> tuple[list[RawObservation], list[dict[str, Any]]]:
                placeholder_text = (
                    f"[DYNAMIC_PAGE: JAVASCRIPT_REQUIRED] Dynamic rendering required for {page_url} (DYNAMIC_JAVASCRIPT_REQUIRED)"
                )
                obs = RawObservation(
                    source_url=page_url,
                    canonical_url=page_url,
                    publisher="HotelPortal",
                    content_hash=compute_content_hash(placeholder_text),
                    retrieved_at=datetime.now(UTC),
                    excerpt_text=placeholder_text,
                    excerpt_locator="html",
                    extraction_method="NEEDS_BROWSER",
                    artifact_ref={"browser_reason": "DYNAMIC_JAVASCRIPT_REQUIRED"},
                )
                b_reqs = [
                    {
                        "url": page_url,
                        "canonical_url": page_url,
                        "category": "lodging",
                        "reason": "DYNAMIC_JAVASCRIPT_REQUIRED",
                    }
                ]
                return [obs], b_reqs

        digha_dest_obs = RawObservation.create(
            source_url="https://example.com/digha-overview",
            publisher="TravelGuide",
            excerpt_text="Digha sea beach attractions and travel guide",
            excerpt_locator="p:1",
            extraction_method="SCRAPLING_HTTP",
            artifact_ref={"gps": {"lat": 21.6266, "lng": 87.5074}},
        )
        search_adapter = FixtureSearchAdapter(fixtures=[digha_dest_obs])
        extraction_adapter = BrowserRequestExtractionAdapter()

        await evidence_process_record(
            record={
                "body": {
                    "kind": "TASK_DISPATCH",
                    "event_id": str(dispatch_event.id),
                    "execution_id": str(exec_id),
                    "task_id": str(collect_task_id),
                    "payload": payload_container,
                }
            },
            session_factory=session_factory,
            adapter=search_adapter,
            extraction_adapter=extraction_adapter,
        )

        async with session_factory() as session:
            res_stmt = select(WorkerResultModel).where(WorkerResultModel.task_id == collect_task_id)
            worker_res = (await session.execute(res_stmt)).scalar_one()
            result_id = worker_res.id

        # 2. Reconcile completion
        reconciler = CompletionReconciliationService(session_factory, default_graph=graph)
        await reconciler.reconcile_task_event(
            event=TaskCompletionEvent(
                event_id=uuid.uuid4(),
                task_id=collect_task_id,
                execution_id=exec_id,
                attempt=worker_res.attempt,
                result_id=result_id,
            ),
            graph=graph,
        )

        # 3. Resume 1: LAYA decision router selects DISPATCH_BROWSER_ESCALATION
        resume_event_1 = ExecutionResumeEvent(execution_id=exec_id, triggered_by_task_id=collect_task_id)
        resume_result_1 = await runner.handle_execution_resume(event=resume_event_1, graph=graph)

        # Verifies execution pauses at browser dispatch boundary without premature scheduling
        assert resume_result_1.status == "WAITING_TASKS"
        assert resume_result_1.tasks_dispatched == 1
        browser_task_id = resume_result_1.task_ids[0]

        async with session_factory() as session:
            browser_task = (await session.execute(select(TaskModel).where(TaskModel.id == browser_task_id))).scalar_one()
            assert browser_task.worker == "BROWSER"
            assert browser_task.action == "render"

            # Verify Phase 2 decision persisted
            phase2_dec = (
                await session.execute(
                    select(LayaDecisionModel).where(
                        LayaDecisionModel.execution_id == exec_id,
                        LayaDecisionModel.chosen_action == DecisionAction.DISPATCH_BROWSER_ESCALATION.value,
                    )
                )
            ).scalar_one_or_none()
            assert phase2_dec is not None

        # Checkpoint check: Still no premature scheduling
        snap = await graph.aget_state(config={"configurable": {"thread_id": str(exec_id)}})
        assert "optimized_schedule" not in snap.values

        # 4. Complete browser task via real browser consumer
        async with session_factory() as session:
            b_outbox_stmt = select(OutboxEventModel).where(
                OutboxEventModel.execution_id == exec_id,
                OutboxEventModel.task_id == browser_task_id,
                OutboxEventModel.kind == "TASK_DISPATCH",
            )
            b_dispatch_event = (await session.execute(b_outbox_stmt)).scalars().first()
        assert b_dispatch_event is not None

        class MockBrowserExtractionPort(BrowserExtractionPort):
            async def render_page(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
                return BrowserRenderResult(
                    status=BrowserRenderStatus.SUCCESS,
                    canonical_url=payload.canonical_url,
                    effective_url=payload.url,
                    content_hash=hashlib.sha256(b"digha rates 3500 INR").hexdigest(),
                    rendered_html_snippet="<html><body>Digha luxury hotel rates: 3500 INR per night</body></html>",
                    excerpt_text="Digha luxury hotel rates: 3500 INR per night",
                    excerpt_locator="body",
                    render_duration_ms=45,
                    usage=BrowserResourceUsage(duration_ms=45, memory_mb=1),
                )

        await browser_process_record(
            record={
                "body": {
                    "kind": "TASK_DISPATCH",
                    "event_id": str(b_dispatch_event.id),
                    "execution_id": str(exec_id),
                    "task_id": str(browser_task_id),
                    "payload": b_dispatch_event.payload,
                }
            },
            session_factory=session_factory,
            adapter=MockBrowserExtractionPort(),
        )

        async with session_factory() as session:
            b_res_stmt = select(WorkerResultModel).where(WorkerResultModel.task_id == browser_task_id)
            b_worker_res = (await session.execute(b_res_stmt)).scalar_one()
            b_result_id = b_worker_res.id

            # Verify browser observation was persisted in PostgreSQL with BROWSER_HTML
            b_obs_stmt = select(SourceObservationModel).where(
                SourceObservationModel.execution_id == exec_id,
                SourceObservationModel.extraction_method == "BROWSER_HTML",
            )
            b_obs = (await session.execute(b_obs_stmt)).scalar_one_or_none()
            assert b_obs is not None
            assert "digha" in b_obs.excerpt_text.lower()

        # Reconcile browser task completion
        await reconciler.reconcile_task_event(
            event=TaskCompletionEvent(
                event_id=uuid.uuid4(),
                task_id=browser_task_id,
                execution_id=exec_id,
                attempt=b_worker_res.attempt,
                result_id=b_result_id,
            ),
            graph=graph,
        )

        # 5. Resume 2: All browser requests handled -> advances to schedule solving -> SUCCEEDED
        resume_event_2 = ExecutionResumeEvent(execution_id=exec_id, triggered_by_task_id=browser_task_id)
        resume_result_2 = await runner.handle_execution_resume(event=resume_event_2, graph=graph)

        assert resume_result_2.status == "SUCCEEDED"
        assert resume_result_2.tasks_dispatched == 0

        final_snap = await graph.aget_state(config={"configurable": {"thread_id": str(exec_id)}})
        assert final_snap.values.get("status") == "completed"
        assert "proposal" in final_snap.values


@pytest.mark.asyncio
async def test_durable_decision_reuse_runtime_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies that existing decision in PostgreSQL is reused through graph node without invoking remote adapter."""
    exec_id, trip_id = await _create_test_execution(session_factory)

    # Compute expected idempotency key and store decision envelope in database
    phase = DecisionPhase.BROWSER_ELIGIBILITY
    eligible = [
        DecisionAction.DISPATCH_BROWSER_ESCALATION,
        DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
    ]
    context = {
        "execution_id": str(exec_id),
        "phase": phase.value,
        "category": "lodging",
        "destination": "Digha",
        "static_search_completed": True,
        "candidate_places_count": 1,
        "unhandled_browser_requests": 1,
        "browser_escalation_slots_remaining": 3,
        "sanitized_summary": "Discovered 1 places. Unhandled dynamic requests: 1.",
    }
    digest, idemp_key = compute_canonical_replay_digest(
        execution_id=exec_id,
        phase=phase,
        context_version=1,
        policy_version=LayaPolicyValidator.POLICY_VERSION,
        question_version="evidence_routing:v1",
        eligible_actions=eligible,
        sanitized_context=context,
    )

    stored_envelope = DecisionEnvelope(
        decision_id=uuid.uuid4(),
        decision_type=DecisionType.EVIDENCE_ROUTING,
        phase=phase,
        execution_id=exec_id,
        trip_id=trip_id,
        idempotency_key=idemp_key,
        allowed_choice=DecisionAction.DISPATCH_BROWSER_ESCALATION,
        status=DecisionStatus.VALIDATED,
        confidence=0.92,
        answer_confidence=0.92,
        model_version="persisted-durable-laya",
        policy_version=LayaPolicyValidator.POLICY_VERSION,
        rationale="Persisted decision reused cleanly",
        latency_ms=8,
        request_digest=digest,
        created_at=datetime.now(UTC).isoformat(),
    )

    async with session_factory() as session:
        repo = LayaDecisionRepository(session=session)
        await repo.persist_decision(
            envelope=stored_envelope,
            context_version=1,
            allowed_choices=eligible,
        )
        valid_obs = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=exec_id,
            source_url="https://example.com/digha-beach",
            canonical_url="https://example.com/digha-beach",
            publisher="TravelGuide",
            content_hash=hashlib.sha256(b"digha beach hotels").hexdigest(),
            retrieved_at=datetime.now(UTC),
            excerpt_text="Digha sea beach hotels and resorts directory",
            excerpt_locator="body",
            extraction_method="SCRAPLING_HTTP",
            artifact_ref={"gps": {"lat": 21.6266, "lng": 87.5074}},
        )
        session.add(valid_obs)
        await session.commit()

    # Create controllable adapter with call counter
    mock_adapter = MockLifecycleLayaAdapter()
    assert mock_adapter.call_count == 0

    brief_data = {
        "origin_name": "Kolkata",
        "origin_lat": 22.5726,
        "origin_lng": 88.3639,
        "destinations": [
            {
                "name": "Digha",
                "latitude": 21.6266,
                "longitude": 87.5074,
                "stay_days": 2,
                "confidence": 0.95,
            }
        ],
        "start_date": "2026-11-01",
        "end_date": "2026-11-03",
        "travelers": 2,
        "rooms": 1,
        "budget": {"amount": "25000.00", "currency": "INR"},
    }

    state: TripPlanningState = {
        "execution_id": str(exec_id),
        "trip_id": str(trip_id),
        "brief": brief_data,
        "discovered_places": [{"name": "Digha", "lat": 21.6266, "lng": 87.5074}],
        "task_outputs": {},
        "static_search_completed": True,
        "required_evidence_satisfied": True,
        "browser_requests": [{"url": "https://hotel.example.com/rates", "canonical_url": "https://hotel.example.com/rates"}],
        "remaining_search_budget": 0,
        "_laya_adapter": mock_adapter,
    }

    async with session_factory() as session:
        state["_db_session"] = session
        result = await route_evidence_decision_node(state)

    # Crucial proof: Remote adapter was NOT queried because decision was reused from PostgreSQL
    assert mock_adapter.call_count == 0, "Expected zero remote inference queries when reusable decision exists in database"
    assert result["evidence_routing_action"] == DecisionAction.DISPATCH_BROWSER_ESCALATION.value
    assert len(result["laya_decisions"]) == 1
    reused_record = result["laya_decisions"][0]
    assert reused_record["decision_id"] == str(stored_envelope.decision_id)
    assert reused_record["model_version"] == "persisted-durable-laya"
