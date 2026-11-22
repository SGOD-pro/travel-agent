"""Live backend journey evaluation runner conforming to /check verify requirements.

Executes the real SWENA backend application end-to-end without frontend:
1. Validates real external providers (Bedrock, SerpAPI, Scrapling, Valhalla, Open-Meteo, Obscura, LAYA).
2. Starts FastAPI application on 127.0.0.1:8000 connected to isolated PostgreSQL acceptance_db.
3. Runs local outbox relay, real worker execution, completion reconciliation, and task recovery.
4. Submits authenticated fresh trip brief for Bengaluru -> Mysuru -> Madikeri via API.
5. Polls jobs, itinerary, weather, and route map endpoints.
6. Audits database state (executions, tasks, claims, source_observations, routes, laya_decisions).
7. Emits structured report to backend/scripts/live_journey_result.json.
"""

from __future__ import annotations

# ruff: noqa: E402
import asyncio
import copy
import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Set environment before any internal imports
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/acceptance_db"
os.environ["ACCEPTANCE_DATABASE_URL"] = "postgres://postgres:postgres@127.0.0.1:5432/acceptance_db"
os.environ["AWS_PROFILE"] = "aws"
os.environ["VALHALLA_ENDPOINT_URL"] = "https://valhalla1.openstreetmap.de"

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import config.db as db_module
from acceptance_keys import TEST_PUBLIC_KEY_PEM, generate_jwt
from config.config import settings

# Configure application settings for acceptance execution
settings.DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/acceptance_db"
settings.ACCEPTANCE_DATABASE_URL = "postgres://postgres:postgres@127.0.0.1:5432/acceptance_db"
settings.AUTH_PUBLIC_KEY = TEST_PUBLIC_KEY_PEM
settings.VALHALLA_ENDPOINT_URL = "https://valhalla1.openstreetmap.de"
settings.BEDROCK_MODEL_ID = "apac.amazon.nova-lite-v1:0"
settings.AWS_REGION = "ap-south-1"
settings.LAYA_ENDPOINT_URL = ""
os.environ["LAYA_ENDPOINT_URL"] = ""

# Reset database engine singletons
db_module._engine = None
db_module._session_factory = None
session_factory = db_module.get_session_factory()

