"""Connected End-to-End Trace Verification conforming to spec 0001.

Validates the full connected execution flow:
1. HTTP Job Acceptance (POST /api/v1/trips/{trip_id}/plans) -> 202 Accepted + EXECUTION_START outbox
2. EXECUTION_START SQS consumption -> Graph execution -> Task dispatch -> WAITING_TASKS + TASK_DISPATCH outbox
3. TASK_DISPATCH SQS consumption -> Worker claim & complete via WorkerFencingService
4. TASK_COMPLETED SQS consumption -> CompletionReconciliationService -> Checkpoint state update -> WAITING_TASKS resolved -> EXECUTION_RESUME outbox
5. EXECUTION_RESUME SQS consumption -> GraphRunnerService resume -> actual graph continuation -> SUCCEEDED + EXECUTION_SUCCEEDED outbox
6. Terminal job status and full timeline query via HTTP API (GET /api/v1/jobs/{job_id})
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

backend_dir = "/home/swyra/projects/travel-agent/backend"
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
backend_src = "/home/swyra/projects/travel-agent/backend/src"
if backend_src not in sys.path:
    sys.path.insert(0, backend_src)

from httpx import ASGITransport, AsyncClient
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.config import settings
from config.db import get_session
from functions.api.app import app
from functions.api.models import TripModel
from functions.orchestrator.execution import (
    CompletionReconciliationService,
    ExecutionModel,
    ExecutionState,
    GraphRunnerService,
    OutboxEventModel,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.completion_consumer import (
    process_batch as completion_process_batch,
)
from functions.orchestrator.execution.consumer import process_batch as runner_process_batch
from functions.orchestrator.execution.worker_consumer import (
    process_batch as worker_process_batch,
)
from functions.orchestrator.execution.executor_registry import EXECUTOR_REGISTRY
from middleware.auth import AuthenticatedUser, get_current_user

# Inject a mock executor for the E2E test graph tasks so that dispatch succeeds
async def _mock_executor(payload: Any) -> Any:
    return {"status": "mock_success", "payload_received": payload}

EXECUTOR_REGISTRY[("EVIDENCE", "collect")] = _mock_executor
EXECUTOR_REGISTRY[("TEST", "resume")] = _mock_executor


def build_connected_e2e_graph(checkpointer: TransactionBoundCheckpointer):
    """Builds a connected test graph that emits tasks on START, pauses, and resumes on RESUME."""

    def worker_node(state: ExecutionState) -> dict[str, Any]:
        return {
            "current_step": "tasks_dispatched",
            "pending_tasks": [
                {
                    "worker": "EVIDENCE",
                    "action": "collect",
                    "payload": {
                        "location": "Paris",
                        "poi_types": ["museum", "landmark"],
                    },
                    "idempotency_key": "step1-evidence-collect",
                }
            ],
        }

    def join_node(state: ExecutionState) -> dict[str, Any]:
        outputs = state.get("task_outputs", {})
        return {
            "current_step": "plan_finalized",
            "status": "SUCCEEDED",
            "pending_tasks": [],
            "proposal": {
                "title": "Connected Paris Itinerary",
                "collected_evidence": outputs,
                "status": "ready_for_approval",
            },
        }

    builder = StateGraph(ExecutionState)
    builder.add_node("worker_node", worker_node)
    builder.add_node("join_node", join_node)
    builder.add_edge(START, "worker_node")
    builder.add_edge("worker_node", "join_node")
    builder.add_edge("join_node", END)

    # Interrupt before join_node so the graph pauses to wait for external worker tasks
    return builder.compile(checkpointer=checkpointer, interrupt_before=["join_node"])


async def main() -> None:
    print("=" * 70)
    print("STARTING CONNECTED E2E TRACE (Spec 0001)")
    print("=" * 70)

    db_url = settings.DATABASE_URL.replace("?sslmode=require", "")
    engine = create_async_engine(db_url, poolclass=NullPool)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()

    # Pre-seed trip in database
    async with session_factory() as session:
        trip = TripModel(id=trip_id, owner_id=owner_id, current_version=1, state="draft")
        session.add(trip)
        await session.commit()

    current_authenticated_user = AuthenticatedUser(id=owner_id, email="owner@travel.test")

    async def override_get_current_user():
        return current_authenticated_user

    async def override_get_session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_session] = override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # ----------------------------------------------------------------------
        # STEP 1: HTTP Job Acceptance
        # ----------------------------------------------------------------------
        print("\n[Step 1] HTTP Job Acceptance via POST /api/v1/trips/{trip_id}/plans...")
        idempotency_key = str(uuid.uuid4())
        resp = await client.post(
            f"/api/v1/trips/{trip_id}/plans",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "workflow": "TRAVEL",
                "brief": {
                    "origin_name": "London",
                    "origin_lat": 51.5074,
                    "origin_lng": -0.1278,
                    "destinations": [
                        {
                            "name": "Paris",
                            "latitude": 48.8566,
                            "longitude": 2.3522,
                            "stay_days": 2,
                        }
                    ],
                    "start_date": "2026-11-01",
                    "end_date": "2026-11-05",
                    "adults": 2,
                },
            },
        )
        assert resp.status_code == 202, f"Failed job acceptance: {resp.status_code} {resp.text}"
        job_data = resp.json()
        job_id = uuid.UUID(job_data["job_id"])
        print(f" -> Job accepted successfully. Job ID: {job_id}")

        async with session_factory() as session:
            exec_row = (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == job_id)
                )
            ).scalar_one_or_none()
            assert exec_row is not None, "Execution record missing in database"

            outbox_res = await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == job_id,
                    OutboxEventModel.kind == "EXECUTION_START",
                )
            )
            start_event = outbox_res.scalar_one_or_none()
            assert start_event is not None, "EXECUTION_START outbox event not emitted"
            print(f" -> Verified database: Execution {exec_row.status}, Outbox Event {start_event.id} (EXECUTION_START)")

        # ----------------------------------------------------------------------
        # STEP 2: EXECUTION_START Consumer -> WAITING_TASKS + TASK_DISPATCH
        # ----------------------------------------------------------------------
        print("\n[Step 2] Processing EXECUTION_START through GraphRunner Consumer...")
        checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
        connected_graph = build_connected_e2e_graph(checkpointer)
        runner = GraphRunnerService(session_factory, default_graph=connected_graph)

        start_sqs_event = {
            "Records": [
                {
                    "messageId": f"msg-start-{job_id}",
                    "body": json.dumps(
                        {
                            "id": str(start_event.id),
                            "kind": "EXECUTION_START",
                            "execution_id": str(job_id),
                            "workflow": "TRAVEL",
                            "context_version": 1,
                            "payload_ref": start_event.payload_ref,
                        }
                    ),
                }
            ]
        }

        failures = await runner_process_batch(start_sqs_event, None, runner=runner)
        assert failures == [], f"Graph runner returned failures: {failures}"

        async with session_factory() as session:
            exec_row = (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == job_id)
                )
            ).scalar_one_or_none()
            assert exec_row.status == "WAITING_TASKS", f"Expected WAITING_TASKS, got {exec_row.status}"

            task_res = await session.execute(
                select(TaskModel).where(TaskModel.execution_id == job_id)
            )
            dispatched_task = task_res.scalar_one_or_none()
            assert dispatched_task is not None, "Task record not created"
            assert dispatched_task.status == "PENDING"

            dispatch_outbox_res = await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == job_id,
                    OutboxEventModel.kind == "TASK_DISPATCH",
                )
            )
            dispatch_event = dispatch_outbox_res.scalar_one_or_none()
            assert dispatch_event is not None, "TASK_DISPATCH outbox event not emitted"
            print(f" -> Verified: Execution status is WAITING_TASKS, Task {dispatched_task.id} (PENDING), Outbox TASK_DISPATCH emitted")

        # ----------------------------------------------------------------------
        # STEP 3: TASK_DISPATCH Worker Consumer -> Worker claim and completion
        # ----------------------------------------------------------------------
        print("\n[Step 3] Processing TASK_DISPATCH through Worker Consumer...")
        worker_sqs_event = {
            "Records": [
                {
                    "messageId": f"msg-dispatch-{dispatched_task.id}",
                    "body": json.dumps(
                        {
                            "id": str(dispatch_event.id),
                            "kind": "TASK_DISPATCH",
                            "task_id": str(dispatched_task.id),
                            "execution_id": str(job_id),
                            "attempt": 0,
                            "context_version": 1,
                            "payload_ref": {
                                "worker": dispatched_task.worker,
                                "action": dispatched_task.action,
                                "payload": {"location": "Paris"},
                            },
                        }
                    ),
                }
            ]
        }

        worker_failures = await worker_process_batch(
            worker_sqs_event, None, session_factory=session_factory
        )
        assert worker_failures == [], f"Worker consumer failed: {worker_failures}"

        async with session_factory() as session:
            updated_task = (
                await session.execute(
                    select(TaskModel).where(TaskModel.id == dispatched_task.id)
                )
            ).scalar_one()
            assert updated_task.status == "COMPLETED", f"Expected task COMPLETED, got {updated_task.status}"

            res_record = (
                await session.execute(
                    select(WorkerResultModel).where(
                        WorkerResultModel.task_id == dispatched_task.id
                    )
                )
            ).scalar_one_or_none()
            assert res_record is not None, "WorkerResultModel record missing"
            print(f" -> Verified: Task {updated_task.id} status is COMPLETED with WorkerResult recorded")

        # ----------------------------------------------------------------------
        # STEP 4: TASK_COMPLETED Completion Consumer -> Reconcile & Emit RESUME
        # ----------------------------------------------------------------------
        print("\n[Step 4] Processing TASK_COMPLETED through Completion Receipt Consumer...")
        reconciler = CompletionReconciliationService(session_factory, default_graph=connected_graph)
        completion_receipt_id = uuid.uuid4()
        completion_sqs_event = {
            "Records": [
                {
                    "messageId": f"msg-completion-{dispatched_task.id}",
                    "body": json.dumps(
                        {
                            "id": str(completion_receipt_id),
                            "kind": "TASK_COMPLETED",
                            "task_id": str(dispatched_task.id),
                            "execution_id": str(job_id),
                            "attempt": 1,
                            "result_id": str(res_record.id),
                            "output": {"status": "mock_success", "location": "Paris"},
                        }
                    ),
                }
            ]
        }

        completion_failures = await completion_process_batch(
            completion_sqs_event, None, reconciler=reconciler
        )
        assert completion_failures == [], f"Completion consumer failed: {completion_failures}"

        async with session_factory() as session:
            resume_outbox_res = await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.execution_id == job_id,
                    OutboxEventModel.kind == "EXECUTION_RESUME",
                )
            )
            resume_event = resume_outbox_res.scalar_one_or_none()
            assert resume_event is not None, "EXECUTION_RESUME outbox event not emitted"

            exec_row = (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == job_id)
                )
            ).scalar_one()
            assert exec_row.status == "RUNNING", f"Expected RUNNING after completion reconciliation, got {exec_row.status}"
            print(f" -> Verified: Execution transitioned to RUNNING, Outbox EXECUTION_RESUME emitted ({resume_event.id})")

        # ----------------------------------------------------------------------
        # STEP 5: EXECUTION_RESUME Consumer -> Actual Graph Continuation -> SUCCEEDED
        # ----------------------------------------------------------------------
        print("\n[Step 5] Processing EXECUTION_RESUME through Graph Runner Consumer...")
        resume_sqs_event = {
            "Records": [
                {
                    "messageId": f"msg-resume-{job_id}",
                    "body": json.dumps(
                        {
                            "id": str(resume_event.id),
                            "kind": "EXECUTION_RESUME",
                            "execution_id": str(job_id),
                            "task_id": str(dispatched_task.id),
                            "context_version": 1,
                        }
                    ),
                }
            ]
        }

        resume_failures = await runner_process_batch(resume_sqs_event, None, runner=runner)
        assert resume_failures == [], f"Graph runner resume failed: {resume_failures}"

        async with session_factory() as session:
            exec_row = (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == job_id)
                )
            ).scalar_one()
            assert exec_row.status == "SUCCEEDED", f"Expected SUCCEEDED, got {exec_row.status}"

            terminal_outbox = (
                await session.execute(
                    select(OutboxEventModel).where(
                        OutboxEventModel.execution_id == job_id,
                        OutboxEventModel.kind == "EXECUTION_SUCCEEDED",
                    )
                )
            ).scalar_one_or_none()
            assert terminal_outbox is not None, "EXECUTION_SUCCEEDED outbox event not emitted"
            print(" -> Verified: Execution reached terminal SUCCEEDED, Outbox EXECUTION_SUCCEEDED emitted")

        # ----------------------------------------------------------------------
        # STEP 6: Terminal Job Status and Timeline Query via HTTP API
        # ----------------------------------------------------------------------
        print("\n[Step 6] Verifying terminal job status and timeline via GET /api/v1/jobs/{job_id}...")
        status_resp = await client.get(f"/api/v1/jobs/{job_id}")
        assert status_resp.status_code == 200, f"Failed job status query: {status_resp.text}"
        status_payload = status_resp.json()

        print(f" -> API Job Status: {status_payload['status']}")
        assert status_payload["status"].upper() == "SUCCEEDED", f"Expected 'SUCCEEDED', got {status_payload['status']}"
        assert status_payload["progress"]["tasks_total"] == 1
        assert status_payload["progress"]["tasks_completed"] == 1
        assert status_payload["progress"]["tasks_failed"] == 0

        timeline = status_payload.get("timeline", [])
        event_types = [event["event_type"] for event in timeline]
        print(f" -> Chronological Timeline Events ({len(timeline)} events):")
        for e in timeline:
            print(f"    - Seq #{e['sequence']}: {e['event_type']} @ {e['timestamp']}")

        # Verify key timeline milestones are present
        assert "JOB_ACCEPTED" in event_types, "JOB_ACCEPTED missing from timeline"
        assert "TASKS_DISPATCHED" in event_types, "TASKS_DISPATCHED missing from timeline"
        assert "GRAPH_RESUMED" in event_types, "GRAPH_RESUMED missing from timeline"
        assert "EXECUTION_RESUMING" in event_types, "EXECUTION_RESUMING missing from timeline"
        assert "EXECUTION_SUCCEEDED" in event_types, "EXECUTION_SUCCEEDED missing from timeline"

        print("\n" + "=" * 70)
        print("SUCCESS: Full connected E2E trace verified through EXECUTION_RESUME,")
        print("actual graph continuation, terminal SUCCEEDED status, and complete timeline.")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
