"""Integration tests for TripPlannerGraph proposal publication, budget calculation, and versioning on isolated PostgreSQL.

Conforms to specification 0007 Slice 5, AC-1, AC-7, AC-8, and AC-9:
1. Complete proposal publication lifecycle: GraphRunnerService start -> WAITING_TASKS -> RouteOptimizerWorkerService
   claim and solve -> CompletionReconciliationService -> GraphRunnerService resume -> compute budget ->
   synthesize proposal -> publish proposal -> SUCCEEDED with current_route_id and itinerary_json set on trips.
2. Slice 4 database recovery path in real resumption: when reconciliation occurs without active graph,
   resumption recovers worker result from TaskModel and WorkerResultModel, consumes route, computes budget,
   and publishes proposal.
3. Concurrency control and versioning: if trips.current_version advances during planning, proposal publication
   detects the version mismatch under row lock, records PROPOSAL_SUPERSEDED event, transitions execution to
   SUPERSEDED, and leaves trip untouched.
4. Tenant isolation: foreign trip access raises PermissionError under transaction lock.
5. Replay safety and duplicate delivery: duplicate publication calls update the proposal without inserting
   duplicate TripDeltaModel rows.
6. Resilient routing unavailable outcome: when routing fails or is unavailable, verified stops and accommodations
   are preserved, road fuel is omitted without fabricating road travel, and proposal publishes with current_route_id None.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select, update

from contracts.money import BudgetCategory
from contracts.routing import RouteStatus
from functions.api.models import TripDeltaModel, TripModel
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.models import ClaimEvidenceModel, ClaimModel
from functions.orchestrator.evidence.schemas import (
    ClaimCategory,
    FreshnessStatus,
    VerificationStatus,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.fencing import WorkerFencingService
from functions.orchestrator.execution.models import (
    ExecutionModel,
    JobEventModel,
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
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    compile_trip_planning_workflow,
    publish_proposal_node,
)
from functions.route_optimizer.fixtures import MockValhallaClient
from functions.route_optimizer.service import RouteOptimizerWorkerService


async def _seed_test_execution_and_trip(
    session: Any,
    owner_id: uuid.UUID,
    trip_id: uuid.UUID,
    execution_id: uuid.UUID,
    workflow: str = "TRAVEL",
) -> None:
    now = datetime.now(UTC)
    trip = TripModel(
        id=trip_id,
        owner_id=owner_id,
        current_version=1,
        state="draft",
        created_at=now,
        updated_at=now,
    )
    session.add(trip)
    await session.flush()

    execution = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow=workflow,
        status="CREATED",
        context_version=1,
        deadline_at=now + timedelta(hours=2),
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    await session.flush()

    # Seed verified source observation and claim for destination Digha
    obs_digha = SourceObservationModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        source_url="https://wbtourism.gov.in/digha",
        canonical_url="https://wbtourism.gov.in/digha_destination",
        publisher="wbtourism.gov.in",
        content_hash=f"hash_{uuid.uuid4().hex}",
        retrieved_at=now,
        published_at=now - timedelta(days=2),
        excerpt_text="Digha is a premier coastal seaside resort town in West Bengal with beautiful beaches.",
        excerpt_locator="article p",
        extraction_method="SCRAPLING_HTTP",
        artifact_ref={
            "status": "SUCCESS",
            "gps": {"lat": 21.6266, "lng": 87.5074},
        },
        created_at=now,
    )
    session.add(obs_digha)
    await session.flush()

    claim_digha = ClaimModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        subject_id="Digha",
        category=ClaimCategory.ATTRACTION.value,
        typed_value={"verified_name": "Digha"},
        verification=VerificationStatus.VERIFIED.value,
        availability="AVAILABLE",
        freshness=FreshnessStatus.FRESH.value,
        valid_from=now - timedelta(days=30),
        valid_until=now + timedelta(days=60),
        created_at=now,
        updated_at=now,
    )
    session.add(claim_digha)
    await session.flush()

    link_digha = ClaimEvidenceModel(
        claim_id=claim_digha.id,
        observation_id=obs_digha.id,
        support_type="SUPPORTS",
        locator="article p",
        created_at=now,
    )
    session.add(link_digha)
    await session.flush()


def _build_test_initial_state(
    owner_id: uuid.UUID,
    trip_id: uuid.UUID,
    execution_id: uuid.UUID,
) -> dict[str, Any]:
    return {
        "trip_id": str(trip_id),
        "owner_id": str(owner_id),
        "execution_id": str(execution_id),
        "version": 1,
        "static_search_completed": True,
        "remaining_search_budget": 0,
        "brief": {
            "origin_name": "Kolkata",
            "origin_lat": 22.5726,
            "origin_lng": 88.3639,
            "destinations": [
                {
                    "name": "Digha",
                    "latitude": 21.6266,
                    "longitude": 87.5074,
                    "stay_days": 1,
                    "confidence": 1.0,
                }
            ],
            "start_date": "2026-11-01",
            "end_date": "2026-11-02",
            "travelers": 2,
            "rooms": 1,
            "budget": {"amount": "20000.00", "currency": "INR"},
            "preferred_modes": ["car_petrol"],
        },
        "discovered_places": [
            {
                "name": "Digha",
                "lat": 21.6266,
                "lng": 87.5074,
            },
            {
                "name": "Digha Marine Station",
                "category": "museum",
                "lat": 21.6300,
                "lng": 87.5100,
                "stay_minutes": 60,
            },
        ],
        "task_outputs": {
            "search-1": {
                "observations": [
                    {
                        "execution_id": str(execution_id),
                        "owner_id": str(owner_id),
                        "canonical_url": "https://digha.org/guide",
                        "content_hash": "hash_dest_digha",
                        "excerpt_text": "Digha is a coastal resort town in West Bengal with beaches and attractions.",
                        "support_type": "SUPPORTS",
                        "extraction_method": "SCRAPLING_HTTP",
                        "artifact_ref": {
                            "status": "SUCCESS",
                            "gps": {"lat": 21.6266, "lng": 87.5074},
                        },
                    },
                    {
                        "execution_id": str(execution_id),
                        "owner_id": str(owner_id),
                        "canonical_url": "https://digha.org/marine",
                        "content_hash": "hash_marine_cand",
                        "excerpt_text": "Digha Marine Station is a research facility and aquarium in Digha with entry fee 50 INR.",
                        "support_type": "SUPPORTS",
                        "extraction_method": "SCRAPLING_HTTP",
                        "artifact_ref": {
                            "status": "SUCCESS",
                            "gps": {"lat": 21.6300, "lng": 87.5100},
                        },
                    },
                ],
                "claims": [
                    {
                        "execution_id": str(execution_id),
                        "owner_id": str(owner_id),
                        "subject_id": "Digha",
                        "category": "ATTRACTION",
                        "verification": "VERIFIED",
                        "freshness": "FRESH",
                        "evidence_links": [
                            {
                                "support_type": "SUPPORTS",
                                "observation": {
                                    "extraction_method": "SCRAPLING_HTTP",
                                    "artifact_ref": {
                                        "status": "SUCCESS",
                                        "gps": {"lat": 21.6266, "lng": 87.5074},
                                    },
                                },
                            }
                        ],
                    },
                    {
                        "execution_id": str(execution_id),
                        "owner_id": str(owner_id),
                        "subject_id": "Digha Marine Station",
                        "category": "ATTRACTION",
                        "verification": "VERIFIED",
                        "freshness": "FRESH",
                        "evidence_links": [
                            {
                                "support_type": "SUPPORTS",
                                "observation": {
                                    "extraction_method": "SCRAPLING_HTTP",
                                    "artifact_ref": {
                                        "status": "SUCCESS",
                                        "gps": {"lat": 21.6300, "lng": 87.5100},
                                    },
                                },
                            }
                        ],
                    },
                ],
                "search_completed": True,
            }
        },
    }


@pytest.mark.asyncio
async def test_trip_planner_proposal_publication_end_to_end(
    session_factory: Any,
) -> None:
    """Verifies complete proposal publication lifecycle on live isolated PostgreSQL."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.commit()

    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    trip_graph = compile_trip_planning_workflow(checkpointer=checkpointer)

    runner = GraphRunnerService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )

    initial_payload = _build_test_initial_state(owner_id, trip_id, execution_id)

    # 1. Start execution -> halts at AWAIT_ROUTE_RESULTS with WAITING_TASKS
    start_event = ExecutionStartEvent(
        execution_id=execution_id,
        workflow="TRAVEL",
        context_version=1,
        payload=initial_payload,
    )
    start_res = await runner.handle_execution_start(event=start_event)
    assert start_res.status == "WAITING_TASKS"
    assert start_res.tasks_dispatched == 1

    # 2. Get task payload from outbox
    async with session_factory() as session:
        task_stmt = select(TaskModel).where(TaskModel.execution_id == execution_id)
        task_row = (await session.execute(task_stmt)).scalar_one()

        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "TASK_DISPATCH",
        )
        dispatch_outbox = (await session.execute(outbox_stmt)).scalar_one()
        dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

    # 3. Worker solves route and persists it
    mock_valhalla = MockValhallaClient()
    async with session_factory() as session:
        worker_service = RouteOptimizerWorkerService(
            session=session,
            valhalla_client=mock_valhalla,
        )
        persisted_route_id, worker_output = await worker_service.execute_task(dispatch_payload)
        await session.commit()

    assert persisted_route_id is not None
    assert worker_output.status == RouteStatus.FEASIBLE

    # 4. Reconcile completion
    async with session_factory() as session:
        task_updated = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_row.id))
        ).scalar_one()

    reconciler = CompletionReconciliationService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )
    completion_event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_updated.id,
        execution_id=execution_id,
        attempt=task_updated.attempt,
        event_type="TASK_COMPLETED",
        result_id=task_updated.result_id,
        output=worker_output.model_dump(mode="json"),
    )
    reconcile_res = await reconciler.reconcile_task_event(event=completion_event)
    assert reconcile_res.status == "APPLIED"

    # 5. Resume execution -> graph consumes route, computes budget, synthesizes proposal, publishes proposal
    async with session_factory() as session:
        resume_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "EXECUTION_RESUME",
        )
        resume_outbox = (await session.execute(resume_stmt)).scalar_one()

    resume_event = ExecutionResumeEvent(
        execution_id=execution_id,
        event_id=resume_outbox.id,
        triggered_by_task_id=task_row.id,
        context_version=1,
    )
    resume_res = await runner.handle_execution_resume(event=resume_event)
    assert resume_res.status == "SUCCEEDED"
    assert resume_res.tasks_dispatched == 0

    # 6. Database assertions for Slice 5 proposal publication
    async with session_factory() as session:
        # Trip aggregate root checks
        trip_stmt = select(TripModel).where(TripModel.id == trip_id)
        trip_after = (await session.execute(trip_stmt)).scalar_one()
        assert trip_after.state == "proposed"
        assert trip_after.current_route_id == persisted_route_id
        assert trip_after.itinerary_json is not None
        itinerary = trip_after.itinerary_json
        assert "schedule" in itinerary
        assert "budget_summary" in itinerary
        assert "budget_lines" in itinerary
        assert "weather_advisories" in itinerary
        assert itinerary.get("route_id") == str(persisted_route_id)

        # Trip delta checks
        delta_stmt = select(TripDeltaModel).where(
            TripDeltaModel.trip_id == trip_id,
            TripDeltaModel.base_version == 1,
        )
        delta_row = (await session.execute(delta_stmt)).scalar_one()
        assert delta_row.proposal_json is not None
        assert delta_row.proposal_json.get("route_id") == str(persisted_route_id)

        # Execution checks
        exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id)
        exec_row = (await session.execute(exec_stmt)).scalar_one()
        assert exec_row.status == "SUCCEEDED"

        # Timeline job event checks
        events_stmt = (
            select(JobEventModel)
            .where(
                JobEventModel.execution_id == execution_id,
                JobEventModel.event_type == "PROPOSAL_PUBLISHED",
            )
        )
        published_event = (await session.execute(events_stmt)).scalar_one()
        payload = published_event.safe_payload
        assert payload.get("trip_id") == str(trip_id)
        assert payload.get("route_id") == str(persisted_route_id)


