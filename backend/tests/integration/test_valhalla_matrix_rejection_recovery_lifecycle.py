"""Integration test for Valhalla matrix distance rejection recovery conforming to Spec 0007.

Verifies:
1. Exact HTTP 400/154 response injected through the actual Valhalla client at the HTTP boundary.
2. Route optimizer consumer claims task, catches classified rejection, and durably completes
   under lease with degraded ROUTING_UNAVAILABLE output.
3. No stranded claim (lease released, task COMPLETED).
4. No phantom route (routes table has zero records for execution/trip).
5. CompletionReconciliationService reconciles task completion and emits EXECUTION_RESUME.
6. Checkpoint resume advances LangGraph through weather, budget, synthesis, and publication.
7. Partial itinerary published in trips table and retrievable via GET /api/v1/trips/{trip_id}/itinerary.
8. API response asserts ROUTING_UNAVAILABLE, route_id=None, map_geojson=None, preserved places/provenance.
9. Redelivery of optimizer dispatch message is idempotent and safely discarded without duplicate publication.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config.db import get_session
from functions.api.app import app
from functions.api.models import TripDeltaModel, TripModel
from functions.api.repository import SqlAlchemyUnitOfWork
from functions.api.routes.trips import get_uow
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.evidence.models import ClaimEvidenceModel, ClaimModel
from functions.orchestrator.evidence.schemas import (
    ClaimCategory,
    FreshnessStatus,
    VerificationStatus,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.models import (
    ExecutionModel,
    OutboxEventModel,
    TaskModel,
    WorkerResultModel,
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
from functions.orchestrator.routing.models import RouteModel
from functions.route_optimizer.consumer import process_record
from functions.route_optimizer.valhalla import ValhallaClient
from middleware.auth import AuthenticatedUser, get_current_user


async def _seed_test_execution_and_trip(
    session: AsyncSession,
    owner_id: uuid.UUID,
    trip_id: uuid.UUID,
    execution_id: uuid.UUID,
) -> None:
    now = datetime.now(UTC)
    trip = TripModel(
        id=trip_id,
        owner_id=owner_id,
        current_version=1,
        state="planning",
        created_at=now,
        updated_at=now,
    )
    session.add(trip)
    await session.flush()

    execution = ExecutionModel(
        id=execution_id,
        owner_id=owner_id,
        trip_id=trip_id,
        workflow="TRAVEL",
        status="CREATED",
        context_version=1,
        deadline_at=now + timedelta(hours=2),
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    await session.flush()

    # Seed verified destination observation and claim for Mysuru
    obs_mysuru = SourceObservationModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        source_url="https://karnatakatourism.org/mysuru",
        canonical_url="https://karnatakatourism.org/mysuru",
        publisher="karnatakatourism.org",
        content_hash=f"hash_{uuid.uuid4().hex}",
        retrieved_at=now,
        published_at=now - timedelta(days=5),
        excerpt_text="Mysuru is a heritage destination in Karnataka known for palaces.",
        excerpt_locator="article p",
        extraction_method="SCRAPLING_HTTP",
        artifact_ref={
            "status": "SUCCESS",
            "gps": {"lat": 12.2958, "lng": 76.6394},
        },
        created_at=now,
    )
    session.add(obs_mysuru)
    await session.flush()

    claim_mysuru = ClaimModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        subject_id="Mysuru",
        category=ClaimCategory.ATTRACTION.value,
        typed_value={"verified_name": "Mysuru"},
        verification=VerificationStatus.VERIFIED.value,
        availability="AVAILABLE",
        freshness=FreshnessStatus.FRESH.value,
        valid_from=now - timedelta(days=30),
        valid_until=now + timedelta(days=60),
        created_at=now,
        updated_at=now,
    )
    session.add(claim_mysuru)
    await session.flush()

    link_mysuru = ClaimEvidenceModel(
        claim_id=claim_mysuru.id,
        observation_id=obs_mysuru.id,
        support_type="SUPPORTS",
        locator="article p",
        created_at=now,
    )
    session.add(link_mysuru)
    await session.flush()

    # Seed verified destination observation and claim for Madikeri
    obs_madikeri = SourceObservationModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        source_url="https://karnatakatourism.org/madikeri",
        canonical_url="https://karnatakatourism.org/madikeri",
        publisher="karnatakatourism.org",
        content_hash=f"hash_{uuid.uuid4().hex}",
        retrieved_at=now,
        published_at=now - timedelta(days=5),
        excerpt_text="Madikeri is a scenic hill town in Kodagu with viewpoints and coffee estates.",
        excerpt_locator="article p",
        extraction_method="SCRAPLING_HTTP",
        artifact_ref={
            "status": "SUCCESS",
            "gps": {"lat": 12.4244, "lng": 75.7382},
        },
        created_at=now,
    )
    session.add(obs_madikeri)
    await session.flush()

    claim_madikeri = ClaimModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        subject_id="Madikeri",
        category=ClaimCategory.ATTRACTION.value,
        typed_value={"verified_name": "Madikeri"},
        verification=VerificationStatus.VERIFIED.value,
        availability="AVAILABLE",
        freshness=FreshnessStatus.FRESH.value,
        valid_from=now - timedelta(days=30),
        valid_until=now + timedelta(days=60),
        created_at=now,
        updated_at=now,
    )
    session.add(claim_madikeri)
    await session.flush()

    link_madikeri = ClaimEvidenceModel(
        claim_id=claim_madikeri.id,
        observation_id=obs_madikeri.id,
        support_type="SUPPORTS",
        locator="article p",
        created_at=now,
    )
    session.add(link_madikeri)
    await session.flush()


def _build_intercity_trip_initial_state(
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
            "origin_name": "Bengaluru",
            "origin_lat": 12.9716,
            "origin_lng": 77.5946,
            "destinations": [
                {
                    "name": "Mysuru",
                    "latitude": 12.2958,
                    "longitude": 76.6394,
                    "stay_days": 1,
                    "confidence": 1.0,
                },
                {
                    "name": "Madikeri",
                    "latitude": 12.4244,
                    "longitude": 75.7382,
                    "stay_days": 1,
                    "confidence": 1.0,
                },
            ],
            "start_date": "2026-11-24",
            "end_date": "2026-11-26",
            "travelers": 2,
            "rooms": 1,
            "budget": {"amount": "25000.00", "currency": "INR"},
            "preferred_modes": ["car_petrol"],
        },
        "discovered_places": [
            {
                "name": "Mysuru Palace",
                "category": "heritage",
                "lat": 12.3051,
                "lng": 76.6551,
                "stay_minutes": 120,
            },
            {
                "name": "Raja Seat Madikeri",
                "category": "viewpoint",
                "lat": 12.4200,
                "lng": 75.7300,
                "stay_minutes": 60,
            },
        ],
        "task_outputs": {
            "search-1": {
                "observations": [
                    {
                        "execution_id": str(execution_id),
                        "owner_id": str(owner_id),
                        "canonical_url": "https://karnatakatourism.org/mysuru",
                        "content_hash": "hash_mysuru_dest",
                        "excerpt_text": "Mysuru is a heritage destination in Karnataka known for palaces.",
                        "support_type": "SUPPORTS",
                        "extraction_method": "SCRAPLING_HTTP",
                        "artifact_ref": {
                            "status": "SUCCESS",
                            "gps": {"lat": 12.2958, "lng": 76.6394},
                        },
                    },
                    {
                        "execution_id": str(execution_id),
                        "owner_id": str(owner_id),
                        "canonical_url": "https://karnatakatourism.org/madikeri",
                        "content_hash": "hash_madikeri_dest",
                        "excerpt_text": "Madikeri is a scenic hill town in Kodagu with viewpoints and coffee estates.",
                        "support_type": "SUPPORTS",
                        "extraction_method": "SCRAPLING_HTTP",
                        "artifact_ref": {
                            "status": "SUCCESS",
                            "gps": {"lat": 12.4244, "lng": 75.7382},
                        },
                    },
                ],
                "claims": [
                    {
                        "execution_id": str(execution_id),
                        "owner_id": str(owner_id),
                        "subject_id": "Mysuru",
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
                                        "gps": {"lat": 12.2958, "lng": 76.6394},
                                    },
                                },
                            }
                        ],
                    },
                    {
                        "execution_id": str(execution_id),
                        "owner_id": str(owner_id),
                        "subject_id": "Madikeri",
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
                                        "gps": {"lat": 12.4244, "lng": 75.7382},
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
async def test_valhalla_matrix_rejection_degraded_recovery_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies complete end-to-end recovery when Valhalla /sources_to_targets rejects with HTTP 400/154."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    # 1. Seed database with trip and execution
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

    initial_payload = _build_intercity_trip_initial_state(owner_id, trip_id, execution_id)

    # 2. Start execution -> runs until WAITING_TASKS at route optimizer dispatch
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

    # 3. Fetch dispatched optimizer task and outbox payload
    async with session_factory() as session:
        task_stmt = select(TaskModel).where(TaskModel.execution_id == execution_id)
        task_row = (await session.execute(task_stmt)).scalar_one()
        assert task_row.worker == "OPTIMIZER"
        assert task_row.action == "solve"
        assert task_row.status == "PENDING"

        outbox_stmt = select(OutboxEventModel).where(
            OutboxEventModel.execution_id == execution_id,
            OutboxEventModel.kind == "TASK_DISPATCH",
        )
        dispatch_outbox = (await session.execute(outbox_stmt)).scalar_one()
        dispatch_payload = dispatch_outbox.payload_ref or dispatch_outbox.payload
        assert dispatch_payload is not None

    # 4. Inject exact HTTP 400 / 154 response through actual Valhalla client at HTTP boundary
    valhalla_call_count = 0

    def mock_valhalla_boundary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal valhalla_call_count
        valhalla_call_count += 1
        # Exact response returned by public Valhalla instance when inter-city distance exceeds 150km
        if "/sources_to_targets" in str(request.url):
            return httpx.Response(
                status_code=400,
                headers={"Content-Type": "application/json"},
                json={
                    "error_code": 154,
                    "error": "Path distance exceeds the max distance limit: 150000 meters",
                    "status_code": 400,
                    "status": "Bad Request",
                },
            )
        return httpx.Response(status_code=200, json={})

    transport = httpx.MockTransport(mock_valhalla_boundary_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        injected_valhalla_client = ValhallaClient(
            "http://valhalla.provider.local",
            http_client=http_client,
        )

        # 5. Process through the actual route optimizer consumer
        await process_record(
            dispatch_payload,
            session_factory=session_factory,
            valhalla_client=injected_valhalla_client,
        )

    # Fast-fail nonretryable verification: exact 1 call at HTTP boundary (no retries on 400/154)
    assert valhalla_call_count == 1

    # 6. Verify durable task completion under lease
    async with session_factory() as session:
        task_after = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_row.id))
        ).scalar_one()

        # Invariant 1: No stranded claim (task is COMPLETED, lease token cleared)
        assert task_after.status == "COMPLETED"
        assert task_after.lease_token is None
        assert task_after.lease_expiry is None
        assert task_after.result_id is not None

        # Invariant 2: No phantom route (zero routes persisted for this execution/trip)
        route_check = (
            await session.execute(
                select(RouteModel).where(RouteModel.execution_id == execution_id)
            )
        ).scalars().all()
        assert len(route_check) == 0

        # Invariant 3: Worker result contains degraded ROUTING_UNAVAILABLE output with honest diagnostic
        res_stmt = select(WorkerResultModel).where(WorkerResultModel.id == task_after.result_id)
        worker_res = (await session.execute(res_stmt)).scalar_one()
        assert worker_res.status == "SUCCESS"
        out_ref = worker_res.output_ref
        assert out_ref is not None
        assert out_ref["status"] == "FAILED"
        assert out_ref["routing_status"] == "ROUTING_UNAVAILABLE"
        assert "route_id" not in out_ref
        assert any("distance limit exceeded" in str(d).lower() for d in out_ref["diagnostics"])

        # Invariant 4: Outbox has TASK_COMPLETED event ready for reconciliation
        outbox_completed = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == execution_id,
                    OutboxEventModel.kind == "TASK_COMPLETED",
                )
            )
        ).scalar_one()
        assert outbox_completed is not None

    # 7. Reconcile task completion -> emits EXECUTION_RESUME
    reconciler = CompletionReconciliationService(
        session_factory,
        workflow_graphs={"TRAVEL": trip_graph},
    )
    completion_event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_after.id,
        execution_id=execution_id,
        attempt=task_after.attempt,
        event_type="TASK_COMPLETED",
        result_id=task_after.result_id,
        output=out_ref,
    )
    reconcile_res = await reconciler.reconcile_task_event(event=completion_event)
    assert reconcile_res.status == "APPLIED"
    assert reconcile_res.checkpoint_updated is True
    assert reconcile_res.graph_resumed is True

    # 8. Resume execution -> LangGraph consumes degraded output and finishes SUCCEEDED
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
    assert resume_res.graph_advanced is True

    # 9. Query trip itinerary via the real authenticated HTTP API
    owner_user = AuthenticatedUser(
        id=owner_id,
        email="traveler@swena.internal",
        roles=["user"],
    )

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_uow] = lambda: SqlAlchemyUnitOfWork(session_factory)
    app.dependency_overrides[get_current_user] = lambda: owner_user

    transport_api = ASGITransport(app=app)
    async with AsyncClient(transport=transport_api, base_url="http://testserver") as client:
        res = await client.get(f"/api/v1/trips/{trip_id}/itinerary")
        assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
        itinerary = res.json()

        # Invariant 5: Proposal is published in state 'proposed' with no route ID and ROUTING_UNAVAILABLE
        assert itinerary["state"] == "proposed"
        assert itinerary["status"] == "ready_for_approval"
        assert itinerary["route_id"] is None
        assert itinerary["routing_status"] == "ROUTING_UNAVAILABLE"
        assert itinerary["map_geojson"] is None

        # Invariant 6: Verified places and provenance are preserved
        assert len(itinerary["validated_candidates"]) >= 1
        assert len(itinerary["discovered_places"]) >= 1

        # Invariant 7: Honest diagnostics explain provider rejection without Euclidean guesswork
        assert any(
            "Road routing engine unavailable" in d
            for d in itinerary["route_diagnostics"]
        )
        assert any(
            "distance limit exceeded" in d.lower()
            for d in itinerary["route_diagnostics"]
        )

        # Invariant 8: Road geometry and driving schedule remain unknown
        schedule = itinerary.get("schedule") or {}
        assert len(schedule.get("legs", [])) == 0

    app.dependency_overrides.clear()

    # 10. Invariant 9: Replay safety and duplicate redelivery
    # Redelivering the exact same dispatch payload to the consumer must safely discard
    async with httpx.AsyncClient(transport=transport) as http_client:
        injected_client_replay = ValhallaClient(
            "http://valhalla.provider.local",
            http_client=http_client,
        )
        await process_record(
            dispatch_payload,
            session_factory=session_factory,
            valhalla_client=injected_client_replay,
        )

    # Assert no duplicate delta was created and trip version remained unchanged
    async with session_factory() as session:
        deltas = (
            await session.execute(
                select(TripDeltaModel).where(TripDeltaModel.trip_id == trip_id)
            )
        ).scalars().all()
        assert len(deltas) == 1

        trip_final = (
            await session.execute(select(TripModel).where(TripModel.id == trip_id))
        ).scalar_one()
        assert trip_final.current_version == 1
        assert trip_final.current_route_id is None
        assert trip_final.state == "proposed"
