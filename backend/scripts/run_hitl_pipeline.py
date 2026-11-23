"""Direct Python HITL Pipeline Runner conforming to spec 0007.

Executes the real SWENA backend pipeline directly from Python:
1. Validates safe local acceptance database guard to prevent accidental production execution.
2. Ingests an incomplete brief missing travel dates.
3. Submits through authenticated API endpoints and pumps the transactional outbox relay.
4. Pauses safely when WAITING_USER is reached, displaying question prompt and validation rules.
5. Accepts traveler answers interactively or via non interactive automated flags.
6. Submits answer via POST /api/v1/jobs/{job_id}/responses to advance context and schedule resume.
7. Supports process restart and resumption via --resume <job_id>.
8. Prints progress events, final itinerary, and unresolved uncertainties.
9. Persists a distinct JSON artifact of the run.
"""

from __future__ import annotations

# ruff: noqa: E402
import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Enforce local acceptance database configuration before any imports
DEFAULT_ACCEPTANCE_DB = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/acceptance_db"
os.environ.setdefault("DATABASE_URL", DEFAULT_ACCEPTANCE_DB)
os.environ.setdefault("ACCEPTANCE_DATABASE_URL", "postgres://postgres:postgres@127.0.0.1:5432/acceptance_db")
os.environ.setdefault("AWS_PROFILE", "aws")
os.environ.setdefault("VALHALLA_ENDPOINT_URL", "https://valhalla1.openstreetmap.de")
os.environ["LAYA_ENDPOINT_URL"] = ""

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import config.db as db_module
from acceptance_keys import TEST_PUBLIC_KEY_PEM, generate_jwt
from config.config import settings

settings.DATABASE_URL = os.environ["DATABASE_URL"]
settings.ACCEPTANCE_DATABASE_URL = os.environ["ACCEPTANCE_DATABASE_URL"]
settings.AUTH_PUBLIC_KEY = TEST_PUBLIC_KEY_PEM
settings.VALHALLA_ENDPOINT_URL = os.environ["VALHALLA_ENDPOINT_URL"]
settings.BEDROCK_MODEL_ID = "apac.amazon.nova-lite-v1:0"
settings.AWS_REGION = "ap-south-1"
settings.LAYA_ENDPOINT_URL = ""

db_module._engine = None
db_module._session_factory = None

from functions.api.app import create_app
from functions.browser import consumer as browser_consumer
from functions.evidence_collector import consumer as evidence_collector_consumer
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.models import (
    ExecutionModel,
    OutboxEventModel,
)
from functions.orchestrator.execution.reconciliation import CompletionReconciliationService
from functions.orchestrator.execution.recovery import TaskRecoveryService
from functions.orchestrator.execution.runner import (
    ExecutionResumeEvent,
    ExecutionStartEvent,
    GraphRunnerService,
)
from functions.orchestrator.graphs.route_optimizer import compile_route_optimizer_workflow
from functions.orchestrator.graphs.trip_planner import compile_trip_planning_workflow
from functions.route_optimizer import consumer as route_optimizer_consumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("hitl_pipeline_runner")


def enforce_safe_database(url: str) -> None:
    """Refuses execution against unapproved or potential production databases."""
    lower_url = url.lower()
    allowed_hosts = ("127.0.0.1", "localhost", "test")
    is_safe_host = any(h in lower_url for h in allowed_hosts)
    is_safe_db = "acceptance_db" in lower_url or "test" in lower_url
    if not (is_safe_host and is_safe_db):
        sys.stderr.write(
            f"REFUSAL: Target database {url} is not an approved acceptance or test database.\n"
            "Execution is restricted strictly to local acceptance environments.\n"
        )
        sys.exit(1)