@pytest.mark.asyncio
async def test_trip_planner_proposal_resumption_database_recovery_path(
    session_factory: Any,
) -> None:
    """Verifies that resumption recovers optimizer worker output from PostgreSQL when task_outputs is not in checkpoint."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.commit()

    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    trip_graph = compile_trip_planning_workflow(checkpointer=checkpointer)

    runner = GraphRunnerService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )

    initial_payload = _build_test_initial_state(owner_id, trip_id, execution_id)
    start_event = ExecutionStartEvent(
        execution_id=execution_id,
        workflow="TRAVEL",
        context_version=1,
        payload=initial_payload,
    )
    await runner.handle_execution_start(event=start_event)

    async with session_factory() as session:
        task_row = (
            await session.execute(select(TaskModel).where(TaskModel.execution_id == execution_id))
        ).scalar_one()
        dispatch_outbox = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == execution_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
            )
        ).scalar_one()
        dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

    mock_valhalla = MockValhallaClient()
    async with session_factory() as session:
        worker_service = RouteOptimizerWorkerService(
            session=session,
            valhalla_client=mock_valhalla,
        )
        persisted_route_id, worker_output = await worker_service.execute_task(dispatch_payload)
        await session.commit()

    # Reconcile completion WITHOUT active graph so checkpoint state does NOT receive task_outputs
    async with session_factory() as session:
        task_updated = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_row.id))
        ).scalar_one()

    reconciler = CompletionReconciliationService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )
    completion_event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_updated.id,
        execution_id=execution_id,
        attempt=task_updated.attempt,
        event_type="TASK_COMPLETED",
        result_id=task_updated.result_id,
        output=worker_output.model_dump(mode="json"),
    )
    reconcile_res = await reconciler.reconcile_task_event(event=completion_event)
    assert reconcile_res.status == "APPLIED"

    # Simulate checkpoint state where task_outputs was lost or absent:
    # explicitly clear task_outputs in checkpoint so resume MUST recover worker output from PostgreSQL
    checkpointer_config = {"configurable": {"thread_id": str(execution_id)}}
    await trip_graph.aupdate_state(
        config=checkpointer_config,
        values={"task_outputs": {}},
    )
    async with session_factory() as session:
        await checkpointer.flush(session, str(execution_id))
        await session.commit()

    # Resume execution -> graph loads checkpoint from database, discovers empty task_outputs,
    # and invokes database recovery path in consume_route_result_node to query WorkerResultModel
    async with session_factory() as session:
        resume_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "EXECUTION_RESUME",
        )
        resume_outbox = (await session.execute(resume_stmt)).scalar_one()

    resume_event = ExecutionResumeEvent(
        execution_id=execution_id,
        event_id=resume_outbox.id,
        triggered_by_task_id=task_row.id,
        context_version=1,
    )
    resume_res = await runner.handle_execution_resume(event=resume_event)
    assert resume_res.status == "SUCCEEDED"

    # Verify that the route was linked and proposal published through recovery path
    async with session_factory() as session:
        trip_after = (
            await session.execute(select(TripModel).where(TripModel.id == trip_id))
        ).scalar_one()
        assert trip_after.state == "proposed"
        assert trip_after.current_route_id == persisted_route_id
        assert trip_after.itinerary_json is not None


@pytest.mark.asyncio
async def test_trip_planner_proposal_version_conflict_superseded(
    session_factory: Any,
) -> None:
    """Verifies that concurrent trip modification detects version mismatch and sets SUPERSEDED."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.commit()

    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    trip_graph = compile_trip_planning_workflow(checkpointer=checkpointer)

    runner = GraphRunnerService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )

    initial_payload = _build_test_initial_state(owner_id, trip_id, execution_id)
    start_event = ExecutionStartEvent(
        execution_id=execution_id,
        workflow="TRAVEL",
        context_version=1,
        payload=initial_payload,
    )
    await runner.handle_execution_start(event=start_event)

    async with session_factory() as session:
        task_row = (
            await session.execute(select(TaskModel).where(TaskModel.execution_id == execution_id))
        ).scalar_one()
        dispatch_outbox = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == execution_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
            )
        ).scalar_one()
        dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

    mock_valhalla = MockValhallaClient()
    async with session_factory() as session:
        worker_service = RouteOptimizerWorkerService(
            session=session,
            valhalla_client=mock_valhalla,
        )
        persisted_route_id, worker_output = await worker_service.execute_task(dispatch_payload)
        await session.commit()

    async with session_factory() as session:
        task_updated = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_row.id))
        ).scalar_one()

    reconciler = CompletionReconciliationService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )
    completion_event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_updated.id,
        execution_id=execution_id,
        attempt=task_updated.attempt,
        event_type="TASK_COMPLETED",
        result_id=task_updated.result_id,
        output=worker_output.model_dump(mode="json"),
    )
    await reconciler.reconcile_task_event(event=completion_event)

    # Simulate concurrent trip edit: advance current_version from 1 to 2 before resumption
    async with session_factory() as session:
        await session.execute(
            update(TripModel)
            .where(TripModel.id == trip_id)
            .values(current_version=2, updated_at=datetime.now(UTC))
        )
        await session.commit()

    async with session_factory() as session:
        resume_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "EXECUTION_RESUME",
        )
        resume_outbox = (await session.execute(resume_stmt)).scalar_one()

    resume_event = ExecutionResumeEvent(
        execution_id=execution_id,
        event_id=resume_outbox.id,
        triggered_by_task_id=task_row.id,
        context_version=1,
    )
    resume_res = await runner.handle_execution_resume(event=resume_event)
    assert resume_res.status == "SUPERSEDED"

    # Database assertions: execution is SUPERSEDED, trip remains draft, stale data not written
    async with session_factory() as session:
        trip_after = (
            await session.execute(select(TripModel).where(TripModel.id == trip_id))
        ).scalar_one()
        assert trip_after.state == "draft"
        assert trip_after.current_route_id is None
        assert trip_after.itinerary_json is None
        assert trip_after.current_version == 2

        exec_after = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
        ).scalar_one()
        assert exec_after.status == "SUPERSEDED"

        events_stmt = (
            select(JobEventModel)
            .where(
                JobEventModel.execution_id == execution_id,
                JobEventModel.event_type == "PROPOSAL_SUPERSEDED",
            )
        )
        superseded_event = (await session.execute(events_stmt)).scalar_one()
        payload = superseded_event.safe_payload
        assert payload.get("trip_id") == str(trip_id)
        assert payload.get("expected_base_version") == 1
        assert payload.get("current_trip_version") == 2