from functions.api.app import create_app
from functions.browser import consumer as browser_consumer
from functions.evidence_collector import consumer as evidence_collector_consumer
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer
from functions.orchestrator.execution.models import (
    ExecutionModel,
    JobEventModel,
    OutboxEventModel,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.execution.reconciliation import (
    CompletionReconciliationService,
)
from functions.orchestrator.execution.recovery import TaskRecoveryService
from functions.orchestrator.execution.runner import (
    ExecutionResumeEvent,
    ExecutionStartEvent,
    GraphRunnerService,
)
from functions.orchestrator.graphs.route_optimizer import (
    compile_route_optimizer_workflow,
)
from functions.orchestrator.graphs.trip_planner import (
    compile_trip_planning_workflow,
)
from functions.route_optimizer import consumer as route_optimizer_consumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("live_backend_journey")


def redact_secrets(obj: Any) -> Any:
    """Recursively redacts API keys and sensitive tokens."""
    if isinstance(obj, dict):
        redacted = {}
        for k, v in obj.items():
            if any(secret in k.lower() for secret in ("key", "token", "secret", "password")):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_secrets(v)
        return redacted
    elif isinstance(obj, list):
        return [redact_secrets(item) for item in obj]
    return obj


async def probe_providers() -> dict[str, Any]:
    """Inspects live external provider readiness without printing raw credentials."""
    report: dict[str, Any] = {}

    # 1. PostgreSQL acceptance_db
    try:
        async with session_factory() as session:
            res = await session.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            )
            table_count = res.scalar()
            res_alembic = await session.execute(text("SELECT version_num FROM alembic_version"))
            alembic_rev = res_alembic.scalar()
            report["postgresql"] = {
                "readiness": "PASS",
                "details": f"Connected to acceptance_db ({table_count} tables, alembic {alembic_rev})",
            }
    except Exception as exc:
        report["postgresql"] = {"readiness": "FAIL", "details": str(exc)}

    # 2. AWS Bedrock Nova-Lite
    try:
        import boto3

        session = boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "aws"), region_name="ap-south-1"
        )
        sts = session.client("sts")
        caller = sts.get_caller_identity()
        account_last4 = caller["Account"][-4:]
        bedrock = session.client("bedrock-runtime", region_name="ap-south-1")
        t0 = time.perf_counter()
        resp = bedrock.converse(
            modelId="apac.amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": "Reply with single word: READY"}]}],
            inferenceConfig={"maxTokens": 10, "temperature": 0.0},
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        reply = resp["output"]["message"]["content"][0]["text"].strip()
        report["bedrock"] = {
            "readiness": "PASS",
            "model_id": "apac.amazon.nova-lite-v1:0",
            "region": "ap-south-1",
            "account_ending": account_last4,
            "latency_ms": round(latency_ms, 1),
            "reply": reply,
        }
    except Exception as exc:
        report["bedrock"] = {"readiness": "FAIL", "details": str(exc)}

    # 3. SerpAPI
    try:
        serpapi_key = os.environ.get("SERPAPI_API_KEY") or getattr(settings, "SERPAPI_API_KEY", "")
        if not serpapi_key:
            report["serpapi"] = {
                "readiness": "FAIL",
                "details": "SERPAPI_API_KEY missing from environment",
            }
        else:
            t0 = time.perf_counter()
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(
                    "https://serpapi.com/search",
                    params={"engine": "google_maps", "q": "Bengaluru", "api_key": serpapi_key},
                )
                latency_ms = (time.perf_counter() - t0) * 1000.0
                if res.status_code == 200:
                    report["serpapi"] = {
                        "readiness": "PASS",
                        "status_code": res.status_code,
                        "latency_ms": round(latency_ms, 1),
                    }
                else:
                    report["serpapi"] = {
                        "readiness": "FAIL",
                        "status_code": res.status_code,
                        "details": res.text[:200],
                    }
    except Exception as exc:
        report["serpapi"] = {"readiness": "FAIL", "details": str(exc)}

    # 4. Scrapling / Public HTTP
    try:
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            res = await client.get("https://karnatakatourism.org/")
            latency_ms = (time.perf_counter() - t0) * 1000.0
            report["scrapling_http"] = {
                "readiness": "PASS" if res.status_code == 200 else "DEGRADED",
                "status_code": res.status_code,
                "latency_ms": round(latency_ms, 1),
            }
    except Exception as exc:
        report["scrapling_http"] = {"readiness": "FAIL", "details": str(exc)}

    # 5. Valhalla
    try:
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get("https://valhalla1.openstreetmap.de/status")
            latency_ms = (time.perf_counter() - t0) * 1000.0
            report["valhalla"] = {
                "readiness": "PASS" if res.status_code == 200 else "FAIL",
                "status_code": res.status_code,
                "latency_ms": round(latency_ms, 1),
            }
    except Exception as exc:
        report["valhalla"] = {"readiness": "FAIL", "details": str(exc)}

    # 6. Open-Meteo
    try:
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": 12.97,
                    "longitude": 77.59,
                    "daily": "weather_code",
                    "timezone": "Asia/Kolkata",
                },
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            report["open_meteo"] = {
                "readiness": "PASS" if res.status_code == 200 else "FAIL",
                "status_code": res.status_code,
                "latency_ms": round(latency_ms, 1),
            }
    except Exception as exc:
        report["open_meteo"] = {"readiness": "FAIL", "details": str(exc)}

    # 7. Obscura Engine Binary
    bin_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "build", "bin", "obscura")
    )
    if os.path.exists(bin_path) and os.access(bin_path, os.X_OK):
        report["obscura"] = {
            "readiness": "PASS",
            "path": bin_path,
            "size_bytes": os.path.getsize(bin_path),
        }
    else:
        report["obscura"] = {
            "readiness": "FAIL",
            "path": bin_path,
            "details": "Binary missing or not executable",
        }

    # 8. Remote LAYA
    laya_url = getattr(settings, "LAYA_ENDPOINT_URL", "")
    if not laya_url:
        report["remote_laya"] = {
            "readiness": "DISABLED",
            "url": None,
            "details": "Remote LAYA explicitly kept off for this live verification run",
        }
    else:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.post(laya_url, json={"prompt": "ping"})
                report["remote_laya"] = {
                    "readiness": "PASS",
                    "url": laya_url,
                    "status_code": res.status_code,
                }
        except Exception as exc:
            report["remote_laya"] = {
                "readiness": "OFFLINE",
                "url": laya_url,
                "prerequisite": "Cloudflare tunnel on Kaggle (missed-catalog-tub-renewal.trycloudflare.com) is down or not resolving DNS",
                "details": str(exc),
            }

    return report


