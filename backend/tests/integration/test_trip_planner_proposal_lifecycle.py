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
import sqlalchemy as sa
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
    compute_budget_node,
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


def _seed_canonical_place(
    session: Any,
    place_id: uuid.UUID,
    primary_name: str,
    lat: float = 21.6266,
    lng: float = 87.5074,
    locality: str = "West Bengal",
) -> None:
    from geoalchemy2.elements import WKTElement

    from functions.orchestrator.places.models import CanonicalPlaceModel

    place = CanonicalPlaceModel(
        id=place_id,
        primary_name=primary_name,
        locality=locality,
        point=WKTElement(f"POINT({lng} {lat})", srid=4326),
        names=[primary_name],
        access_points=[],
        provider_ids={},
        access_status="PUBLIC",
        is_candidate=False,
    )
    session.add(place)


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
    held_lease = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(
                status="RUNNING",
                lease_token=held_lease,
                lease_expiry=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await session.commit()

    sample_state = _build_test_initial_state(foreign_owner_id, trip_id, execution_id)
    sample_state["owner_id"] = str(foreign_owner_id)

    with pytest.raises(PermissionError, match="Tenant isolation mismatch"):
        await publish_proposal_node(
            cast(TripPlanningState, sample_state),
            config={
                "configurable": {
                    "session_factory": session_factory,
                    "runner_lease": held_lease,
                }
            },
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
    held_lease = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(
                status="RUNNING",
                lease_token=held_lease,
                lease_expiry=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await session.commit()

    sample_state = _build_test_initial_state(owner_id, trip_id, execution_id)
    sample_state["proposal"] = {"schedule": {"stops": []}, "version": 1}

    # First publication
    res1 = await publish_proposal_node(
        cast(TripPlanningState, sample_state),
        config={
            "configurable": {
                "session_factory": session_factory,
                "runner_lease": held_lease,
            }
        },
    )
    assert res1["proposal_status"] == "PUBLISHED"

    # Second publication with updated payload (replay)
    sample_state["proposal"] = {"schedule": {"stops": ["updated"]}, "version": 1}
    res2 = await publish_proposal_node(
        cast(TripPlanningState, sample_state),
        config={
            "configurable": {
                "session_factory": session_factory,
                "runner_lease": held_lease,
            }
        },
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


@pytest.mark.asyncio
async def test_trip_planner_proposal_lost_runner_lease_fencing_on_postgres(
    session_factory: Any,
) -> None:
    """Verifies Finding 3: lost runner lease halts publication and does not write trip or delta."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    held_lease = uuid.uuid4()
    stale_lease = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        # Execution has an active lease held by runner A
        await session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(
                status="RUNNING",
                lease_token=held_lease,
                lease_expiry=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await session.commit()

    sample_state = _build_test_initial_state(owner_id, trip_id, execution_id)
    sample_state["proposal"] = {"schedule": {"stops": ["test"]}, "version": 1}

    # Runner B attempts publication with a stale or mismatched lease
    res = await publish_proposal_node(
        cast(TripPlanningState, sample_state),
        config={
            "configurable": {
                "session_factory": session_factory,
                "runner_lease": stale_lease,
            }
        },
    )
    assert res["status"] == "FAILED"
    assert res["current_step"] == "proposal_publication_lost_lease"

    # Trip and deltas must remain untouched in PostgreSQL
    async with session_factory() as session:
        trip_after = (
            await session.execute(select(TripModel).where(TripModel.id == trip_id))
        ).scalar_one()
        assert trip_after.state == "draft"
        assert trip_after.current_route_id is None
        assert trip_after.itinerary_json is None

        delta_count = (
            await session.execute(
                select(sa.func.count(TripDeltaModel.id)).where(TripDeltaModel.trip_id == trip_id)
            )
        ).scalar()
        assert delta_count == 0


@pytest.mark.asyncio
async def test_trip_planner_proposal_superseded_replay_rejected_on_postgres(
    session_factory: Any,
) -> None:
    """Verifies Finding 4: duplicate delivery or replay across start, resume, and reconciliation does not restart SUPERSEDED execution."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        task = TaskModel(
            id=task_id,
            execution_id=execution_id,
            worker="OPTIMIZER",
            action="route",
            payload_ref={},
            input_hash="hash_superseded",
            idempotency_key=f"{execution_id}:task:{task_id}",
            attempt=1,
            max_attempts=3,
            context_version=1,
            status="RUNNING",
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        # Mark execution as SUPERSEDED
        await session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(status="SUPERSEDED")
        )
        await session.commit()

    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    trip_graph = compile_trip_planning_workflow(checkpointer=checkpointer)
    runner = GraphRunnerService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )

    # 1. Duplicate start event on superseded execution
    initial_payload = _build_test_initial_state(owner_id, trip_id, execution_id)
    start_event = ExecutionStartEvent(
        execution_id=execution_id,
        workflow="TRAVEL",
        context_version=1,
        payload=initial_payload,
    )
    start_res = await runner.handle_execution_start(event=start_event)
    assert start_res.status == "SUPERSEDED"
    assert start_res.graph_advanced is False

    # 2. Duplicate resume event on superseded execution
    resume_event = ExecutionResumeEvent(
        execution_id=execution_id,
        event_id=uuid.uuid4(),
        triggered_by_task_id=task_id,
        context_version=1,
    )
    resume_res = await runner.handle_execution_resume(event=resume_event)
    assert resume_res.status == "SUPERSEDED"
    assert resume_res.graph_advanced is False

    # 3. Duplicate task completion event on superseded execution
    reconciler = CompletionReconciliationService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )
    completion_event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_id,
        execution_id=execution_id,
        attempt=1,
        event_type="TASK_COMPLETED",
        result_id=None,
        output={},
    )
    reconcile_res = await reconciler.reconcile_task_event(event=completion_event)
    assert reconcile_res.status == "PARENT_TERMINAL"
    assert reconcile_res.graph_resumed is False

    # Execution remains SUPERSEDED, trip untouched
    async with session_factory() as session:
        exec_after = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
        ).scalar_one()
        assert exec_after.status == "SUPERSEDED"


