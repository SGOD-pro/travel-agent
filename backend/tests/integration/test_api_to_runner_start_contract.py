"""Isolated PostgreSQL integration tests for Spec 0007 API-to-runner start contract.

Controlled fixture-provider contract tests (NOT live-provider acceptance):
uses explicit `LLM_PROVIDER=fixture` and `WEATHER_PROVIDER=fixture` with zero paid
live-provider calls while exercising the real HTTP API, real RS256 JWT auth,
real OutboxRelayService serialization, real execution SQS consumer, real
compile_trip_planning_workflow, and real GraphRunnerService against isolated PostgreSQL.

Covers:
1. Authenticated POST /api/v1/trips/{trip_id}/plans -> serialized OutboxMessage ->
   production execution consumer -> GraphRunnerService -> checkpoint + first EVIDENCE task
   dispatch without manual payload hydration or direct graph-node calls.
2. Collector budget initialized strictly via QueryPlanningService owning policy;
   client planning_options cannot authorize arbitrary spending or override identity/brief.
3. Duplicate API acceptance and duplicate outbox delivery create zero duplicate
   executions, tasks, outbox events, or budget re-initializations.
4. Cross-owner API access and forged/mismatched runner start identity are rejected.
5. Brief updates after acceptance (v1 -> v2) cannot silently alter the accepted v1
   execution snapshot or its dispatched evidence queries.
6. Acceptance rollback leaves zero partial api_idempotency, executions, outbox_events,
   or job_events rows; missing persisted brief snapshot or unsupported workflow fails closed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acceptance_keys import TEST_PUBLIC_KEY_PEM, generate_jwt
from config.config import settings
from config.db import get_session
from functions.api.app import app
from functions.api.idempotency import IdempotencyRepository
from functions.api.models import ApiIdempotencyModel, TripModel, TripVersionModel
from functions.api.repository import SqlAlchemyUnitOfWork
from functions.api.routes.trips import get_uow
from functions.orchestrator.evidence.query_planner import QueryPlanningService
from functions.orchestrator.execution.budget import ensure_execution_budget_structure
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.consumer import process_batch as runner_process_batch
from functions.orchestrator.execution.models import (
    ExecutionModel,
    JobEventModel,
    OutboxEventModel,
    TaskModel,
)
from functions.orchestrator.execution.relay import OutboxRelayService
from functions.orchestrator.execution.runner import (
    ExecutionOwnerMismatchError,
    ExecutionStartEvent,
    ExecutionTripMismatchError,
    GraphRunnerError,
    GraphRunnerService,
)
from functions.orchestrator.execution.transport import ControlledTransportAdapter
from functions.orchestrator.graphs.trip_planner import compile_trip_planning_workflow
from middleware.auth import get_current_user


class _StubLambdaContext:
    def __init__(self, remaining_ms: int = 30_000) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


def _make_brief(
    *,
    origin_name: str = "Bengaluru",
    destination_name: str = "Mysuru",
    dest_lat: float = 12.2958,
    dest_lng: float = 76.6394,
    start_date: str = "2026-12-01",
    end_date: str = "2026-12-02",
) -> dict[str, Any]:
    return {
        "origin_name": origin_name,
        "origin_lat": 12.9716,
        "origin_lng": 77.5946,
        "destinations": [
            {
                "name": destination_name,
                "latitude": dest_lat,
                "longitude": dest_lng,
                "stay_days": 2,
            }
        ],
        "start_date": start_date,
        "end_date": end_date,
        "timezone": "Asia/Kolkata",
        "adults": 2,
    }


def _configure_controlled_env(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Configures real RS256 auth and explicit fixture providers (no live paid calls)."""
    monkeypatch.setattr(settings, "AUTH_PUBLIC_KEY", TEST_PUBLIC_KEY_PEM)
    monkeypatch.setattr(settings, "AUTH_ISSUER", "https://oauth21.vercel.app")
    monkeypatch.setattr(settings, "CLIENT_ID", "swena_travel_agent_client")
    monkeypatch.setattr(settings, "ALLOW_DEV_LOCAL_USER", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fixture")
    monkeypatch.setattr(settings, "WEATHER_PROVIDER", "fixture")

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_uow] = lambda: SqlAlchemyUnitOfWork(session_factory)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_authenticated_api_to_runner_start_contract_end_to_end(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: TransactionBoundCheckpointer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies authenticated API acceptance -> serialized outbox -> production consumer ->
    GraphRunnerService -> checkpoint and first EVIDENCE task dispatch without manual hydration.
    """
    _configure_controlled_env(monkeypatch, session_factory)
    owner_id = uuid.uuid4()
    owner_token = generate_jwt(sub=owner_id, email="owner@swena.internal")
    brief_v1 = _make_brief(destination_name="Mysuru")

    expected_queries = QueryPlanningService.plan_destination_queries(brief_v1)
    expected_budget = ensure_execution_budget_structure(
        QueryPlanningService.calculate_collector_budget(expected_queries)
    )

    try:
        transport_http = ASGITransport(app=app)
        async with AsyncClient(transport=transport_http, base_url="http://test") as client:
            # 1. Create trip with persisted v1 brief snapshot through the API
            create_resp = await client.post(
                "/api/v1/trips",
                json={"owner_id": str(owner_id), "brief": brief_v1},
            )
            assert create_resp.status_code == 201, create_resp.text
            trip_id = uuid.UUID(create_resp.json()["id"])

            # 2. Call authenticated POST /api/v1/trips/{trip_id}/plans attempting to inject
            #    arbitrary budget, owner_id, and brief overrides in planning_options
            idemp_key = f"start-contract-{uuid.uuid4()}"
            spoofed_options = {
                "budget": {
                    "remaining_tool_calls": 9999,
                    "remaining_cost_minor": 999_999,
                    "currency": "USD",
                },
                "owner_id": str(uuid.uuid4()),
                "brief": _make_brief(destination_name="SpoofedCity"),
                "version": 99,
            }
            accept_resp = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Idempotency-Key": idemp_key,
                },
                json={"planning_options": spoofed_options, "workflow": "TRAVEL"},
            )
            assert accept_resp.status_code == 202, accept_resp.text
            accept_body = accept_resp.json()
            execution_id = uuid.UUID(accept_body["job_id"])
            assert accept_body["status"] == "QUEUED"
            assert accept_body["status_url"] == f"/api/v1/jobs/{execution_id}"

        # 3. Verify persisted ExecutionModel and budget initialization before consumer runs
        async with session_factory() as session:
            execution = (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == execution_id)
                )
            ).scalar_one()
            assert execution.owner_id == owner_id
            assert execution.trip_id == trip_id
            assert execution.workflow == "TRAVEL"
            assert execution.status == "CREATED"
            assert execution.budget == expected_budget
            assert execution.budget["remaining_tool_calls"] == 4
            assert execution.budget["remaining_cost_minor"] == 200
            assert execution.budget["currency"] == "INR"

        # 4. Relay actual outbox record via OutboxRelayService -> OutboxMessage.to_json()
        outbox_transport = ControlledTransportAdapter()
        relay = OutboxRelayService(session_factory, outbox_transport)
        relay_token = uuid.uuid4()
        claimed = await relay.claim_due_batch(relay_token=relay_token, execution_id=execution_id)
        assert len(claimed) == 1
        batch_result = await relay.process_batch(claimed, relay_token=relay_token)
        assert batch_result.claimed_count == 1
        assert batch_result.delivered_count == 1
        assert len(outbox_transport.published) == 1

        relayed_msg = outbox_transport.published[0]
        assert relayed_msg.kind == "EXECUTION_START"
        assert relayed_msg.execution_id == execution_id
        serialized_body = relayed_msg.to_json()

        # 5. Deliver the exact serialized outbox JSON to production execution consumer (runner=None)
        sqs_event = {
            "Records": [
                {
                    "messageId": f"msg-{relayed_msg.event_id}",
                    "body": serialized_body,
                }
            ]
        }
        with patch(
            "functions.orchestrator.execution.consumer.get_session_factory",
            return_value=session_factory,
        ):
            failures = await runner_process_batch(sqs_event, _StubLambdaContext())

        assert failures == []

        # 6. Verify GraphRunnerService advanced to WAITING_TASKS, saved checkpoint,
        #    and dispatched first EVIDENCE collect task with owning policy budget
        async with session_factory() as session:
            execution_after = (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == execution_id)
                )
            ).scalar_one()
            assert execution_after.status == "WAITING_TASKS"
            assert execution_after.checkpoint_id is not None
            assert execution_after.owner_id == owner_id
            assert execution_after.trip_id == trip_id
            assert execution_after.budget == expected_budget

            tasks = (
                (
                    await session.execute(
                        select(TaskModel).where(TaskModel.execution_id == execution_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(tasks) == 1
            evidence_task = tasks[0]
            assert evidence_task.worker == "EVIDENCE"
            assert evidence_task.action == "collect"
            assert evidence_task.status == "PENDING"
            assert evidence_task.payload_ref["destination"] == "Mysuru"
            assert evidence_task.payload_ref["budget"]["remaining_tool_calls"] == 4
            assert evidence_task.payload_ref["budget"]["remaining_cost_minor"] == 200

            dispatch_outbox = (
                (
                    await session.execute(
                        select(OutboxEventModel).where(
                            OutboxEventModel.execution_id == execution_id,
                            OutboxEventModel.kind == "TASK_DISPATCH",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(dispatch_outbox) == 1
            assert dispatch_outbox[0].task_id == evidence_task.id

        # 7. Verify persisted LangGraph checkpoint state holds exact accepted owner, trip, version, and brief
        compiled_graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
        checkpoint_state = await compiled_graph.aget_state(
            config={"configurable": {"thread_id": str(execution_id)}}
        )
        assert checkpoint_state is not None
        assert checkpoint_state.values["owner_id"] == str(owner_id)
        assert checkpoint_state.values["trip_id"] == str(trip_id)
        assert checkpoint_state.values["version"] == 1
        assert checkpoint_state.values["brief"]["destinations"][0]["name"] == "Mysuru"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_duplicate_acceptance_and_duplicate_delivery_are_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: TransactionBoundCheckpointer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies duplicate API acceptance and duplicate EXECUTION_START delivery create
    no duplicate executions, tasks, outbox rows, or budget re-initialization.
    """
    _configure_controlled_env(monkeypatch, session_factory)
    owner_id = uuid.uuid4()
    owner_token = generate_jwt(sub=owner_id, email="owner@swena.internal")
    brief_v1 = _make_brief(destination_name="Mysuru")

    try:
        transport_http = ASGITransport(app=app)
        async with AsyncClient(transport=transport_http, base_url="http://test") as client:
            create_resp = await client.post(
                "/api/v1/trips",
                json={"owner_id": str(owner_id), "brief": brief_v1},
            )
            assert create_resp.status_code == 201
            trip_id = uuid.UUID(create_resp.json()["id"])

            idemp_key = f"dup-start-{uuid.uuid4()}"
            req_body = {"planning_options": {"pace": "balanced"}, "workflow": "TRAVEL"}
            headers = {
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": idemp_key,
            }

            resp_1 = await client.post(
                f"/api/v1/trips/{trip_id}/plans", headers=headers, json=req_body
            )
            resp_2 = await client.post(
                f"/api/v1/trips/{trip_id}/plans", headers=headers, json=req_body
            )
            assert resp_1.status_code == 202
            assert resp_2.status_code == 202
            assert resp_1.json()["job_id"] == resp_2.json()["job_id"]
            execution_id = uuid.UUID(resp_1.json()["job_id"])

        async with session_factory() as session:
            exec_rows = (
                (await session.execute(select(ExecutionModel).where(ExecutionModel.trip_id == trip_id)))
                .scalars()
                .all()
            )
            assert len(exec_rows) == 1
            start_outbox_rows = (
                (
                    await session.execute(
                        select(OutboxEventModel).where(
                            OutboxEventModel.execution_id == execution_id,
                            OutboxEventModel.kind == "EXECUTION_START",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(start_outbox_rows) == 1

        outbox_transport = ControlledTransportAdapter()
        relay = OutboxRelayService(session_factory, outbox_transport)
        relay_token = uuid.uuid4()
        claimed = await relay.claim_due_batch(relay_token=relay_token, execution_id=execution_id)
        assert len(claimed) == 1
        batch_result = await relay.process_batch(claimed, relay_token=relay_token)
        assert batch_result.delivered_count == 1
        relayed_msg = outbox_transport.published[0]

        sqs_event = {
            "Records": [
                {
                    "messageId": f"msg-1-{relayed_msg.event_id}",
                    "body": relayed_msg.to_json(),
                }
            ]
        }

        with patch(
            "functions.orchestrator.execution.consumer.get_session_factory",
            return_value=session_factory,
        ):
            failures_1 = await runner_process_batch(sqs_event, _StubLambdaContext())
            assert failures_1 == []

            # Simulate budget spend occurring before a duplicate EXECUTION_START arrives
            async with session_factory() as session:
                exec_row = (
                    await session.execute(
                        select(ExecutionModel).where(ExecutionModel.id == execution_id)
                    )
                ).scalar_one()
                assert exec_row.budget is not None
                updated_budget = dict(exec_row.budget)
                updated_budget["remaining_tool_calls"] = 3
                updated_budget["remaining_cost_minor"] = 150
                updated_budget["spent_tool_calls"] = 1
                updated_budget["spent_cost_minor"] = 50
                exec_row.budget = updated_budget
                await session.commit()

            # Deliver exact duplicate EXECUTION_START message
            dup_sqs_event = {
                "Records": [
                    {
                        "messageId": f"msg-2-{relayed_msg.event_id}",
                        "body": relayed_msg.to_json(),
                    }
                ]
            }
            failures_2 = await runner_process_batch(dup_sqs_event, _StubLambdaContext())
            assert failures_2 == []

        async with session_factory() as session:
            exec_after = (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == execution_id)
                )
            ).scalar_one()
            assert exec_after.status == "WAITING_TASKS"
            # Duplicate delivery must NEVER re-initialize or overwrite existing budget state
            assert exec_after.budget is not None
            assert exec_after.budget["remaining_tool_calls"] == 3
            assert exec_after.budget["remaining_cost_minor"] == 150
            assert exec_after.budget["spent_tool_calls"] == 1
            assert exec_after.budget["spent_cost_minor"] == 50

            tasks = (
                (
                    await session.execute(
                        select(TaskModel).where(TaskModel.execution_id == execution_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(tasks) == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cross_owner_start_and_runner_identity_mismatch_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: TransactionBoundCheckpointer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies cross-owner API start is rejected and GraphRunnerService fails closed on
    owner_id or trip_id mismatch in EXECUTION_START payloads.
    """
    _configure_controlled_env(monkeypatch, session_factory)
    owner_id = uuid.uuid4()
    other_owner_id = uuid.uuid4()
    other_token = generate_jwt(sub=other_owner_id, email="intruder@swena.internal")
    owner_token = generate_jwt(sub=owner_id, email="owner@swena.internal")
    brief_v1 = _make_brief(destination_name="Mysuru")

    try:
        transport_http = ASGITransport(app=app)
        async with AsyncClient(transport=transport_http, base_url="http://test") as client:
            create_resp = await client.post(
                "/api/v1/trips",
                json={"owner_id": str(owner_id), "brief": brief_v1},
            )
            assert create_resp.status_code == 201
            trip_id = uuid.UUID(create_resp.json()["id"])

            # 1. Unauthenticated request -> HTTP 401
            unauth_resp = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers={"Idempotency-Key": f"unauth-{uuid.uuid4()}"},
                json={"workflow": "TRAVEL"},
            )
            assert unauth_resp.status_code == 401

            # 2. Authenticated cross-owner request -> HTTP 404 with zero persisted rows
            cross_key = f"cross-{uuid.uuid4()}"
            cross_resp = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers={
                    "Authorization": f"Bearer {other_token}",
                    "Idempotency-Key": cross_key,
                },
                json={"workflow": "TRAVEL"},
            )
            assert cross_resp.status_code == 404

            async with session_factory() as session:
                exec_rows = (
                    (
                        await session.execute(
                            select(ExecutionModel).where(ExecutionModel.trip_id == trip_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert exec_rows == []
                idemp_rows = (
                    (
                        await session.execute(
                            select(ApiIdempotencyModel).where(
                                ApiIdempotencyModel.key == cross_key
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert idemp_rows == []

            # 3. Legitimate owner accepts plan; runner rejects tampered owner_id or trip_id
            ok_resp = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Idempotency-Key": f"owner-ok-{uuid.uuid4()}",
                },
                json={"workflow": "TRAVEL"},
            )
            assert ok_resp.status_code == 202
            execution_id = uuid.UUID(ok_resp.json()["job_id"])

        compiled_graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
        runner = GraphRunnerService(
            session_factory,
            default_graph=compiled_graph,
            workflow_graphs={"TRAVEL": compiled_graph},
        )

        with pytest.raises(ExecutionOwnerMismatchError, match="owner_id") as exc_owner:
            await runner.handle_execution_start(
                event=ExecutionStartEvent(
                    execution_id=execution_id,
                    workflow="TRAVEL",
                    context_version=1,
                    payload={
                        "owner_id": str(other_owner_id),
                        "trip_id": str(trip_id),
                        "version": 1,
                        "brief": brief_v1,
                    },
                )
            )
        assert isinstance(exc_owner.value, PermissionError)
        assert isinstance(exc_owner.value, GraphRunnerError)

        with pytest.raises(ExecutionTripMismatchError, match="trip_id") as exc_trip:
            await runner.handle_execution_start(
                event=ExecutionStartEvent(
                    execution_id=execution_id,
                    workflow="TRAVEL",
                    context_version=1,
                    payload={
                        "owner_id": str(owner_id),
                        "trip_id": str(uuid.uuid4()),
                        "version": 1,
                        "brief": brief_v1,
                    },
                )
            )
        assert isinstance(exc_trip.value, ValueError)
        assert isinstance(exc_trip.value, GraphRunnerError)

        # Execution must remain CREATED without acquiring lease or dispatching tasks
        async with session_factory() as session:
            exec_row = (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == execution_id)
                )
            ).scalar_one()
            assert exec_row.status == "CREATED"
            assert exec_row.lease_token is None
            tasks = (
                (
                    await session.execute(
                        select(TaskModel).where(TaskModel.execution_id == execution_id)
                    )
                )
                .scalars()
                .all()
            )
            assert tasks == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_brief_update_after_acceptance_preserves_accepted_version_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    postgres_checkpointer: TransactionBoundCheckpointer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that updating a trip brief from v1 to v2 after plan acceptance never
    silently replaces the accepted v1 brief when EXECUTION_START is relayed and consumed.
    """
    _configure_controlled_env(monkeypatch, session_factory)
    owner_id = uuid.uuid4()
    owner_token = generate_jwt(sub=owner_id, email="owner@swena.internal")
    brief_v1 = _make_brief(
        destination_name="Mysuru",
        dest_lat=12.2958,
        dest_lng=76.6394,
    )
    brief_v2 = _make_brief(
        destination_name="Ooty",
        dest_lat=11.4102,
        dest_lng=76.6950,
    )

    try:
        transport_http = ASGITransport(app=app)
        async with AsyncClient(transport=transport_http, base_url="http://test") as client:
            # 1. Create trip at v1 (Mysuru)
            create_resp = await client.post(
                "/api/v1/trips",
                json={"owner_id": str(owner_id), "brief": brief_v1},
            )
            assert create_resp.status_code == 201
            trip_id = uuid.UUID(create_resp.json()["id"])

            # 2. Accept plan at v1 BEFORE any brief change
            idemp_key = f"v1-plan-{uuid.uuid4()}"
            headers = {
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": idemp_key,
            }
            accept_resp = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers=headers,
                json={"workflow": "TRAVEL"},
            )
            assert accept_resp.status_code == 202
            execution_id = uuid.UUID(accept_resp.json()["job_id"])

            # 3. Mutate trip brief to v2 (Ooty) BEFORE EXECUTION_START is relayed
            update_resp = await client.post(
                f"/api/v1/trips/{trip_id}/brief",
                json={
                    "expected_base_version": 1,
                    "brief": brief_v2,
                    "command_id": str(uuid.uuid4()),
                },
            )
            assert update_resp.status_code == 200, update_resp.text
            assert update_resp.json()["current_version"] == 2

            # 4. Replaying the v1 Idempotency-Key returns the v1 job_id without modifying v1
            replay_resp = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers=headers,
                json={"workflow": "TRAVEL"},
            )
            assert replay_resp.status_code == 202
            assert replay_resp.json()["job_id"] == str(execution_id)

        # 5. Now relay and consume EXECUTION_START
        outbox_transport = ControlledTransportAdapter()
        relay = OutboxRelayService(session_factory, outbox_transport)
        relay_token = uuid.uuid4()
        claimed = await relay.claim_due_batch(relay_token=relay_token, execution_id=execution_id)
        assert len(claimed) == 1
        batch_result = await relay.process_batch(claimed, relay_token=relay_token)
        assert batch_result.delivered_count == 1
        relayed_msg = outbox_transport.published[0]

        sqs_event = {
            "Records": [
                {
                    "messageId": f"msg-v1-{relayed_msg.event_id}",
                    "body": relayed_msg.to_json(),
                }
            ]
        }
        with patch(
            "functions.orchestrator.execution.consumer.get_session_factory",
            return_value=session_factory,
        ):
            failures = await runner_process_batch(sqs_event, _StubLambdaContext())
        assert failures == []

        # 6. Verify checkpoint and dispatched EVIDENCE task strictly used v1 (Mysuru), NOT v2 (Ooty)
        compiled_graph = compile_trip_planning_workflow(checkpointer=postgres_checkpointer)
        checkpoint_state = await compiled_graph.aget_state(
            config={"configurable": {"thread_id": str(execution_id)}}
        )
        assert checkpoint_state.values["version"] == 1
        assert checkpoint_state.values["brief"]["destinations"][0]["name"] == "Mysuru"

        async with session_factory() as session:
            trip_row = (
                await session.execute(select(TripModel).where(TripModel.id == trip_id))
            ).scalar_one()
            assert trip_row.current_version == 2

            task = (
                await session.execute(
                    select(TaskModel).where(TaskModel.execution_id == execution_id)
                )
            ).scalar_one()
            assert task.payload_ref["destination"] == "Mysuru"
            query_texts = [
                q.get("query", "") if isinstance(q, dict) else str(q)
                for q in task.payload_ref["queries"]
            ]
            assert all("Ooty" not in q_text for q_text in query_texts)
            assert any("Mysuru" in q_text for q_text in query_texts)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_acceptance_rollback_and_missing_brief_leave_zero_partial_records(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies atomic rollback leaves zero partial execution/outbox/idempotency/job_event
    records and missing brief snapshots or unsupported workflows fail closed.
    """
    _configure_controlled_env(monkeypatch, session_factory)
    owner_id = uuid.uuid4()
    owner_token = generate_jwt(sub=owner_id, email="owner@swena.internal")
    brief_v1 = _make_brief(destination_name="Mysuru")

    try:
        # 1. Seed a TripModel with no TripVersionModel -> HTTP 400 with zero persisted rows
        trip_without_brief_id = uuid.uuid4()
        now = datetime.now(UTC)
        async with session_factory() as session:
            session.add(
                TripModel(
                    id=trip_without_brief_id,
                    owner_id=owner_id,
                    current_version=1,
                    state="draft",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

        transport_http = ASGITransport(app=app)
        async with AsyncClient(transport=transport_http, base_url="http://test") as client:
            missing_brief_key = f"no-brief-{uuid.uuid4()}"
            resp_no_brief = await client.post(
                f"/api/v1/trips/{trip_without_brief_id}/plans",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Idempotency-Key": missing_brief_key,
                },
                json={"workflow": "TRAVEL"},
            )
            assert resp_no_brief.status_code == 400
            assert "no valid brief snapshot" in resp_no_brief.json()["detail"].lower()

            # 2. Create a valid trip and verify unsupported workflow fails closed with HTTP 400
            create_resp = await client.post(
                "/api/v1/trips",
                json={"owner_id": str(owner_id), "brief": brief_v1},
            )
            assert create_resp.status_code == 201
            valid_trip_id = uuid.UUID(create_resp.json()["id"])

            bad_wf_key = f"bad-wf-{uuid.uuid4()}"
            resp_bad_wf = await client.post(
                f"/api/v1/trips/{valid_trip_id}/plans",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Idempotency-Key": bad_wf_key,
                },
                json={"workflow": "ROUTE_OPTIMIZER"},
            )
            assert resp_bad_wf.status_code == 400
            assert "TRAVEL" in resp_bad_wf.json()["detail"]

        # 3. Capture baseline outbox/job_event counts and simulate mid-acceptance failures
        async with session_factory() as session:
            baseline_outbox_ids = set(
                (await session.execute(select(OutboxEventModel.id))).scalars().all()
            )
            baseline_job_event_ids = set(
                (await session.execute(select(JobEventModel.id))).scalars().all()
            )

        rollback_key_1 = f"rb-exec-{uuid.uuid4()}"
        rollback_key_2 = f"rb-outbox-{uuid.uuid4()}"
        planned_queries = QueryPlanningService.plan_destination_queries(brief_v1)
        budget = ensure_execution_budget_structure(
            QueryPlanningService.calculate_collector_budget(planned_queries)
        )
        start_payload = {
            "trip_id": str(valid_trip_id),
            "owner_id": str(owner_id),
            "version": 1,
            "brief": brief_v1,
            "workflow": "TRAVEL",
            "context_version": 1,
        }

        async with session_factory() as session:
            repo = IdempotencyRepository(session)
            with pytest.raises(RuntimeError, match="after executions insert"):
                await repo.accept_job(
                    owner_id=owner_id,
                    operation=f"start_trip_plan:{valid_trip_id}",
                    idempotency_key=rollback_key_1,
                    payload={"trip_id": str(valid_trip_id), "workflow": "TRAVEL"},
                    trip_id=valid_trip_id,
                    workflow="TRAVEL",
                    start_payload=start_payload,
                    execution_budget=budget,
                    test_fail_after_execution_insert=True,
                )

        async with session_factory() as session:
            repo = IdempotencyRepository(session)
            with pytest.raises(RuntimeError, match="after outbox_events insert"):
                await repo.accept_job(
                    owner_id=owner_id,
                    operation=f"start_trip_plan:{valid_trip_id}",
                    idempotency_key=rollback_key_2,
                    payload={"trip_id": str(valid_trip_id), "workflow": "TRAVEL"},
                    trip_id=valid_trip_id,
                    workflow="TRAVEL",
                    start_payload=start_payload,
                    execution_budget=budget,
                    test_fail_after_outbox_insert=True,
                )

        # 4. Verify zero partial rows remain across all four tables
        async with session_factory() as session:
            for tid in (trip_without_brief_id, valid_trip_id):
                exec_rows = (
                    (await session.execute(select(ExecutionModel).where(ExecutionModel.trip_id == tid)))
                    .scalars()
                    .all()
                )
                assert exec_rows == []
            for key in (missing_brief_key, bad_wf_key, rollback_key_1, rollback_key_2):
                idemp_rows = (
                    (
                        await session.execute(
                            select(ApiIdempotencyModel).where(ApiIdempotencyModel.key == key)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert idemp_rows == []
            after_outbox_ids = set(
                (await session.execute(select(OutboxEventModel.id))).scalars().all()
            )
            after_job_event_ids = set(
                (await session.execute(select(JobEventModel.id))).scalars().all()
            )
            assert after_outbox_ids == baseline_outbox_ids
            assert after_job_event_ids == baseline_job_event_ids
            owner_execs = (
                (
                    await session.execute(
                        select(ExecutionModel).where(ExecutionModel.owner_id == owner_id)
                    )
                )
                .scalars()
                .all()
            )
            assert owner_execs == []
            # Ensure TripVersionModel table remains intact for valid_trip_id
            tv_row = (
                await session.execute(
                    select(TripVersionModel).where(
                        TripVersionModel.trip_id == valid_trip_id,
                        TripVersionModel.version == 1,
                    )
                )
            ).scalar_one_or_none()
            assert tv_row is not None
    finally:
        app.dependency_overrides.clear()