class LocalOutboxRelay:
    """Relays transactional outbox events to real workers and reconciles completions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        target_owner_id: uuid.UUID,
    ) -> None:
        self._session_factory = session_factory
        self._target_owner_id = target_owner_id
        self._running = False
        self._task: asyncio.Task[None] | None = None

        checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
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
            session_factory,
            default_graph=self._trip_graph,
            workflow_graphs=workflow_graphs,
        )
        self._reconciler = CompletionReconciliationService(
            session_factory,
            default_graph=self._trip_graph,
            workflow_graphs=workflow_graphs,
        )
        self._recovery = TaskRecoveryService(session_factory)

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
        last_recovery_time = time.monotonic()
        while self._running:
            try:
                # Periodic lease recovery every 5s
                now_mono = time.monotonic()
                if now_mono - last_recovery_time >= 5.0:
                    last_recovery_time = now_mono
                    try:
                        await self._recovery.recover_expired_leases(batch_size=10)
                    except Exception as exc:
                        logger.warning("Task recovery error: %s", exc)

                # Query pending outbox events scoped strictly to target_owner_id
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
                    res = await session.execute(stmt)
                    events = list(res.scalars().all())

                for evt in events:
                    kind = evt.kind
                    exec_id = evt.execution_id
                    payload = evt.payload_ref or evt.payload or {}
                    t_start = time.perf_counter()

                    logger.info(
                        "Outbox relay processing event %s (kind=%s, exec_id=%s)",
                        evt.id,
                        kind,
                        exec_id,
                    )

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
                            duration_ms = (time.perf_counter() - t_start) * 1000.0
                            self.events_processed.append(
                                {
                                    "event_id": str(evt.id),
                                    "kind": kind,
                                    "duration_ms": round(duration_ms, 2),
                                    "outcome": result.status,
                                    "tasks_dispatched": result.tasks_dispatched,
                                }
                            )

                        elif kind == "TASK_DISPATCH":
                            worker = str(payload.get("worker", "EVIDENCE"))
                            action = str(payload.get("action", "collect"))
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
                                # Live SerpAPI and Live Scrapling extraction
                                await evidence_collector_consumer.process_record(
                                    record,
                                    self._session_factory,
                                    adapter=None,
                                    extraction_adapter=None,
                                )
                            elif worker == "BROWSER":
                                # Live Obscura browser worker
                                await browser_consumer.process_record(
                                    record,
                                    self._session_factory,
                                    adapter=None,
                                )
                            elif worker in ("OPTIMIZER", "ROUTE_OPTIMIZER"):
                                # Live Valhalla and OR-Tools routing solver
                                await route_optimizer_consumer.process_record(
                                    record,
                                    self._session_factory,
                                    valhalla_client=None,
                                )
                            else:
                                logger.warning("Unknown worker %s for task %s", worker, task_id)

                            await self._mark_delivered(evt.id)
                            duration_ms = (time.perf_counter() - t_start) * 1000.0
                            self.events_processed.append(
                                {
                                    "event_id": str(evt.id),
                                    "kind": kind,
                                    "worker": worker,
                                    "action": action,
                                    "duration_ms": round(duration_ms, 2),
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
                            duration_ms = (time.perf_counter() - t_start) * 1000.0
                            self.events_processed.append(
                                {
                                    "event_id": str(evt.id),
                                    "kind": kind,
                                    "status": reconcile_res.status,
                                    "graph_resumed": getattr(reconcile_res, "graph_resumed", False),
                                    "duration_ms": round(duration_ms, 2),
                                }
                            )

                        elif kind == "EXECUTION_RESUME":
                            resume_evt = ExecutionResumeEvent(
                                execution_id=exec_id,
                                event_id=evt.id,
                                triggered_by_task_id=uuid.UUID(str(payload["triggered_by_task_id"]))
                                if payload.get("triggered_by_task_id")
                                else None,
                                action=payload.get("action", "resume_graph"),
                                context_version=int(payload.get("context_version", 1)),
                            )
                            resume_res = await self._runner.handle_execution_resume(
                                event=resume_evt
                            )
                            await self._mark_delivered(evt.id)
                            duration_ms = (time.perf_counter() - t_start) * 1000.0
                            self.events_processed.append(
                                {
                                    "event_id": str(evt.id),
                                    "kind": kind,
                                    "outcome": resume_res.status,
                                    "tasks_dispatched": resume_res.tasks_dispatched,
                                    "duration_ms": round(duration_ms, 2),
                                }
                            )

                        else:
                            # Informational or telemetry outbox events
                            await self._mark_delivered(evt.id)

                    except Exception as exc:
                        logger.exception(
                            "Error processing outbox event %s (%s): %s", evt.id, kind, exc
                        )
                        async with self._session_factory() as session:
                            stmt_fail = select(OutboxEventModel).where(
                                OutboxEventModel.id == evt.id
                            )
                            row = (await session.execute(stmt_fail)).scalar_one_or_none()
                            if row:
                                row.last_error = str(exc)
                                row.failed_at = datetime.now(UTC)
                                await session.commit()
                        self.events_processed.append(
                            {
                                "event_id": str(evt.id),
                                "kind": kind,
                                "error": str(exc),
                            }
                        )

            except asyncio.CancelledError:
                break
            except Exception as loop_exc:
                logger.error("Outbox relay loop exception: %s", loop_exc)

            await asyncio.sleep(0.3)


async def main() -> None:
    logger.info("=== Starting SWENA Real Backend Journey Verification ===")

    # 1. Provider Readiness
    provider_readiness = await probe_providers()
    logger.info("Provider Readiness: %s", json.dumps(provider_readiness, indent=2))

    # 2. Start FastAPI Server
    app = create_app()
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # Wait for server health
    api_ready = False
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=5.0) as client:
        for _ in range(30):
            try:
                r = await client.get("/health")
                if r.status_code == 200:
                    api_ready = True
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)

    if not api_ready:
        logger.error("FastAPI server failed to start on port 8000")
        server.should_exit = True
        await server_task
        return

    logger.info("FastAPI server is running and healthy on http://127.0.0.1:8000")

    # 3. Setup Target Owner and Start Outbox Relay
    owner_id = uuid.uuid4()
    auth_token = generate_jwt(
        sub=owner_id, email="swena_eval@travel-agent.local", name="Evaluation Traveler"
    )
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }

    relay = LocalOutboxRelay(session_factory, owner_id)
    relay_task = relay.start()
    logger.info("Local Outbox Relay started for owner_id=%s", owner_id)

    # 4. Submit Brief through authenticated API
    # Natural language brief requested:
    # "Plan a fresh two-day petrol-car trip for two adults from Mysuru through Srirangapatna to Mandya
    #  on 25–26 November 2026. Budget ₹12,000 total for one night of lodging, meals, fuel, and tolls.
    #  Prefer heritage, local Karnataka food, and daylight driving."
    # Dates in Asia/Kolkata: 2026-11-25 to 2026-11-26 (2 days)
    # Origin: Mysuru (12.2958, 76.6394)
    # Destinations: Srirangapatna (12.4218, 76.6932), Mandya (12.5230, 76.8970)
    create_trip_payload = {
        "owner_id": str(owner_id),
        "brief": {
            "origin_name": "Mysuru",
            "origin_lat": 12.2958,
            "origin_lng": 76.6394,
            "destinations": [
                {
                    "name": "Srirangapatna",
                    "latitude": 12.4218,
                    "longitude": 76.6932,
                    "stay_days": 1,
                    "confidence": 1.0,
                },
                {
                    "name": "Mandya",
                    "latitude": 12.5230,
                    "longitude": 76.8970,
                    "stay_days": 1,
                    "confidence": 1.0,
                },
            ],
            "start_date": "2026-11-25",
            "end_date": "2026-11-26",
            "timezone": "Asia/Kolkata",
            "adults": 2,
            "child_ages": [],
            "rooms": 1,
            "target_budget": {
                "amount": "12000.000000",
                "currency": "INR",
            },
            "preferred_modes": ["car_petrol"],
            "dietary_preferences": ["local Karnataka food"],
            "accessibility_requirements": [],
            "hard_constraints": ["heritage", "local Karnataka food", "daylight driving"],
        },
    }

    submitted_records: dict[str, Any] = {
        "create_trip_request": copy.deepcopy(create_trip_payload),
    }

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30.0) as client:
        # Step A: POST /api/v1/trips
        r_create = await client.post("/api/v1/trips", json=create_trip_payload, headers=headers)
        submitted_records["create_trip_response"] = {
            "status_code": r_create.status_code,
            "body": r_create.json(),
        }
        if r_create.status_code != 201:
            logger.error("Failed to create trip: %s", r_create.text)
            relay.stop()
            server.should_exit = True
            await asyncio.gather(relay_task, server_task, return_exceptions=True)
            return

        trip_data = r_create.json()
        trip_id = trip_data["id"]
        logger.info("Trip created: trip_id=%s, version=%s", trip_id, trip_data["current_version"])

        # Step B: POST /api/v1/trips/{trip_id}/plans
        idempotency_key = f"live-eval-{uuid.uuid4()}"
        start_plan_payload = {
            "workflow": "TRAVEL",
            "planning_options": {},
        }
        plan_headers = {
            **headers,
            "Idempotency-Key": idempotency_key,
        }
        submitted_records["start_plan_request"] = {
            "url": f"/api/v1/trips/{trip_id}/plans",
            "headers": {"Idempotency-Key": idempotency_key},
            "body": copy.deepcopy(start_plan_payload),
        }

        r_plan = await client.post(
            f"/api/v1/trips/{trip_id}/plans",
            json=start_plan_payload,
            headers=plan_headers,
        )
        submitted_records["start_plan_response"] = {
            "status_code": r_plan.status_code,
            "body": r_plan.json(),
        }
        if r_plan.status_code != 202:
            logger.error("Failed to start trip plan: %s", r_plan.text)
            relay.stop()
            server.should_exit = True
            await asyncio.gather(relay_task, server_task, return_exceptions=True)
            return

        plan_data = r_plan.json()
        job_id = plan_data["job_id"]
        logger.info("Plan accepted: job_id=%s, status=%s", job_id, plan_data["status"])

        # Step C: Poll GET /api/v1/jobs/{job_id} until terminal status
        terminal_states = {"SUCCEEDED", "FAILED", "PARTIAL", "CANCELLED"}
        job_status = plan_data["status"]
        polls: list[dict[str, Any]] = []
        poll_count = 0
        max_polls = 100  # 100 * 2s = 200 seconds maximum

        t_start_journey = time.perf_counter()
        while job_status not in terminal_states and poll_count < max_polls:
            await asyncio.sleep(2.0)
            poll_count += 1
            try:
                r_job = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
                if r_job.status_code == 200:
                    job_info = r_job.json()
                    job_status = job_info.get("status")
                    elapsed = round(time.perf_counter() - t_start_journey, 1)
                    logger.info(
                        "Job %s poll #%d: status=%s, progress=%s, elapsed=%ss",
                        job_id,
                        poll_count,
                        job_status,
                        job_info.get("progress"),
                        elapsed,
                    )
                    polls.append(
                        {
                            "poll": poll_count,
                            "elapsed_seconds": elapsed,
                            "status": job_status,
                            "progress": job_info.get("progress"),
                            "timeline_count": len(job_info.get("timeline", [])),
                            "error": job_info.get("error"),
                        }
                    )
                else:
                    logger.warning("Job poll status %d: %s", r_job.status_code, r_job.text)
            except Exception as poll_exc:
                logger.warning("Job poll error: %s", poll_exc)

        submitted_records["job_polls"] = polls

        # Step D: Retrieve Itinerary and Linked Surfaces
        itinerary_response: dict[str, Any] = {}
        weather_response: dict[str, Any] = {}
        map_response: dict[str, Any] = {}

        try:
            r_itin = await client.get(f"/api/v1/trips/{trip_id}/itinerary", headers=headers)
            itinerary_response = {
                "status_code": r_itin.status_code,
                "body": r_itin.json() if r_itin.status_code == 200 else r_itin.text,
            }
        except Exception as exc:
            itinerary_response = {"error": str(exc)}

        try:
            r_weath = await client.get(f"/api/v1/trips/{trip_id}/weather", headers=headers)
            weather_response = {
                "status_code": r_weath.status_code,
                "body": r_weath.json() if r_weath.status_code == 200 else r_weath.text,
            }
        except Exception as exc:
            weather_response = {"error": str(exc)}

        route_id = None
        if isinstance(itinerary_response.get("body"), dict):
            route_id = itinerary_response["body"].get("route_id")

        if route_id:
            try:
                r_map = await client.get(
                    f"/api/v1/optimization/routes/{route_id}/map", headers=headers
                )
                map_response = {
                    "status_code": r_map.status_code,
                    "body": r_map.json() if r_map.status_code == 200 else r_map.text,
                }
            except Exception as exc:
                map_response = {"error": str(exc)}

        submitted_records["itinerary_endpoint"] = itinerary_response
        submitted_records["weather_endpoint"] = weather_response
        submitted_records["map_endpoint"] = map_response

    # 5. Audit Database State
    db_audit: dict[str, Any] = {}
    async with session_factory() as session:
        # Execution
        exec_row = (
            await session.execute(
                select(ExecutionModel).where(ExecutionModel.id == uuid.UUID(job_id))
            )
        ).scalar_one_or_none()
        if exec_row:
            db_audit["execution"] = {
                "id": str(exec_row.id),
                "status": exec_row.status,
                "workflow": exec_row.workflow,
                "context_version": exec_row.context_version,
                "budget": exec_row.budget,
                "error": getattr(exec_row, "error", None),
                "created_at": exec_row.created_at.isoformat() if exec_row.created_at else None,
                "updated_at": exec_row.updated_at.isoformat() if exec_row.updated_at else None,
            }

        # Tasks
        task_rows = (
            (
                await session.execute(
                    select(TaskModel)
                    .where(TaskModel.execution_id == uuid.UUID(job_id))
                    .order_by(TaskModel.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        db_audit["tasks"] = [
            {
                "id": str(t.id),
                "worker": t.worker,
                "action": t.action,
                "status": t.status,
                "attempt": t.attempt,
                "payload_summary": {
                    "stops_count": len((t.payload_ref or {}).get("stops", []))
                    if isinstance(t.payload_ref, dict) and "stops" in t.payload_ref
                    else None,
                    "stops": [
                        s.get("name")
                        for s in (t.payload_ref or {}).get("stops", [])
                        if isinstance(s, dict)
                    ]
                    if isinstance(t.payload_ref, dict) and "stops" in t.payload_ref
                    else None,
                }
                if t.payload_ref
                else None,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
            for t in task_rows
        ]

        # Worker Results
        task_ids = [t.id for t in task_rows]
        if task_ids:
            wr_rows = (
                (
                    await session.execute(
                        select(WorkerResultModel).where(WorkerResultModel.task_id.in_(task_ids))
                    )
                )
                .scalars()
                .all()
            )
            db_audit["worker_results"] = [
                {
                    "id": str(wr.id),
                    "task_id": str(wr.task_id),
                    "status": wr.status,
                    "output_summary": {
                        k: (len(v) if isinstance(v, (list, dict)) else v)
                        for k, v in (wr.output_ref or {}).items()
                    }
                    if wr.output_ref
                    else None,
                    "created_at": wr.finished_at.isoformat() if getattr(wr, "finished_at", None) else None,
                }
                for wr in wr_rows
            ]

        # Source Observations
        obs_res = await session.execute(
            text(
                "SELECT extraction_method, count(*) FROM source_observations WHERE execution_id = :eid GROUP BY extraction_method"
            ),
            {"eid": uuid.UUID(job_id)},
        )
        db_audit["source_observations_by_extraction_method"] = {row[0]: row[1] for row in obs_res.all()}

        # Claims
        claims_res = await session.execute(
            text(
                "SELECT category, verification, count(*) FROM claims WHERE execution_id = :eid GROUP BY category, verification"
            ),
            {"eid": uuid.UUID(job_id)},
        )
        db_audit["claims_by_category_verification"] = [
            {"category": row[0], "verification": row[1], "count": row[2]} for row in claims_res.all()
        ]

        # LAYA Decisions
        laya_res = await session.execute(
            text(
                "SELECT decision_type, chosen_action, status, model_version, count(*) FROM laya_decisions WHERE execution_id = :eid GROUP BY decision_type, chosen_action, status, model_version"
            ),
            {"eid": uuid.UUID(job_id)},
        )
        db_audit["laya_decisions"] = [
            {
                "decision_type": row[0],
                "chosen_action": row[1],
                "status": row[2],
                "model_version": row[3],
                "count": row[4],
            }
            for row in laya_res.all()
        ]

        # Routes
        route_res = await session.execute(
            text(
                "SELECT id, total_distance_m, total_travel_seconds, fuel_amount, status, solver_status FROM routes WHERE execution_id = :eid"
            ),
            {"eid": uuid.UUID(job_id)},
        )
        routes_list = []
        for r_row in route_res.all():
            r_id = r_row[0]
            stops_res = await session.execute(
                text(
                    "SELECT stop_order, name, stop_type, planned_arrival_at, planned_departure_at, dwell_seconds, wait_seconds FROM route_stops WHERE route_id = :rid ORDER BY stop_order"
                ),
                {"rid": r_id},
            )
            route_stops = [
                {
                    "stop_order": stop_row[0],
                    "name": stop_row[1],
                    "stop_type": stop_row[2],
                    "planned_arrival_at": stop_row[3].isoformat() if stop_row[3] else None,
                    "planned_departure_at": stop_row[4].isoformat() if stop_row[4] else None,
                    "dwell_seconds": stop_row[5],
                    "wait_seconds": stop_row[6],
                }
                for stop_row in stops_res.all()
            ]
            legs_res = await session.execute(
                text(
                    "SELECT leg_order, transport_mode, distance_m, travel_seconds, provider FROM route_legs WHERE route_id = :rid ORDER BY leg_order"
                ),
                {"rid": r_id},
            )
            route_legs = [
                {
                    "leg_order": leg_row[0],
                    "transport_mode": leg_row[1],
                    "distance_m": leg_row[2],
                    "travel_seconds": leg_row[3],
                    "provider": leg_row[4],
                }
                for leg_row in legs_res.all()
            ]
            routes_list.append(
                {
                    "id": str(r_id),
                    "total_distance_m": r_row[1],
                    "total_travel_seconds": r_row[2],
                    "fuel_amount": str(r_row[3]) if r_row[3] is not None else None,
                    "status": r_row[4],
                    "solver_status": r_row[5],
                    "stops": route_stops,
                    "legs": route_legs,
                }
            )
        db_audit["routes"] = routes_list

        # Job Events timeline
        events_rows = (
            (
                await session.execute(
                    select(JobEventModel)
                    .where(JobEventModel.execution_id == uuid.UUID(job_id))
                    .order_by(JobEventModel.sequence.asc())
                )
            )
            .scalars()
            .all()
        )
        db_audit["job_events_timeline"] = [
            {
                "sequence": ev.sequence,
                "event_type": ev.event_type,
                "timestamp": ev.timestamp.isoformat() if ev.timestamp else None,
                "payload": ev.safe_payload,
            }
            for ev in events_rows
        ]

    # Stop background tasks
    relay.stop()
    server.should_exit = True
    await asyncio.gather(relay_task, server_task, return_exceptions=True)

    # 6. Save Complete Structured Results
    final_output = {
        "execution_date": date.today().isoformat(),
        "provider_readiness": provider_readiness,
        "outbox_events_processed": relay.events_processed,
        "api_journey": submitted_records,
        "database_audit": db_audit,
    }

    out_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "live_journey_result.json"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(redact_secrets(final_output), f, indent=2)

    logger.info("Saved complete journey results to %s", out_path)
    logger.info("Journey final execution status: %s", db_audit.get("execution", {}).get("status"))


if __name__ == "__main__":
    asyncio.run(main())