@pytest.mark.asyncio
async def test_trip_planner_proposal_crash_retry_reconciliation_on_postgres(
    session_factory: Any,
) -> None:
    """Verifies Finding 3: real crash recovery with GraphRunnerService and TransactionBoundCheckpointer."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.commit()

    checkpointer1 = TransactionBoundCheckpointer(session_factory=session_factory)
    trip_graph1 = compile_trip_planning_workflow(checkpointer=checkpointer1)
    runner1 = GraphRunnerService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph1},
    )

    initial_payload = _build_test_initial_state(owner_id, trip_id, execution_id)
    start_event = ExecutionStartEvent(
        execution_id=execution_id,
        workflow="TRAVEL",
        context_version=1,
        payload=initial_payload,
    )
    start_res = await runner1.handle_execution_start(event=start_event)
    assert start_res.status == "WAITING_TASKS"

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
        workflow_graphs={"TRAVEL": trip_graph1},
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

    # Intercept runner1._apply_graph_outcome to simulate crash right after publication
    async def simulated_crash_apply(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Simulated crash right before runner outcome completion")

    runner1._apply_graph_outcome = simulated_crash_apply  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="Simulated crash right before runner outcome completion"):
        await runner1.handle_execution_resume(event=resume_event)

    # In PostgreSQL: publication succeeded in node, but runner1 crashed before committing completion
    async with session_factory() as session:
        trip_after_crash = (
            await session.execute(select(TripModel).where(TripModel.id == trip_id))
        ).scalar_one()
        assert trip_after_crash.state == "proposed"
        assert trip_after_crash.current_route_id == persisted_route_id

        exec_after_crash = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
        ).scalar_one()
        assert exec_after_crash.status == "RUNNING"

        # Simulate runner1 lease expiration
        await session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(lease_expiry=datetime.now(UTC) - timedelta(seconds=5))
        )
        await session.commit()

    # Restart with fresh checkpointer and fresh runner
    checkpointer2 = TransactionBoundCheckpointer(session_factory=session_factory)
    trip_graph2 = compile_trip_planning_workflow(checkpointer=checkpointer2)
    runner2 = GraphRunnerService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph2},
    )

    retry_res = await runner2.handle_execution_resume(event=resume_event)
    assert retry_res.status == "SUCCEEDED"

    # Verify persisted checkpoint, trip, delta, events and terminal status
    async with session_factory() as session:
        trip_final = (
            await session.execute(select(TripModel).where(TripModel.id == trip_id))
        ).scalar_one()
        assert trip_final.state == "proposed"
        assert trip_final.current_route_id == persisted_route_id
        assert trip_final.itinerary_json is not None

        deltas = (
            await session.execute(
                select(TripDeltaModel).where(
                    TripDeltaModel.trip_id == trip_id,
                    TripDeltaModel.base_version == 1,
                )
            )
        ).scalars().all()
        assert len(deltas) == 1
        assert deltas[0].proposal_json is not None

        exec_final = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
        ).scalar_one()
        assert exec_final.status == "SUCCEEDED"
        assert exec_final.lease_token is None
        assert exec_final.lease_expiry is None
        assert exec_final.checkpoint_id is not None

        checkpoint_tuple = await checkpointer2.aget_tuple(
            {"configurable": {"thread_id": str(execution_id)}}
        )
        assert checkpoint_tuple is not None
        assert checkpoint_tuple.checkpoint["id"] == exec_final.checkpoint_id

        published_stmt = select(JobEventModel).where(
            JobEventModel.execution_id == execution_id,
            JobEventModel.event_type == "PROPOSAL_PUBLISHED",
        )
        published_events = (await session.execute(published_stmt)).scalars().all()
        assert len(published_events) == 1

        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "EXECUTION_SUCCEEDED",
        )
        outbox_event = (await session.execute(outbox_stmt)).scalar_one_or_none()
        assert outbox_event is not None


@pytest.mark.asyncio
async def test_trip_planner_proposal_missing_leases_on_postgres(
    session_factory: Any,
) -> None:
    """Verifies that missing runner lease token or expiry fails closed in production without writing trip or delta."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    runner_lease = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.commit()

    sample_state = _build_test_initial_state(owner_id, trip_id, execution_id)
    sample_state["proposal"] = {"schedule": {"stops": ["test"]}, "version": 1}

    # Case 1: missing runner lease in configurable and state
    res1 = await publish_proposal_node(
        cast(TripPlanningState, sample_state),
        config={"configurable": {"session_factory": session_factory}},
    )
    assert res1["status"] == "FAILED"
    assert res1["current_step"] == "proposal_publication_lost_lease"

    # Case 2: runner lease in config, but execution has lease_token NULL in database
    res2 = await publish_proposal_node(
        cast(TripPlanningState, sample_state),
        config={
            "configurable": {
                "session_factory": session_factory,
                "runner_lease": runner_lease,
            }
        },
    )
    assert res2["status"] == "FAILED"
    assert res2["current_step"] == "proposal_publication_lost_lease"

    # Case 3: execution has matching lease_token but lease_expiry is NULL in database
    async with session_factory() as session:
        await session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(
                status="RUNNING",
                lease_token=runner_lease,
                lease_expiry=None,
            )
        )
        await session.commit()

    res3 = await publish_proposal_node(
        cast(TripPlanningState, sample_state),
        config={
            "configurable": {
                "session_factory": session_factory,
                "runner_lease": runner_lease,
            }
        },
    )
    assert res3["status"] == "FAILED"
    assert res3["current_step"] == "proposal_publication_lost_lease"

    # Trip and deltas in PostgreSQL must remain untouched
    async with session_factory() as session:
        trip_after = (
            await session.execute(select(TripModel).where(TripModel.id == trip_id))
        ).scalar_one()
        assert trip_after.state == "draft"
        assert trip_after.current_route_id is None
        assert trip_after.itinerary_json is None

        delta_count = (
            await session.execute(
                select(sa.func.count(TripDeltaModel.id)).where(TripDeltaModel.trip_id == trip_id)
            )
        ).scalar()
        assert delta_count == 0


