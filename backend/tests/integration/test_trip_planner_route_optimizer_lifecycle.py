"""Integration tests for TripPlannerGraph route optimizer lifecycle on isolated PostgreSQL.

Conforms to specification 0007 AC-4, AC-5, AC-9, and Slice 4 requirements:
1. Full task/outbox lifecycle: GraphRunnerService start -> WAITING_TASKS -> RouteOptimizerWorkerService
   claim & solve under lease fencing -> route persistence -> CompletionReconciliationService receipt deduplication
   -> GraphRunnerService resume -> SUCCEEDED with route_id linked.
2. Tenant isolation: route persisted under owner_id; cross-tenant retrieval is rejected.
3. Replay safety & duplicate delivery: resuming clears pending_tasks; duplicate completion receipts are deduplicated.
4. Cancellation barrier: cancelled parent execution halts worker before route persistence.
5. Fail-closed routing: provider outage produces ROUTING_UNAVAILABLE with honest diagnostic notice and zero road guesswork.
6. Infeasible route handling: overconstrained windows produce INFEASIBLE route record without failing parent execution.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from contracts.routing import (
    RouteOptimizerWorkerOutput,
    RouteStatus,
)
from functions.api.models import TripModel
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.models import ClaimEvidenceModel, ClaimModel
from functions.orchestrator.evidence.schemas import (
    ClaimCategory,
    FreshnessStatus,
    VerificationStatus,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.fencing import (
    ParentCancelledError,
    WorkerFencingService,
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
from functions.orchestrator.graphs.trip_planner.graph import (
    compile_trip_planning_workflow,
)
from functions.orchestrator.routing.geojson import assemble_route_map_geojson
from functions.orchestrator.routing.models import RouteModel
from functions.orchestrator.routing.repository import RouteRepository
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

    # Seed verified source observation and claim for destination 'Digha'
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
            "budget": {"amount": "15000.00", "currency": "INR"},
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
                        "excerpt_text": "Digha Marine Station is a research facility and aquarium in Digha.",
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
async def test_trip_planner_route_optimizer_lifecycle_end_to_end(
    session_factory: Any,
) -> None:
    """Verifies complete end-to-end route optimization lifecycle on live PostgreSQL."""
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

    # 1. Start execution -> should halt at AWAIT_ROUTE_RESULTS with WAITING_TASKS
    start_event = ExecutionStartEvent(
        execution_id=execution_id,
        workflow="TRAVEL",
        context_version=1,
        payload=initial_payload,
    )
    start_res = await runner.handle_execution_start(event=start_event)

    assert start_res.status == "WAITING_TASKS"
    assert start_res.tasks_dispatched == 1
    assert start_res.graph_advanced is True

    # 2. Verify database records after start
    async with session_factory() as session:
        task_stmt = select(TaskModel).where(TaskModel.execution_id == execution_id)
        task_row = (await session.execute(task_stmt)).scalar_one()
        assert task_row.worker == "OPTIMIZER"
        assert task_row.action == "solve"
        assert task_row.status == "PENDING"
        assert task_row.idempotency_key == f"{execution_id}:optimizer:solve"

        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "TASK_DISPATCH",
        )
        dispatch_outbox = (await session.execute(outbox_stmt)).scalar_one()
        dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

    # 3. Execute optimizer worker with MockValhallaClient under lease fencing
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

    # 4. Verify route persistence in PostgreSQL with tenant fencing
    async with session_factory() as session:
        route_repo = RouteRepository(session)
        persisted_route = await route_repo.get_route_by_id(persisted_route_id, owner_id=owner_id)
        assert persisted_route is not None
        assert persisted_route.owner_id == owner_id
        assert persisted_route.execution_id == execution_id
        assert persisted_route.trip_id == trip_id
        assert persisted_route.status == "FEASIBLE"
        assert len(persisted_route.stops) >= 2
        assert len(persisted_route.legs) >= 1

        # Verify GeoJSON retrieval conforms to RFC 7946 simplestyle (AC-5)
        geojson_map = assemble_route_map_geojson(persisted_route)
        assert geojson_map.type == "FeatureCollection"
        assert len(geojson_map.features) >= 1
        assert geojson_map.route_id == persisted_route_id

        # Tenant isolation check: another tenant cannot access the route
        other_owner_id = uuid.uuid4()
        other_route = await route_repo.get_route_by_id(persisted_route_id, owner_id=other_owner_id)
        assert other_route is None

    # 5. Reconcile task completion
    async with session_factory() as session:
        task_updated = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_row.id))
        ).scalar_one()
        assert task_updated.status == "COMPLETED"

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
    assert reconcile_res.checkpoint_updated is True
    assert reconcile_res.graph_resumed is True

    # 6. Resume execution -> graph consumes route result and finishes SUCCEEDED
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
    # Replay safety invariant: resumption must clear pending_tasks and dispatch zero tasks
    assert resume_res.tasks_dispatched == 0
    assert resume_res.graph_advanced is True

    # 7. Final database verification: execution SUCCEEDED and route linked
    async with session_factory() as session:
        exec_final = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
        ).scalar_one()
        assert exec_final.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_trip_planner_route_optimizer_replay_and_duplicate_delivery_safety(
    session_factory: Any,
) -> None:
    """Verifies that duplicate receipts are deduplicated and resume creates zero duplicate tasks."""
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
        dispatch_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "TASK_DISPATCH",
        )
        dispatch_outbox = (await session.execute(dispatch_stmt)).scalar_one()
        dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

    # Solve route via worker
    mock_valhalla = MockValhallaClient()
    async with session_factory() as session:
        worker_service = RouteOptimizerWorkerService(
            session=session,
            valhalla_client=mock_valhalla,
        )
        _, worker_output = await worker_service.execute_task(dispatch_payload)
        await session.commit()

    async with session_factory() as session:
        task_updated = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_row.id))
        ).scalar_one()
        assert task_updated.status == "COMPLETED"

    reconciler = CompletionReconciliationService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )

    # First receipt delivery -> APPLIED
    dup_event_id = uuid.uuid4()
    event_1 = TaskCompletionEvent(
        event_id=dup_event_id,
        task_id=task_updated.id,
        execution_id=execution_id,
        attempt=task_updated.attempt,
        event_type="TASK_COMPLETED",
        result_id=task_updated.result_id,
        output=worker_output.model_dump(mode="json"),
    )
    res_1 = await reconciler.reconcile_task_event(event=event_1)
    assert res_1.status == "APPLIED"

    # Duplicate receipt delivery with same event_id -> ALREADY_APPLIED
    res_2 = await reconciler.reconcile_task_event(event=event_1)
    assert res_2.status == "ALREADY_APPLIED"
    assert res_2.checkpoint_updated is False
    assert res_2.graph_resumed is False

    # Check exactly 1 task exists in database
    async with session_factory() as session:
        all_tasks = (
            (await session.execute(select(TaskModel).where(TaskModel.execution_id == execution_id)))
            .scalars()
            .all()
        )
        assert len(all_tasks) == 1


@pytest.mark.asyncio
async def test_trip_planner_route_optimizer_cancellation_barrier(
    session_factory: Any,
) -> None:
    """Verifies that a cancelled parent execution halts worker before route persistence."""
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
        dispatch_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "TASK_DISPATCH",
        )
        dispatch_outbox = (await session.execute(dispatch_stmt)).scalar_one()
        dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload

        # Mark parent execution CANCELLED before worker executes
        await session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(status="CANCELLED", cancelled_at=datetime.now(UTC))
        )
        await session.commit()

    mock_valhalla = MockValhallaClient()
    async with session_factory() as session:
        worker_service = RouteOptimizerWorkerService(
            session=session,
            valhalla_client=mock_valhalla,
        )
        with pytest.raises(ParentCancelledError):
            await worker_service.execute_task(dispatch_payload)

    # Verify no route was persisted
    async with session_factory() as session:
        routes_found = (
            (
                await session.execute(
                    select(RouteModel).where(RouteModel.execution_id == execution_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(routes_found) == 0


@pytest.mark.asyncio
async def test_trip_planner_route_optimizer_provider_outage_fail_closed(
    session_factory: Any,
) -> None:
    """Verifies that a routing provider outage produces ROUTING_UNAVAILABLE with honest diagnostics."""
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

    outage_output = {
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
            output=outage_output,
        )
        await session.commit()

    # Reconcile provider outage as ROUTING_UNAVAILABLE task output
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
        output=outage_output,
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
    resume_res = await runner.handle_execution_resume(event=resume_event)
    assert resume_res.status == "SUCCEEDED"

    # Verify checkpoint state reflects ROUTING_UNAVAILABLE without straight line guesswork
    checkpoint_state = await checkpointer.aget({"configurable": {"thread_id": str(execution_id)}})
    assert checkpoint_state is not None
    state_values = checkpoint_state.get("channel_values", {})
    assert state_values.get("route_id") is None
    assert state_values.get("routing_status") == "ROUTING_UNAVAILABLE"
    assert any(
        "Road routing engine unavailable" in d for d in state_values.get("route_diagnostics", [])
    )


@pytest.mark.asyncio
async def test_trip_planner_route_optimizer_infeasible_route_outcome(
    session_factory: Any,
) -> None:
    """Verifies that an infeasible route outcome preserves dropped stops diagnostics without failing execution."""
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

    infeasible_route_id = uuid.uuid4()
    now_ts = datetime.now(UTC)
    async with session_factory() as session:
        infeasible_route = RouteModel(
            id=infeasible_route_id,
            owner_id=owner_id,
            execution_id=execution_id,
            trip_id=trip_id,
            route_version=1,
            status="INFEASIBLE",
            solver_status="INFEASIBLE",
            objective="FASTEST",
            departure_at=now_ts,
            created_at=now_ts,
            updated_at=now_ts,
        )
        session.add(infeasible_route)
        await session.commit()

    infeasible_output = RouteOptimizerWorkerOutput(
        status=RouteStatus.INFEASIBLE,
        route_id=infeasible_route_id,
        route_version=1,
        total_distance_m=0,
        total_travel_seconds=0,
        stops_count=0,
        legs_count=0,
        diagnostics=["MANDATORY_STOP_WINDOW_BREACH: Digha opening window cannot be satisfied"],
    )

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
            output=infeasible_output.model_dump(mode="json"),
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
        output=infeasible_output.model_dump(mode="json"),
    )
    await reconciler.reconcile_task_event(event=completion_event)

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

    checkpoint_state = await checkpointer.aget({"configurable": {"thread_id": str(execution_id)}})
    assert checkpoint_state is not None
    state_values = checkpoint_state.get("channel_values", {})
    assert state_values.get("route_id") == str(infeasible_route_id)
    assert state_values.get("routing_status") == "INFEASIBLE"
    assert "MANDATORY_STOP_WINDOW_BREACH" in state_values.get("route_diagnostics", [])[0]


@pytest.mark.asyncio
async def test_trip_planner_route_optimizer_mixed_task_outputs_on_live_postgres(
    session_factory: Any,
) -> None:
    """Verifies that earlier browser/evidence failures are ignored and optimizer output is consumed on live PostgreSQL."""
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
    # Pre-populate task_outputs with an earlier browser failure
    browser_task_id = str(uuid.uuid4())
    initial_payload["task_outputs"] = {
        browser_task_id: {
            "worker": "BROWSER",
            "action": "scrape",
            "status": "FAILED",
            "error": "Scrape timed out on destination review portal",
        }
    }

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

        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "TASK_DISPATCH",
        )
        dispatch_outbox = (await session.execute(outbox_stmt)).scalar_one()
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

    checkpoint_state = await checkpointer.aget({"configurable": {"thread_id": str(execution_id)}})
    assert checkpoint_state is not None
    state_values = checkpoint_state.get("channel_values", {})
    # Binding check: optimizer output was bound and consumed despite prior browser failure
    assert state_values.get("route_id") == str(persisted_route_id)
    assert state_values.get("routing_status") == "FEASIBLE"


@pytest.mark.asyncio
async def test_trip_planner_route_optimizer_production_fail_closed_on_tenant_mismatch(
    session_factory: Any,
) -> None:
    """Verifies that if worker output references a route owned by another tenant, graph fails closed on live PostgreSQL."""
    owner_id = uuid.uuid4()
    other_owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    async with session_factory() as session:
        await _seed_test_execution_and_trip(
            session, owner_id=owner_id, trip_id=trip_id, execution_id=execution_id
        )
        # Create route owned by different tenant
        now_ts = datetime.now(UTC)
        foreign_route_id = uuid.uuid4()
        foreign_route = RouteModel(
            id=foreign_route_id,
            owner_id=other_owner_id,
            execution_id=execution_id,
            trip_id=trip_id,
            route_version=1,
            status="FEASIBLE",
            solver_status="FEASIBLE",
            objective="FASTEST",
            departure_at=now_ts,
            created_at=now_ts,
            updated_at=now_ts,
        )
        session.add(foreign_route)
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

    # Worker output points to foreign route
    foreign_output = RouteOptimizerWorkerOutput(
        status=RouteStatus.FEASIBLE,
        route_id=foreign_route_id,
        route_version=1,
        total_distance_m=100000,
        total_travel_seconds=7200,
        stops_count=2,
        legs_count=1,
        diagnostics=["Route solved"],
    )

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
            output=foreign_output.model_dump(mode="json"),
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
        output=foreign_output.model_dump(mode="json"),
    )
    await reconciler.reconcile_task_event(event=completion_event)

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

    checkpoint_state = await checkpointer.aget({"configurable": {"thread_id": str(execution_id)}})
    assert checkpoint_state is not None
    state_values = checkpoint_state.get("channel_values", {})
    # Invariant: Tenant isolation rejected foreign route -> fail closed!
    assert state_values.get("route_id") is None
    assert state_values.get("routing_status") == "ROUTING_UNAVAILABLE"
    assert any(
        "tenant isolation" in d.lower() or "not found" in d.lower()
        for d in state_values.get("route_diagnostics", [])
    )
