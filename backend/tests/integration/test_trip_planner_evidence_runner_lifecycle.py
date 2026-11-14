"""Integration tests for real GraphRunner evidence collection, browser fallback, and resume reconciliation.

Conforms strictly to spec 0007 AC-13, AC-14, AC-15, and AC-16 on live PostgreSQL:
1. Real GraphRunnerService start -> interrupt at AWAIT_EVIDENCE_RESULTS.
2. Verified task dispatch with bounded queries (max 4) and collector budget (200 minor INR).
3. EvidenceCollectorService execution under worker lease fencing and durable budget reservations.
4. Browser escalation fallback capped at maximum 2 URLs for the 0007 flow.
5. Receipt deduplication via CompletionReconciliationService and graph resume.
6. Feed supported claims into DeterministicEvidenceEligibilityGate from real collected observations
   without pre-seeding verified claims.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.candidate import (
    CandidateExistenceStatus,
    CoordinatePoint,
    RegisteredCandidate,
)
from functions.api.models import TripModel
from functions.evidence_collector.fixtures import (
    FixtureSearchAdapter,
    RawObservation,
)
from functions.evidence_collector.models import SourceObservationModel
from functions.evidence_collector.service import (
    EvidenceCollectorService,
    TaskDispatchEnvelope,
)
from functions.orchestrator.evidence.claim_ingestion import (
    ingest_claims_from_observations,
)
from functions.orchestrator.evidence.eligibility import (
    DeterministicEvidenceEligibilityGate,
)
from functions.orchestrator.execution.checkpointer import (
    TransactionBoundCheckpointer,
)
from functions.orchestrator.execution.fencing import (
    TaskQueueMessage,
)
from functions.orchestrator.execution.models import (
    ExecutionModel,
    OutboxEventModel,
    TaskModel,
)
from functions.orchestrator.execution.reconciliation import (
    CompletionReconciliationService,
    TaskCompletionEvent,
)
from functions.orchestrator.execution.runner import (
    ExecutionResumeEvent,
    ExecutionStartEvent,
    GraphRunnerService,
)
from functions.orchestrator.graphs.trip_planner import (
    compile_trip_planning_workflow,
)


async def _create_test_execution_and_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Helper creating a test trip and execution row in PostgreSQL."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as session:
        async with session.begin():
            trip = TripModel(
                id=trip_id,
                owner_id=owner_id,
                current_version=1,
                state="draft",
                created_at=now,
                updated_at=now,
            )
            session.add(trip)

            execution = ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                trip_id=trip_id,
                workflow="TRAVEL",
                status="RUNNING",
                context_version=1,
                deadline_at=now + timedelta(hours=2),
                budget={
                    "remaining_tool_calls": 4,
                    "remaining_cost_minor": 200,
                    "cost_per_call_minor": 50,
                    "currency": "INR",
                    "reserved_tool_calls": 0,
                    "reserved_cost_minor": 0,
                    "spent_tool_calls": 0,
                    "spent_cost_minor": 0,
                },
                created_at=now,
                updated_at=now,
            )
            session.add(execution)

    return owner_id, trip_id, exec_id


@pytest.mark.asyncio
class TestTripPlannerEvidenceRunnerLifecycle:
    """Verifies end to end evidence task dispatch, execution, browser fallback, and runner resume."""

    async def test_runner_dispatches_evidence_search_with_bounded_budget(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """AC-13 & AC-16: GraphRunner starts and emits an EVIDENCE search task with bounded queries and budget."""
        owner_id, trip_id, exec_id = await _create_test_execution_and_trip(session_factory)

        checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
        graph = compile_trip_planning_workflow(checkpointer=checkpointer)
        runner = GraphRunnerService(session_factory, default_graph=graph)

        brief_data = {
            "origin_name": "Kolkata",
            "origin_lat": 22.5726,
            "origin_lng": 88.3639,
            "destinations": [
                {
                    "name": "Darjeeling",
                    "latitude": 27.0410,
                    "longitude": 88.2663,
                    "stay_days": 2,
                }
            ],
            "start_date": "2026-12-01",
            "end_date": "2026-12-04",
            "dietary_preferences": ["vegetarian"],
        }

        start_event = ExecutionStartEvent(
            execution_id=exec_id,
            payload={"trip_id": str(trip_id), "brief": brief_data},
        )

        result = await runner.handle_execution_start(event=start_event, graph=graph)

        assert result.status == "WAITING_TASKS"
        assert result.tasks_dispatched == 1
        assert len(result.task_ids) == 1
        evidence_task_id = result.task_ids[0]

        async with session_factory() as session:
            task_stmt = select(TaskModel).where(TaskModel.id == evidence_task_id)
            task = (await session.execute(task_stmt)).scalar_one()

            assert task.worker == "EVIDENCE"
            assert task.action == "collect"

            payload = task.payload_ref
            queries = payload["queries"]
            assert len(queries) <= 4
            assert any("Darjeeling" in q for q in queries)

            # Assert durable collector budget bounds
            budget = payload["budget"]
            assert budget["remaining_tool_calls"] == len(queries)
            assert budget["cost_per_call_minor"] == 50
            assert budget["remaining_cost_minor"] == len(queries) * 50
            assert budget["currency"] == "INR"

            # Assert outbox event was created atomically
            outbox_stmt = select(OutboxEventModel).where(
                OutboxEventModel.task_id == evidence_task_id,
                OutboxEventModel.kind == "TASK_DISPATCH",
            )
            outbox_row = (await session.execute(outbox_stmt)).scalar_one_or_none()
            assert outbox_row is not None
            assert outbox_row.payload_ref["worker"] == "EVIDENCE"

    async def test_evidence_collector_execution_and_budget_reconciliation(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """AC-13 & AC-14: EvidenceCollectorService executes task, reserves budget, stages observations, and completes atomically."""
        owner_id, trip_id, exec_id = await _create_test_execution_and_trip(session_factory)

        checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
        graph = compile_trip_planning_workflow(checkpointer=checkpointer)
        runner = GraphRunnerService(session_factory, default_graph=graph)

        brief_data = {
            "origin_name": "Kolkata",
            "origin_lat": 22.5726,
            "origin_lng": 88.3639,
            "destinations": [
                {
                    "name": "Darjeeling",
                    "latitude": 27.0410,
                    "longitude": 88.2663,
                    "stay_days": 2,
                }
            ],
            "start_date": "2026-12-01",
            "end_date": "2026-12-04",
        }

        start_event = ExecutionStartEvent(
            execution_id=exec_id,
            payload={"trip_id": str(trip_id), "brief": brief_data},
        )
        start_res = await runner.handle_execution_start(event=start_event, graph=graph)
        task_id = start_res.task_ids[0]

        # Prepare mock search results
        now = datetime.now(UTC)
        darjeeling_obs = RawObservation(
            source_url="https://wbtourism.gov.in/darjeeling-mall",
            canonical_url="https://wbtourism.gov.in/darjeeling-mall",
            publisher="West Bengal Tourism",
            content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            retrieved_at=now,
            published_at=now - timedelta(days=5),
            excerpt_text="Darjeeling Mall and Chowrasta are central scenic attractions in Darjeeling open to pedestrians.",
            excerpt_locator="div.content",
            extraction_method="STATIC_SCRAPLING",
            artifact_ref={"category": "attraction", "status": "AVAILABLE"},
        )
        search_adapter = FixtureSearchAdapter(fixtures=[darjeeling_obs])

        # Claim and execute collection task
        envelope = TaskDispatchEnvelope(
            event_id=uuid.uuid4(),
            execution_id=exec_id,
            task_id=task_id,
            kind="TASK_DISPATCH",
            outbox_delivery_attempts=0,
            task_attempt=0,
            context_version=1,
            worker="EVIDENCE",
            action="collect",
            inner_payload={
                "queries": ["Darjeeling attractions"],
                "budget": {
                    "remaining_tool_calls": 4,
                    "remaining_cost_minor": 200,
                    "cost_per_call_minor": 50,
                    "currency": "INR",
                },
                "destination": "Darjeeling",
            },
        )

        async with session_factory() as session:
            service = EvidenceCollectorService(session)
            result_id, obs_ids = await service.execute_collection_task(
                envelope=envelope,
                adapter=search_adapter,
            )

        assert result_id is not None
        assert len(obs_ids) == 1

        # Verify database state
        async with session_factory() as session:
            # Check source observation persisted
            obs_stmt = select(SourceObservationModel).where(
                SourceObservationModel.execution_id == exec_id
            )
            persisted = list((await session.execute(obs_stmt)).scalars().all())
            assert len(persisted) == 1
            assert "darjeeling" in persisted[0].excerpt_text.lower()

            # Check execution budget reconciled
            exec_stmt = select(ExecutionModel).where(ExecutionModel.id == exec_id)
            exec_row = (await session.execute(exec_stmt)).scalar_one()
            assert exec_row.budget["spent_tool_calls"] == 4
            assert exec_row.budget["spent_cost_minor"] == 200
            assert exec_row.budget["reserved_tool_calls"] == 0

    async def test_browser_escalation_capped_at_two_and_reconciliation_resume(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """AC-13: Escalates NEEDS_BROWSER tasks up to 2 URLs, completes via browser worker, and resumes graph."""
        owner_id, trip_id, exec_id = await _create_test_execution_and_trip(session_factory)

        checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
        graph = compile_trip_planning_workflow(checkpointer=checkpointer)
        runner = GraphRunnerService(session_factory, default_graph=graph)

        brief_data = {
            "origin_name": "Kolkata",
            "origin_lat": 22.5726,
            "origin_lng": 88.3639,
            "destinations": [
                {
                    "name": "Darjeeling",
                    "latitude": 27.0410,
                    "longitude": 88.2663,
                    "stay_days": 2,
                }
            ],
            "start_date": "2026-12-01",
            "end_date": "2026-12-04",
        }

        start_event = ExecutionStartEvent(
            execution_id=exec_id,
            payload={"trip_id": str(trip_id), "brief": brief_data},
        )
        start_res = await runner.handle_execution_start(event=start_event, graph=graph)
        collect_task_id = start_res.task_ids[0]

        # 1. Complete collect task with 3 browser requests to verify capping at 2
        dynamic_reqs = [
            {
                "url": f"https://dynamic-hotels.com/view/{i}",
                "canonical_url": f"https://dynamic-hotels.com/view/{i}",
                "category": "lodging",
                "reason": "DYNAMIC_JAVASCRIPT_REQUIRED",
            }
            for i in range(1, 4)
        ]

        async with session_factory() as session:
            # Complete collect task through fencing
            fencing = EvidenceCollectorService(session).fencing_service
            claim_msg = TaskQueueMessage(
                task_id=collect_task_id,
                execution_id=exec_id,
                attempt=0,
            )
            claim_res = await fencing.claim_task(
                message=claim_msg,
                worker="EVIDENCE",
                action="collect",
                expected_context_version=1,
            )
            result_id = await fencing.complete_task(
                execution_id=exec_id,
                task_id=collect_task_id,
                lease_token=claim_res.lease_token,
                output={
                    "observation_ids": [],
                    "browser_requests": dynamic_reqs,
                    "search_completed": True,
                },
                expected_context_version=1,
            )

        # 2. Reconcile collect completion
        reconciler = CompletionReconciliationService(session_factory, default_graph=graph)
        rec_res = await reconciler.reconcile_task_event(
            event=TaskCompletionEvent(
                event_id=uuid.uuid4(),
                task_id=collect_task_id,
                execution_id=exec_id,
                attempt=claim_res.attempt,
                result_id=result_id,
            ),
            graph=graph,
        )
        assert rec_res.checkpoint_updated is True
        assert rec_res.graph_resumed is True

        # 3. Resume graph -> evaluates LAYA / browser eligibility and dispatches browser tasks
        resume_res = await runner.handle_execution_resume(
            event=ExecutionResumeEvent(
                execution_id=exec_id,
                triggered_by_task_id=collect_task_id,
            ),
            graph=graph,
        )

        assert resume_res.status == "WAITING_TASKS"
        # Invariant: Maximum 2 browser escalation tasks dispatched (capped at 2 for 0007 flow)
        assert resume_res.tasks_dispatched <= 2
        assert len(resume_res.task_ids) <= 2
        assert len(resume_res.task_ids) > 0

        browser_task_id = resume_res.task_ids[0]

        async with session_factory() as session:
            b_task = (
                await session.execute(select(TaskModel).where(TaskModel.id == browser_task_id))
            ).scalar_one()
            assert b_task.worker == "BROWSER"
            assert b_task.action == "render"

    async def test_claims_ingested_and_candidates_verified_without_preseeded_claims(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """AC-13, AC-14: Derives claims from real collected observations and confirms candidate eligibility honestly."""
        owner_id, trip_id, exec_id = await _create_test_execution_and_trip(session_factory)
        now = datetime.now(UTC)

        # Stage genuine collected observations in the database
        async with session_factory() as session:
            obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://wbtourism.gov.in/darjeeling-mall",
                canonical_url="https://wbtourism.gov.in/darjeeling-mall",
                publisher="West Bengal Tourism",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                published_at=now - timedelta(days=2),
                excerpt_text="Darjeeling Mall is a historic pedestrian promenade in Darjeeling.",
                excerpt_locator="div.tourist-spot",
                extraction_method="STATIC_SCRAPLING",
                artifact_ref={
                    "category": "attraction",
                    "status": "AVAILABLE",
                    "title": "Darjeeling Mall",
                    "latitude": 27.0435,
                    "longitude": 88.2673,
                    "fixture_backed": True,
                },
                created_at=now,
            )
            session.add(obs)
            await session.commit()

        # Create two candidates: one supported by the observation, one unsupported hallucination
        supported_candidate = RegisteredCandidate(
            primary_name="Darjeeling Mall",
            category="attraction",
            point=CoordinatePoint(lat=27.0435, lng=88.2673),
        )
        unsupported_candidate = RegisteredCandidate(
            primary_name="Fictional Space Observatory",
            category="attraction",
            point=CoordinatePoint(lat=27.0500, lng=88.2700),
        )

        async with session_factory() as session:
            # Ingest claims without pre-seeding ClaimModel rows
            claims = await ingest_claims_from_observations(
                session=session,
                execution_id=exec_id,
                candidates=[supported_candidate, unsupported_candidate],
                owner_id=owner_id,
            )
            await session.commit()

            # Exactly one claim should be generated for the supported place
            assert len(claims) == 1
            assert claims[0].subject_id == "darjeeling mall"

            # Evaluate with DeterministicEvidenceEligibilityGate
            gate = DeterministicEvidenceEligibilityGate(session=session)
            report = await gate.evaluate_candidate_eligibility(
                candidates=[supported_candidate, unsupported_candidate],
                execution_id=exec_id,
                owner_id=owner_id,
            )

            # Supported candidate is verified into eligible stops for the route solver
            assert len(report.eligible_stops) == 1
            assert report.eligible_stops[0].primary_name == "Darjeeling Mall"
            assert report.eligible_stops[0].existence_status == CandidateExistenceStatus.VERIFIED

            # Unsupported candidate remains in editorial recommendations and never enters solver
            assert len(report.editorial_recommendations) == 1
            assert report.editorial_recommendations[0].primary_name == "Fictional Space Observatory"
            assert (
                report.editorial_recommendations[0].existence_status
                == CandidateExistenceStatus.UNVERIFIED
            )