@pytest.mark.asyncio
async def test_trip_planner_proposal_lock_wait_lease_expiry_on_postgres(
    session_factory: Any,
) -> None:
    """Verifies that runner lease expiring before or during lock acquisition fails closed."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    runner_lease = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        # Seed execution with lease that expired 1 second ago
        await session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(
                status="RUNNING",
                lease_token=runner_lease,
                lease_expiry=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.commit()

    sample_state = _build_test_initial_state(owner_id, trip_id, execution_id)
    sample_state["proposal"] = {"schedule": {"stops": ["test"]}, "version": 1}

    res = await publish_proposal_node(
        cast(TripPlanningState, sample_state),
        config={
            "configurable": {
                "session_factory": session_factory,
                "runner_lease": runner_lease,
            }
        },
    )
    assert res["status"] == "FAILED"
    assert res["current_step"] == "proposal_publication_lost_lease"
    assert any("expired" in err.lower() for err in res.get("errors", []))

    async with session_factory() as session:
        trip_after = (
            await session.execute(select(TripModel).where(TripModel.id == trip_id))
        ).scalar_one()
        assert trip_after.state == "draft"
        assert trip_after.current_route_id is None
        assert trip_after.itinerary_json is None

        delta_count = (
            await session.execute(
                select(sa.func.count(TripDeltaModel.id)).where(TripDeltaModel.trip_id == trip_id)
            )
        ).scalar()
        assert delta_count == 0


@pytest.mark.asyncio
async def test_trip_planner_budget_cross_execution_and_unrelated_place_fees_on_postgres(
    session_factory: Any,
) -> None:
    """Verifies that cross-execution fee claims and unrelated-place fee claims are rejected, remaining UNKNOWN."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    foreign_execution_id = uuid.uuid4()
    place_digha_id = uuid.uuid4()
    place_unrelated_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        _seed_canonical_place(session, place_digha_id, "Digha Beach", 21.6266, 87.5074)
        _seed_canonical_place(session, place_unrelated_id, "Unrelated Museum", 21.6300, 87.5100)

        # Seed foreign execution for foreign_execution_id to satisfy foreign key
        foreign_owner_id = uuid.uuid4()
        foreign_execution = ExecutionModel(
            id=foreign_execution_id,
            owner_id=foreign_owner_id,
            trip_id=trip_id,
            workflow="TRAVEL",
            status="RUNNING",
            context_version=1,
            deadline_at=now + timedelta(hours=2),
            created_at=now,
            updated_at=now,
        )
        session.add(foreign_execution)
        await session.flush()

        # 1. Foreign execution claim: valid fee claim but belongs to foreign_execution_id
        obs_foreign = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=foreign_execution_id,
            source_url="https://tourism.wb.gov.in/foreign-fee",
            canonical_url="https://tourism.wb.gov.in/foreign-fee",
            publisher="tourism.wb.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=1),
            excerpt_text="Foreign entrance fee 100 INR",
            excerpt_locator="p",
            extraction_method="SCRAPLING_HTTP",
            created_at=now,
        )
        session.add(obs_foreign)
        await session.flush()

        claim_foreign = ClaimModel(
            id=uuid.uuid4(),
            execution_id=foreign_execution_id,
            subject_id="Digha Beach",
            place_id=place_digha_id,
            category="FEES",
            typed_value={"price": 100.0, "amount": 100.0, "currency": "INR"},
            verification="VERIFIED",
            availability="AVAILABLE",
            freshness="FRESH",
            policy_version="1.0",
        )
        session.add(claim_foreign)
        await session.flush()

        link_foreign = ClaimEvidenceModel(
            claim_id=claim_foreign.id,
            observation_id=obs_foreign.id,
            support_type="SUPPORTS",
        )
        session.add(link_foreign)

        # 2. Unrelated place claim: belongs to current execution, but linked to place_unrelated_id
        obs_unrelated = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=execution_id,
            source_url="https://tourism.wb.gov.in/unrelated-fee",
            canonical_url="https://tourism.wb.gov.in/unrelated-fee",
            publisher="tourism.wb.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=1),
            excerpt_text="Unrelated place fee 200 INR",
            excerpt_locator="p",
            extraction_method="SCRAPLING_HTTP",
            created_at=now,
        )
        session.add(obs_unrelated)
        await session.flush()

        claim_unrelated = ClaimModel(
            id=uuid.uuid4(),
            execution_id=execution_id,
            subject_id="Unrelated Museum",
            place_id=place_unrelated_id,
            category="FEES",
            typed_value={"price": 200.0, "amount": 200.0, "currency": "INR"},
            verification="VERIFIED",
            availability="AVAILABLE",
            freshness="FRESH",
            policy_version="1.0",
        )
        session.add(claim_unrelated)
        await session.flush()

        link_unrelated = ClaimEvidenceModel(
            claim_id=claim_unrelated.id,
            observation_id=obs_unrelated.id,
            support_type="SUPPORTS",
        )
        session.add(link_unrelated)
        await session.commit()

    sample_state = _build_test_initial_state(owner_id, trip_id, execution_id)
    sample_state["validated_candidates"] = [
        {
            "place_id": str(place_digha_id),
            "primary_name": "Digha Beach",
            "category": "attraction",
            "is_candidate": False,
        }
    ]
    sample_state["optimized_schedule"] = {
        "stops": [
            {
                "stop_id": "stop_1",
                "name": "Digha Beach",
                "place_id": str(place_digha_id),
                "stop_type": "DESTINATION",
            }
        ],
        "legs": [],
    }

    budget_res = await compute_budget_node(
        cast(TripPlanningState, sample_state),
        config={"configurable": {"session_factory": session_factory}},
    )
    activity_lines = [
        line
        for line in budget_res["budget_lines"]
        if line["category"] == BudgetCategory.ACTIVITIES.value
    ]
    assert len(activity_lines) == 1
    fee_line = activity_lines[0]
    assert fee_line["amount"] is None
    assert "Entrance fee unverified" in fee_line["unknown_reason"]