@pytest.mark.asyncio
async def test_trip_planner_proposal_tenant_isolation_rejected(
    session_factory: Any,
) -> None:
    """Verifies that publishing proposal against a foreign trip raises PermissionError."""
    owner_id = uuid.uuid4()
    foreign_owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.commit()

    sample_state = _build_test_initial_state(foreign_owner_id, trip_id, execution_id)
    sample_state["owner_id"] = str(foreign_owner_id)

    with pytest.raises(PermissionError, match="Tenant isolation mismatch"):
        await publish_proposal_node(
            cast(TripPlanningState, sample_state),
            config={"configurable": {"session_factory": session_factory}},
        )

    # Trip remains untouched
    async with session_factory() as session:
        trip_after = (
            await session.execute(select(TripModel).where(TripModel.id == trip_id))
        ).scalar_one()
        assert trip_after.state == "draft"
        assert trip_after.current_route_id is None


@pytest.mark.asyncio
async def test_trip_planner_proposal_replay_duplicate_delivery_idempotent(
    session_factory: Any,
) -> None:
    """Verifies that duplicate publication calls update the proposal without inserting duplicate TripDeltaModel rows."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.commit()

    sample_state = _build_test_initial_state(owner_id, trip_id, execution_id)
    sample_state["proposal"] = {"schedule": {"stops": []}, "version": 1}

    # First publication
    res1 = await publish_proposal_node(
        cast(TripPlanningState, sample_state),
        config={"configurable": {"session_factory": session_factory}},
    )
    assert res1["proposal_status"] == "PUBLISHED"

    # Second publication with updated payload (replay)
    sample_state["proposal"] = {"schedule": {"stops": ["updated"]}, "version": 1}
    res2 = await publish_proposal_node(
        cast(TripPlanningState, sample_state),
        config={"configurable": {"session_factory": session_factory}},
    )
    assert res2["proposal_status"] == "PUBLISHED"

    # Assert exactly 1 TripDeltaModel exists
    async with session_factory() as session:
        delta_stmt = select(TripDeltaModel).where(
            TripDeltaModel.trip_id == trip_id,
            TripDeltaModel.base_version == 1,
        )
        deltas = (await session.execute(delta_stmt)).scalars().all()
        assert len(deltas) == 1
        assert deltas[0].proposal_json == {"schedule": {"stops": ["updated"]}, "version": 1}


@pytest.mark.asyncio
async def test_trip_planner_proposal_routing_unavailable_resilience(
    session_factory: Any,
) -> None:
    """Verifies that routing provider failure preserves verified candidates, omits road fuel, and publishes proposal with route_id None."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.commit()

    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    trip_graph = compile_trip_planning_workflow(checkpointer=checkpointer)

    runner = GraphRunnerService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )

    initial_payload = _build_test_initial_state(owner_id, trip_id, execution_id)
    start_event = ExecutionStartEvent(
        execution_id=execution_id,
        workflow="TRAVEL",
        context_version=1,
        payload=initial_payload,
    )
    await runner.handle_execution_start(event=start_event)

    async with session_factory() as session:
        task_row = (
            await session.execute(select(TaskModel).where(TaskModel.execution_id == execution_id))
        ).scalar_one()

    # Simulate worker returning ROUTING_UNAVAILABLE (e.g. Valhalla outage)
    unavailable_output = {
        "worker": "OPTIMIZER",
        "action": "solve",
        "status": "FAILED",
        "routing_status": "ROUTING_UNAVAILABLE",
        "error": {
            "reason": "ValhallaConnectionError: VALHALLA_ENDPOINT_URL is unreachable",
        },
    }

    async with session_factory() as session:
        fencing = WorkerFencingService(session)
        claim = await fencing.claim_task(
            message={
                "task_id": task_row.id,
                "execution_id": execution_id,
                "attempt": task_row.attempt,
            },
            worker="OPTIMIZER",
            action="solve",
        )
        result_id = await fencing.complete_task(
            execution_id=execution_id,
            task_id=task_row.id,
            lease_token=claim.lease_token,
            output=unavailable_output,
        )
        await session.commit()

    reconciler = CompletionReconciliationService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )
    completion_event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_row.id,
        execution_id=execution_id,
        attempt=claim.attempt,
        event_type="TASK_COMPLETED",
        result_id=result_id,
        output=unavailable_output,
    )
    await reconciler.reconcile_task_event(event=completion_event)

    # Resume execution -> graph consumes ROUTING_UNAVAILABLE, computes budget without road fuel, publishes proposal
    async with session_factory() as session:
        resume_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "EXECUTION_RESUME",
        )
        resume_outbox = (await session.execute(resume_stmt)).scalar_one()

    resume_event = ExecutionResumeEvent(
        execution_id=execution_id,
        event_id=resume_outbox.id,
        triggered_by_task_id=task_row.id,
        context_version=1,
    )
    resume_res = await runner.handle_execution_resume(event=resume_event)
    assert resume_res.status == "SUCCEEDED"

    # Verify database: trip proposed, current_route_id None, itinerary contains routing diagnostics
    async with session_factory() as session:
        trip_after = (
            await session.execute(select(TripModel).where(TripModel.id == trip_id))
        ).scalar_one()
        assert trip_after.state == "proposed"
        assert trip_after.current_route_id is None
        assert trip_after.itinerary_json is not None
        itinerary = trip_after.itinerary_json
        assert itinerary.get("routing_status") == "ROUTING_UNAVAILABLE"
        assert itinerary.get("route_id") is None
        # Budget lines must omit fuel because no road route was feasible
        budget_categories = [b.get("category") for b in itinerary.get("budget_lines", [])]
        assert BudgetCategory.TRANSPORT.value not in budget_categories