class LocalOutboxRelay:
    """Pumps transactional outbox events to live workers and orchestrator services."""

    def __init__(
        self,
        target_session_factory: async_sessionmaker[AsyncSession],
        target_owner_id: uuid.UUID,
    ) -> None:
        self._session_factory = target_session_factory
        self._target_owner_id = target_owner_id
        self._running = False
        self._task: asyncio.Task[None] | None = None

        checkpointer = TransactionBoundCheckpointer(session_factory=target_session_factory)
        self._trip_graph = compile_trip_planning_workflow(checkpointer=checkpointer)
        self._route_graph = compile_route_optimizer_workflow(checkpointer=checkpointer)

        workflow_graphs = {
            "TRAVEL": self._trip_graph,
            "TRIP_PLANNER": self._trip_graph,
            "TRIP_PLANNER_GRAPH": self._trip_graph,
            "PLANNING": self._trip_graph,
            "ROUTE_OPTIMIZER_GRAPH": self._route_graph,
        }

        self._runner = GraphRunnerService(
            target_session_factory,
            default_graph=self._trip_graph,
            workflow_graphs=workflow_graphs,
        )
        self._reconciler = CompletionReconciliationService(
            target_session_factory,
            default_graph=self._trip_graph,
            workflow_graphs=workflow_graphs,
        )
        self._recovery = TaskRecoveryService(target_session_factory)
        self.events_processed: list[dict[str, Any]] = []

    def start(self) -> asyncio.Task[None]:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        return self._task

    def stop(self) -> None:
        self._running = False

    async def _mark_delivered(self, event_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            stmt = select(OutboxEventModel).where(OutboxEventModel.id == event_id)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row:
                row.delivered_at = datetime.now(UTC)
                await session.commit()

    async def _run_loop(self) -> None:
        last_recovery = time.monotonic()
        while self._running:
            try:
                now_mono = time.monotonic()
                if now_mono - last_recovery >= 5.0:
                    last_recovery = now_mono
                    try:
                        await self._recovery.recover_expired_leases(batch_size=10)
                    except Exception as rec_err:
                        logger.warning("Recovery warning: %s", rec_err)

                async with self._session_factory() as session:
                    stmt = (
                        select(OutboxEventModel)
                        .join(ExecutionModel, OutboxEventModel.execution_id == ExecutionModel.id)
                        .where(
                            OutboxEventModel.delivered_at.is_(None),
                            OutboxEventModel.failed_at.is_(None),
                            ExecutionModel.owner_id == self._target_owner_id,
                        )
                        .order_by(OutboxEventModel.created_at.asc())
                        .limit(10)
                    )
                    events = list((await session.execute(stmt)).scalars().all())

                for evt in events:
                    kind = evt.kind
                    exec_id = evt.execution_id
                    payload = evt.payload_ref or evt.payload or {}
                    t0 = time.perf_counter()

                    try:
                        if kind == "EXECUTION_START":
                            start_evt = ExecutionStartEvent(
                                execution_id=exec_id,
                                event_id=evt.id,
                                workflow=payload.get("workflow", "TRAVEL"),
                                context_version=payload.get("context_version", 1),
                                payload=payload,
                            )
                            result = await self._runner.handle_execution_start(event=start_evt)
                            await self._mark_delivered(evt.id)
                            self.events_processed.append(
                                {
                                    "kind": kind,
                                    "status": result.status,
                                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                                }
                            )

                        elif kind == "TASK_DISPATCH":
                            worker = str(payload.get("worker", "EVIDENCE"))
                            task_id = evt.task_id or payload.get("task_id")
                            record = {
                                "kind": "TASK_DISPATCH",
                                "event_id": str(evt.id),
                                "task_id": str(task_id),
                                "execution_id": str(exec_id),
                                "payload": payload,
                                "payload_ref": evt.payload_ref or payload,
                                "attempt": payload.get("attempt", 0),
                                "context_version": payload.get("context_version", 1),
                            }
                            if worker == "EVIDENCE":
                                await evidence_collector_consumer.process_record(
                                    record, self._session_factory
                                )
                            elif worker == "BROWSER":
                                await browser_consumer.process_record(
                                    record, self._session_factory
                                )
                            elif worker in ("OPTIMIZER", "ROUTE_OPTIMIZER"):
                                await route_optimizer_consumer.process_record(
                                    record, self._session_factory
                                )
                            await self._mark_delivered(evt.id)
                            self.events_processed.append(
                                {
                                    "kind": kind,
                                    "worker": worker,
                                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                                }
                            )

                        elif kind in ("TASK_COMPLETED", "TASK_FAILED"):
                            reconcile_dict = {
                                "event_id": str(evt.id),
                                "task_id": str(evt.task_id),
                                "execution_id": str(exec_id),
                                "attempt": int(payload.get("attempt", 1)),
                                "event_type": kind,
                                "result_id": payload.get("result_id"),
                                "output": payload.get("output"),
                                "error": payload.get("error"),
                            }
                            reconcile_res = await self._reconciler.reconcile_task_event(
                                event=reconcile_dict
                            )
                            await self._mark_delivered(evt.id)
                            self.events_processed.append(
                                {
                                    "kind": kind,
                                    "status": reconcile_res.status,
                                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                                }
                            )

                        elif kind == "EXECUTION_RESUME":
                            resume_evt = ExecutionResumeEvent(
                                execution_id=exec_id,
                                event_id=evt.id,
                                triggered_by_task_id=(
                                    uuid.UUID(str(payload["triggered_by_task_id"]))
                                    if payload.get("triggered_by_task_id")
                                    else None
                                ),
                                action=payload.get("action", "resume_graph"),
                                context_version=int(payload.get("context_version", 1)),
                            )
                            resume_res = await self._runner.handle_execution_resume(
                                event=resume_evt
                            )
                            await self._mark_delivered(evt.id)
                            self.events_processed.append(
                                {
                                    "kind": kind,
                                    "status": resume_res.status,
                                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                                }
                            )

                        else:
                            await self._mark_delivered(evt.id)

                    except Exception as err:
                        logger.exception("Error processing outbox event %s (%s): %s", evt.id, kind, err)
                        async with self._session_factory() as session:
                            stmt_f = select(OutboxEventModel).where(OutboxEventModel.id == evt.id)
                            row_f = (await session.execute(stmt_f)).scalar_one_or_none()
                            if row_f:
                                row_f.last_error = str(err)
                                row_f.failed_at = datetime.now(UTC)
                                await session.commit()
            except asyncio.CancelledError:
                break
            except Exception as loop_err:
                logger.error("Outbox loop error: %s", loop_err)

            await asyncio.sleep(0.3)


async def run_pipeline(
    *,
    resume_job_id: uuid.UUID | None = None,
    interactive: bool = True,
    start_date_str: str = "2026-11-25",
    end_date_str: str = "2026-11-26",
    stop_at_question: bool = False,
) -> dict[str, Any]:
    """Runs the HITL pipeline through question presentation, answer submission, and completion."""
    enforce_safe_database(settings.DATABASE_URL)
    session_factory = db_module.get_session_factory()

    job_id: uuid.UUID
    if resume_job_id is not None:
        job_id = resume_job_id
        print(f"\n[HITL Runner] Reopening existing execution: {job_id}")
        async with session_factory() as session:
            exec_row = (
                await session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == job_id)
                )
            ).scalar_one_or_none()
            if exec_row is None:
                raise RuntimeError(f"Execution {job_id} not found in database")
            user_id = exec_row.owner_id
    else:
        user_id = uuid.uuid4()

    auth_token = generate_jwt(sub=str(user_id), email="traveler.hitl@example.com")
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }

    app = create_app()
    transport = httpx.ASGITransport(app=app)

    relay = LocalOutboxRelay(session_factory, target_owner_id=user_id)
    relay_task = relay.start()

    run_summary: dict[str, Any] = {
        "runner": "run_hitl_pipeline.py",
        "started_at": datetime.now(UTC).isoformat(),
        "database_url": settings.DATABASE_URL,
        "resume_mode": resume_job_id is not None,
    }

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=60.0) as client:
            if resume_job_id is None:
                # 1. Ingest incomplete brief
                incomplete_brief = {
                    "title": "Mysuru Heritage and Cultural Journey",
                    "origin_name": "Mysuru, Karnataka",
                    "origin_lat": 12.2958,
                    "origin_lng": 76.6394,
                    "destinations": [
                        {
                            "name": "Srirangapatna, Karnataka",
                            "latitude": 12.4218,
                            "longitude": 76.6932,
                            "stay_days": 1,
                            "confidence": 0.95,
                        },
                        {
                            "name": "Mandya, Karnataka",
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
                    "traveler_preferences": ["heritage", "local Karnataka food", "daylight driving"],
                    "dietary_preferences": ["local vegetarian"],
                    "accessibility_requirements": [],
                }

                print("\n[HITL Runner] Submitting incomplete brief (dates omitted)...")
                trip_create_res = await client.post(
                    "/api/v1/trips",
                    headers=headers,
                    json={"owner_id": str(user_id), "brief": incomplete_brief},
                )
                if trip_create_res.status_code not in (200, 201):
                    raise RuntimeError(f"Failed to create trip: {trip_create_res.text}")
                trip_id = uuid.UUID(trip_create_res.json()["id"])
                print(f"[HITL Runner] Trip created: {trip_id}")

                # Start trip plan
                start_headers = dict(headers)
                start_headers["Idempotency-Key"] = f"hitl_start_{trip_id}"
                plan_start_res = await client.post(
                    f"/api/v1/trips/{trip_id}/plans",
                    headers=start_headers,
                    json={"workflow": "TRAVEL"},
                )
                if plan_start_res.status_code != 202:
                    raise RuntimeError(f"Failed to start plan: {plan_start_res.text}")
                job_id = uuid.UUID(plan_start_res.json()["job_id"])
                print(f"[HITL Runner] Job started: {job_id}")

            run_summary["job_id"] = str(job_id)

            # 2. Poll until WAITING_USER or terminal
            print("\n[HITL Runner] Polling execution status...")
            pending_question: dict[str, Any] | None = None
            for _ in range(60):
                status_res = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
                if status_res.status_code != 200:
                    raise RuntimeError(f"Failed to fetch job status: {status_res.text}")
                status_data = status_res.json()
                current_status = status_data["status"]
                print(f"  Current status: {current_status}")

                if current_status == "WAITING_USER":
                    questions = status_data.get("pending_questions", [])
                    if questions:
                        pending_question = questions[0]
                        break
                elif current_status in ("SUCCEEDED", "FAILED", "CANCELLED"):
                    break

                await asyncio.sleep(1.0)

            if not pending_question and current_status not in ("WAITING_USER", "SUCCEEDED", "PARTIAL"):
                raise RuntimeError(
                    f"Job reached status {current_status} without presenting pending question"
                )

            if pending_question:
                print("\n" + "=" * 60)
                print("[HUMAN IN THE LOOP QUESTION REQUIRED]")
                print("=" * 60)
                q_id = uuid.UUID(pending_question["id"])
                q_key = pending_question["question_key"]
                q_type = pending_question["question_type"]
                prompt_text = pending_question["prompt"]
                rules = pending_question.get("validation_rules", {})

                print(f"Question Key:      {q_key}")
                print(f"Question Type:     {q_type}")
                print(f"Prompt:            {prompt_text}")
                print(f"Validation Rules:  {json.dumps(rules)}")
                print("=" * 60)

                run_summary["question_presented"] = {
                    "id": str(q_id),
                    "key": q_key,
                    "type": q_type,
                    "prompt": prompt_text,
                    "rules": rules,
                }

                if stop_at_question:
                    print("\n[HITL Runner] --stop-at-question requested. Execution paused safely.")
                    print(f"To resume this execution, run:\n  uv run python backend/scripts/run_hitl_pipeline.py --resume {job_id}")
                    run_summary["stopped_at_question"] = True
                    return run_summary

                # 3. Read traveler answer
                answer_payload: dict[str, Any]
                if interactive:
                    if not sys.stdin.isatty():
                        print(f"\n[Non-Interactive Stream] Defaulting to dates: {start_date_str} to {end_date_str}")
                        answer_payload = {"start_date": start_date_str, "end_date": end_date_str}
                    else:
                        print("\nPlease supply the missing travel dates below:")
                        val_start = (await asyncio.to_thread(input, f"Enter start date (YYYY-MM-DD) [{start_date_str}]: ")).strip()
                        if not val_start:
                            val_start = start_date_str
                        val_end = (await asyncio.to_thread(input, f"Enter end date (YYYY-MM-DD) [{end_date_str}]: ")).strip()
                        if not val_end:
                            val_end = end_date_str
                        answer_payload = {"start_date": val_start, "end_date": val_end}
                else:
                    print(f"\n[Automated Answer Mode] Using dates: {start_date_str} to {end_date_str}")
                    answer_payload = {"start_date": start_date_str, "end_date": end_date_str}

                run_summary["submitted_answer"] = answer_payload

                # 4. Submit answer through API
                print(f"\n[HITL Runner] Submitting answer for question {q_id}...")
                resp_res = await client.post(
                    f"/api/v1/jobs/{job_id}/responses",
                    headers=headers,
                    json={
                        "question_id": str(q_id),
                        "answer": answer_payload,
                        "expected_context_version": pending_question.get("context_version", 1),
                    },
                )
                if resp_res.status_code not in (200, 201):
                    raise RuntimeError(f"Failed to submit response: {resp_res.text}")

                sub_result = resp_res.json()
                print(f"[HITL Runner] Response accepted. Status: {sub_result['status']}. Context version: {sub_result['context_version']}")
                run_summary["answer_result"] = sub_result
            else:
                print(f"\n[HITL Runner] Job {job_id} already has no pending questions (status: {current_status}).")

            # 5. Follow execution to completion
            print("\n[HITL Runner] Waiting for resumed execution to finish...")
            final_job_data: dict[str, Any] = {}
            for _ in range(120):
                st_res = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
                if st_res.status_code == 200:
                    final_job_data = st_res.json()
                    st = final_job_data["status"]
                    print(f"  Execution status: {st}")
                    if st in ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"):
                        break
                await asyncio.sleep(1.0)

            run_summary["final_status"] = final_job_data.get("status")
            run_summary["timeline"] = final_job_data.get("timeline", [])

            # 6. Fetch proposal and audit database
            async with session_factory() as session:
                exec_row = (
                    await session.execute(
                        select(ExecutionModel).where(ExecutionModel.id == job_id)
                    )
                ).scalar_one_or_none()
                trip_db_id = exec_row.trip_id if exec_row else None

                if trip_db_id:
                    from functions.api.models import TripModel

                    trip_row = (
                        await session.execute(
                            select(TripModel).where(TripModel.id == trip_db_id)
                        )
                    ).scalar_one_or_none()
                    if trip_row and trip_row.itinerary_json:
                        itinerary = trip_row.itinerary_json
                        run_summary["itinerary"] = itinerary
                        print("\n" + "=" * 60)
                        print("[FINAL ITINERARY SUMMARY]")
                        print("=" * 60)
                        print(f"Trip ID:        {trip_row.id}")
                        print(f"Version:        {trip_row.current_version}")
                        dest_title = (
                            itinerary.get("destination")
                            or itinerary.get("title")
                            or "Mysuru to Mandya Heritage Journey"
                        )
                        print(f"Destination:    {dest_title}")
                        schedule_val = itinerary.get("schedule", {})
                        if isinstance(schedule_val, dict):
                            stops_list = schedule_val.get("stops", [])
                        elif isinstance(schedule_val, list):
                            stops_list = schedule_val
                        else:
                            stops_list = []
                        print(f"Total Stops:    {len(stops_list)}")
                        for idx, stop in enumerate(stops_list, 1):
                            if isinstance(stop, dict):
                                name = stop.get("name") or stop.get("place_name") or "Unknown"
                                arr = stop.get("arrival_time") or stop.get("arrival") or "N/A"
                                dep = stop.get("departure_time") or stop.get("departure") or "N/A"
                            else:
                                name = str(stop)
                                arr = "N/A"
                                dep = "N/A"
                            print(f"  {idx}. {name} (Arrival: {arr}, Departure: {dep})")
                        budget = itinerary.get("budget_summary") or itinerary.get("budget", {})
                        print(f"Total Budget:   {budget.get('total_cost_inr', 'N/A')}")
                        uncertainties = itinerary.get("uncertainties", [])
                        print(f"Uncertainties:  {len(uncertainties)} recorded")
                        for u in uncertainties:
                            print(f"  * {u}")
                        print("=" * 60)

    finally:
        relay.stop()
        await relay_task

    run_summary["completed_at"] = datetime.now(UTC).isoformat()
    return run_summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Direct Python HITL Pipeline Runner")
    parser.add_argument("--resume", type=str, default=None, help="Job ID of paused execution to reopen and resume")
    parser.add_argument("--non-interactive", action="store_true", help="Run in non interactive mode with preset answers")
    parser.add_argument("--start-date", type=str, default="2026-11-25", help="Start date YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, default="2026-11-26", help="End date YYYY-MM-DD")
    parser.add_argument("--stop-at-question", action="store_true", help="Pause and exit upon reaching WAITING_USER")
    parser.add_argument("--output", type=str, default=None, help="Output JSON artifact path")
    args = parser.parse_args()

    resume_id = uuid.UUID(args.resume) if args.resume else None
    interactive = not args.non_interactive

    result = asyncio.run(
        run_pipeline(
            resume_job_id=resume_id,
            interactive=interactive,
            start_date_str=args.start_date,
            end_date_str=args.end_date,
            stop_at_question=args.stop_at_question,
        )
    )

    out_path = args.output
    if not out_path:
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        out_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), f"hitl_pipeline_result_{ts}.json")
        )

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"\n[HITL Runner] Artifact saved to: {out_path}")


if __name__ == "__main__":
    main()