@pytest.mark.asyncio
async def test_trip_planner_budget_unsupported_or_stale_evidence_fees_on_postgres(
    session_factory: Any,
) -> None:
    """Verifies that refuted evidence, stale observations (> 90 days), and candidate estimates without claim are UNKNOWN."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    place_digha_id = uuid.uuid4()
    place_park_id = uuid.uuid4()
    place_fort_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        _seed_canonical_place(session, place_digha_id, "Digha Beach", 21.6266, 87.5074)
        _seed_canonical_place(session, place_park_id, "Nature Park", 21.6300, 87.5100)
        _seed_canonical_place(session, place_fort_id, "Historic Fort", 21.6350, 87.5150)
        await session.flush()

        # 1. Refuted evidence: ClaimEvidenceModel has support_type == "REFUTES"
        obs_refuted = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=execution_id,
            source_url="https://tourism.wb.gov.in/refuted-fee",
            canonical_url="https://tourism.wb.gov.in/refuted-fee",
            publisher="tourism.wb.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=1),
            excerpt_text="Park fee refuted",
            excerpt_locator="p",
            extraction_method="SCRAPLING_HTTP",
            created_at=now,
        )
        session.add(obs_refuted)
        await session.flush()

        claim_refuted = ClaimModel(
            id=uuid.uuid4(),
            execution_id=execution_id,
            subject_id="Nature Park",
            place_id=place_park_id,
            category="FEES",
            typed_value={"price": 30.0, "amount": 30.0, "currency": "INR"},
            verification="VERIFIED",
            availability="AVAILABLE",
            freshness="FRESH",
            policy_version="1.0",
        )
        session.add(claim_refuted)
        await session.flush()

        link_refuted = ClaimEvidenceModel(
            claim_id=claim_refuted.id,
            observation_id=obs_refuted.id,
            support_type="REFUTES",
        )
        session.add(link_refuted)

        # 2. Stale evidence: observation retrieved_at is 100 days ago (> 90 days limit)
        obs_stale = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=execution_id,
            source_url="https://tourism.wb.gov.in/stale-fee",
            canonical_url="https://tourism.wb.gov.in/stale-fee",
            publisher="tourism.wb.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now - timedelta(days=100),
            published_at=now - timedelta(days=120),
            excerpt_text="Historic Fort fee 50 INR",
            excerpt_locator="p",
            extraction_method="SCRAPLING_HTTP",
            created_at=now - timedelta(days=100),
        )
        session.add(obs_stale)
        await session.flush()

        claim_stale = ClaimModel(
            id=uuid.uuid4(),
            execution_id=execution_id,
            subject_id="Historic Fort",
            place_id=place_fort_id,
            category="FEES",
            typed_value={"price": 50.0, "amount": 50.0, "currency": "INR"},
            verification="VERIFIED",
            availability="AVAILABLE",
            freshness="STALE",
            policy_version="1.0",
        )
        session.add(claim_stale)
        await session.flush()

        link_stale = ClaimEvidenceModel(
            claim_id=claim_stale.id,
            observation_id=obs_stale.id,
            support_type="SUPPORTS",
        )
        session.add(link_stale)
        await session.commit()

    sample_state = _build_test_initial_state(owner_id, trip_id, execution_id)
    sample_state["validated_candidates"] = [
        {
            "place_id": str(place_digha_id),
            "primary_name": "Digha Beach",
            "category": "attraction",
            "price_estimate_inr": 60.0,
            "verified_price_inr": 60.0,
            "is_candidate": False,
        },
        {
            "place_id": str(place_park_id),
            "primary_name": "Nature Park",
            "category": "attraction",
            "price_estimate_inr": 30.0,
            "is_candidate": False,
        },
        {
            "place_id": str(place_fort_id),
            "primary_name": "Historic Fort",
            "category": "attraction",
            "price_estimate_inr": 50.0,
            "is_candidate": False,
        },
    ]
    sample_state["optimized_schedule"] = {
        "stops": [
            {
                "stop_id": "stop_1",
                "name": "Digha Beach",
                "place_id": str(place_digha_id),
                "stop_type": "INTERMEDIATE",
            },
            {
                "stop_id": "stop_2",
                "name": "Nature Park",
                "place_id": str(place_park_id),
                "stop_type": "INTERMEDIATE",
            },
            {
                "stop_id": "stop_3",
                "name": "Historic Fort",
                "place_id": str(place_fort_id),
                "stop_type": "DESTINATION",
            },
        ],
        "legs": [],
    }

    budget_res = await compute_budget_node(
        cast(TripPlanningState, sample_state),
        config={"configurable": {"session_factory": session_factory}},
    )
    activity_lines = [
        line
        for line in budget_res["budget_lines"]
        if line["category"] == BudgetCategory.ACTIVITIES.value
    ]
    assert len(activity_lines) == 3

    digha_line = [item for item in activity_lines if item["basis"] == "entry_fee_Digha Beach"][0]
    assert digha_line["amount"] is None
    assert "Entrance fee unverified" in digha_line["unknown_reason"]

    park_line = [item for item in activity_lines if item["basis"] == "entry_fee_Nature Park"][0]
    assert park_line["amount"] is None
    assert "Entrance fee unverified" in park_line["unknown_reason"]

    fort_line = [item for item in activity_lines if item["basis"] == "entry_fee_Historic Fort"][0]
    assert fort_line["amount"] is None
    assert "Entrance fee unverified" in fort_line["unknown_reason"]


@pytest.mark.asyncio
async def test_trip_planner_budget_ac7_verified_versus_unverified_fees_on_postgres(
    session_factory: Any,
) -> None:
    """Verifies Finding 5: only evidence-supported verified prices are QUOTED; unverified fees are explicit UNKNOWN entries."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    place_digha_id = uuid.uuid4()
    place_aquarium_id = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        _seed_canonical_place(session, place_digha_id, "Digha Beach", 21.6266, 87.5074)
        _seed_canonical_place(session, place_aquarium_id, "Marine Aquarium", 21.6300, 87.5100)
        await session.flush()

        # Seed verified observation and claim for Digha Beach entrance fee (50 INR)
        now = datetime.now(UTC)
        obs_fee = SourceObservationModel(
            id=uuid.uuid4(),
            execution_id=execution_id,
            source_url="https://tourism.wb.gov.in/digha-beach-entry",
            canonical_url="https://tourism.wb.gov.in/digha-beach-entry",
            publisher="tourism.wb.gov.in",
            content_hash=f"hash_{uuid.uuid4().hex}",
            retrieved_at=now,
            published_at=now - timedelta(days=1),
            excerpt_text="Entrance fee to Digha Beach attraction is 50 INR per adult.",
            excerpt_locator="article p",
            extraction_method="SCRAPLING_HTTP",
            created_at=now,
        )
        session.add(obs_fee)
        await session.flush()

        claim_fee = ClaimModel(
            id=uuid.uuid4(),
            execution_id=execution_id,
            subject_id="Digha Beach",
            place_id=place_digha_id,
            category="FEES",
            typed_value={"price": 50.0, "amount": 50.0, "currency": "INR"},
            verification="VERIFIED",
            availability="AVAILABLE",
            freshness="FRESH",
            policy_version="1.0",
        )
        session.add(claim_fee)
        await session.flush()

        claim_ev = ClaimEvidenceModel(
            claim_id=claim_fee.id,
            observation_id=obs_fee.id,
            support_type="SUPPORTS",
        )
        session.add(claim_ev)
        await session.commit()

    sample_state = _build_test_initial_state(owner_id, trip_id, execution_id)
    sample_state["validated_candidates"] = [
        {
            "place_id": str(place_digha_id),
            "primary_name": "Digha Beach",
            "category": "attraction",
            "price_estimate_inr": 50.0,
            "is_candidate": False,
        },
        {
            "place_id": str(place_aquarium_id),
            "primary_name": "Marine Aquarium",
            "category": "museum",
            "price_estimate_inr": 80.0,  # Unverified proposal estimate!
            "is_candidate": False,
            "provenance": {"source": "osm:node/12345"},
        },
    ]
    sample_state["optimized_schedule"] = {
        "stops": [
            {
                "stop_id": "stop_1",
                "name": "Marine Aquarium",
                "place_id": str(place_aquarium_id),
                "stop_type": "INTERMEDIATE",
            },
            {
                "stop_id": "stop_2",
                "name": "Digha Beach",
                "place_id": str(place_digha_id),
                "stop_type": "DESTINATION",
            },
        ],
        "legs": [],
    }

    budget_res = await compute_budget_node(
        cast(TripPlanningState, sample_state),
        config={"configurable": {"session_factory": session_factory}},
    )
    activity_lines = [
        line
        for line in budget_res["budget_lines"]
        if line["category"] == BudgetCategory.ACTIVITIES.value
    ]
    assert len(activity_lines) == 2

    # Verified entrance fee backed by ClaimEvidenceModel is QUOTED
    quoted_line = [item for item in activity_lines if item["basis"] == "entry_fee_Digha Beach"][0]
    assert quoted_line["amount"] == "50.0"
    assert quoted_line["estimated"] is False
    assert f"obs:{obs_fee.id}" in quoted_line["source_ref"]

    # Unverified museum entrance fee is explicit UNKNOWN (amount None), never 80.0
    unknown_line = [item for item in activity_lines if item["basis"] == "entry_fee_Marine Aquarium"][0]
    assert unknown_line["amount"] is None
    assert "Entrance fee unverified" in unknown_line["unknown_reason"]
    assert "osm:node/12345" in unknown_line["source_ref"]


@pytest.mark.asyncio
async def test_trip_planner_proposal_stale_runner_after_deadline_on_postgres(
    session_factory: Any,
) -> None:
    """Verifies Finding 2: stale runner cannot cancel or alter execution even after execution deadline."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    stale_runner_lease = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        await session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(
                status="RUNNING",
                deadline_at=now - timedelta(minutes=5),
                lease_token=stale_runner_lease,
                lease_expiry=now - timedelta(seconds=10),
            )
        )
        await session.commit()

    sample_state = _build_test_initial_state(owner_id, trip_id, execution_id)
    sample_state["proposal"] = {"schedule": {"stops": ["test"]}, "version": 1}

    res = await publish_proposal_node(
        cast(TripPlanningState, sample_state),
        config={
            "configurable": {
                "session_factory": session_factory,
                "runner_lease": stale_runner_lease,
            }
        },
    )
    assert res["status"] == "FAILED"
    assert res["current_step"] == "proposal_publication_lost_lease"

    async with session_factory() as session:
        exec_after = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
        ).scalar_one()
        # Stale runner must NOT have mutated execution status to CANCELLED
        assert exec_after.status == "RUNNING"


@pytest.mark.asyncio
async def test_trip_planner_budget_production_rejects_raw_claims_with_fabricated_evidence_on_postgres(
    session_factory: Any,
) -> None:
    """Verifies Finding 1: production fee calculation rejects raw task output claims with fabricated evidence on PostgreSQL."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    place_digha_id = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        _seed_canonical_place(session, place_digha_id, "Digha Beach", 21.6266, 87.5074)
        await session.commit()

    sample_state = _build_test_initial_state(owner_id, trip_id, execution_id)
    sample_state["_offline_fixture_mode"] = False
    sample_state["task_outputs"] = {
        "claims": [
            {
                "id": str(uuid.uuid4()),
                "execution_id": str(execution_id),
                "place_id": str(place_digha_id),
                "subject_id": "Digha Beach",
                "category": "FEES",
                "verification": "VERIFIED",
                "availability": "AVAILABLE",
                "freshness": "FRESH",
                "typed_value": {"price": 250.0, "amount": 250.0, "currency": "INR"},
                "evidence_refs": ["fabricated_evidence_12345"],
                "support_type": "SUPPORTS",
            }
        ]
    }
    sample_state["validated_candidates"] = [
        {
            "place_id": str(place_digha_id),
            "primary_name": "Digha Beach",
            "category": "attraction",
            "price_estimate_inr": 250.0,
            "verified_price_inr": 250.0,
            "is_candidate": False,
        }
    ]
    sample_state["optimized_schedule"] = {
        "stops": [
            {
                "stop_id": "stop_1",
                "name": "Digha Beach",
                "place_id": str(place_digha_id),
                "stop_type": "DESTINATION",
            }
        ],
        "legs": [],
    }

    budget_res = await compute_budget_node(
        cast(TripPlanningState, sample_state),
        config={"configurable": {"session_factory": session_factory}},
    )
    activity_lines = [
        line
        for line in budget_res["budget_lines"]
        if line["category"] == BudgetCategory.ACTIVITIES.value
    ]
    assert len(activity_lines) == 1
    digha_line = activity_lines[0]
    assert digha_line["amount"] is None
    assert "Entrance fee unverified" in digha_line["unknown_reason"]
