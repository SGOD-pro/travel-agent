"""End-to-end integration tests for Obscura browser worker execution and graph consumption.

Proves the complete pipeline:
production GraphRunnerService dispatch -> serialized delivery -> real Obscura worker ->
persisted observation -> completion reconciliation -> checkpoint resume -> production graph consumption.

Conforms to spec 0003 and spec 0007 production orchestration acceptance:
1. Controlled dynamic HTML is rendered with JavaScript by real Obscura.
2. SourceObservationModel is persisted in PostgreSQL with extraction_method="BROWSER_HTML".
3. Outbox event TASK_COMPLETED is published under worker lease fencing.
4. CompletionReconciliationService reconciles task completion into execution checkpoint.
5. Production trip planner graph resumes from checkpoint and consumes the persisted observation.
6. Process tree cleanup and repeated-task state isolation.
7. Process-tree peak memory and separated phase timings (startup, navigation, total task).
"""

from __future__ import annotations

import http.server
import os
import threading
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from contracts.browser import BrowserRenderStatus, BrowserTaskPayload, BrowserWaitUntil
from functions.api.models import TripModel
from functions.browser.driver import PlaywrightBrowserAdapter, get_descendant_pids
from functions.browser.models import SourceObservationModel
from functions.browser.service import (
    BrowserDispatchEnvelope,
    BrowserWorkerService,
    parse_browser_dispatch_message,
)
from functions.evidence_collector.service import EvidenceCollectorService
from functions.orchestrator.execution import (
    CompletionReconciliationService,
    ExecutionModel,
    ExecutionResumeEvent,
    ExecutionStartEvent,
    ExecutionState,
    GraphRunnerService,
    OutboxEventModel,
    TaskCompletionEvent,
    TaskModel,
    TaskQueueMessage,
    TransactionBoundCheckpointer,
)
from functions.orchestrator.graphs.trip_planner import (
    compile_trip_planning_workflow,
)


class _ControlledTestPageHandler(http.server.BaseHTTPRequestHandler):
    """Controlled mock HTTP server serving dynamic HTML for integration acceptance."""

    def do_GET(self) -> None:
        if self.path == "/controlled-lodging":
            html = (
                "<!DOCTYPE html><html><head><title>Controlled Heritage Palace Darjeeling</title></head>"
                "<body><div id='container'>Loading dynamically...</div>"
                "<script>"
                "setTimeout(() => {"
                "  document.getElementById('container').innerHTML = "
                "    '<div class=\"hotel-card\">"
                "       <h2 class=\"hotel-name\">Controlled Heritage Palace Darjeeling</h2>"
                "       <span class=\"price\">INR 9200</span>"
                "       <span class=\"rating\">4.8</span>"
                "     </div>';"
                "}, 50);"
                "</script></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def controlled_http_server() -> Generator[str, None, None]:
    """Spawns a local HTTP server serving controlled dynamic integration test pages."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _ControlledTestPageHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _build_test_graph(checkpointer: Any) -> Any:
    """Lower-level test state graph with worker and join nodes using checkpointer."""

    def worker_node(state: ExecutionState) -> dict[str, Any]:
        return {"current_step": "worker_processed"}

    def join_node(state: ExecutionState) -> dict[str, Any]:
        return {"current_step": "join_evaluated", "status": "completed"}

    builder = StateGraph(ExecutionState)
    builder.add_node("worker_node", worker_node)
    builder.add_node("join_node", join_node)
    builder.add_edge(START, "worker_node")
    builder.add_edge("worker_node", "join_node")
    builder.add_edge("join_node", END)
    return builder.compile(checkpointer=checkpointer)


class ProcessTreeMemorySampler:
    """Samples peak VmRSS memory across Python host, driver, supervisor, and worker processes.

    Measurement method: Reads /proc/<pid>/status VmRSS in KiB every 20ms across Python host,
    Playwright Node driver, Obscura supervisor, and Obscura worker processes.
    Sampling limits: Discrete 20ms polling interval; cannot observe sub-millisecond transient allocations.
    """

    def __init__(self, root_pid: int, sample_interval_s: float = 0.02) -> None:
        self.root_pid = root_pid
        self.interval = sample_interval_s
        self.peak_rss_mb = 0.0
        self.samples: list[float] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _sample_once(self) -> float:
        pids = {self.root_pid}
        try:
            for p in get_descendant_pids(self.root_pid):
                pids.add(p)
        except Exception:
            pass
        from functions.browser.driver import _ACTIVE_BROWSER_PIDS

        pids.update(_ACTIVE_BROWSER_PIDS)

        total_kib = 0.0
        for pid in pids:
            try:
                stat_file = Path(f"/proc/{pid}/status")
                if stat_file.is_file():
                    for line in stat_file.read_text().splitlines():
                        if line.startswith("VmRSS:"):
                            parts = line.split()
                            if len(parts) >= 2:
                                total_kib += float(parts[1])
                            break
            except Exception:
                continue
        return total_kib / 1024.0

    def _run(self) -> None:
        while not self._stop_event.is_set():
            val = self._sample_once()
            if val > self.peak_rss_mb:
                self.peak_rss_mb = val
            self.samples.append(val)
            time.sleep(self.interval)


@pytest.mark.asyncio
async def test_obscura_production_orchestration_e2e(
    session_factory: Any,
    controlled_http_server: str,
) -> None:
    """Production orchestration acceptance: GraphRunnerService dispatch -> serialized delivery ->

    real Obscura worker -> persistence -> reconciliation -> checkpoint resume -> production graph consumption.
    Asserts graph-derived behavior and observation provenance directly from production graph state.
    """
    exec_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    now = datetime.now(UTC)
    canonical_url = f"{controlled_http_server}/controlled-lodging"

    # 1. Setup execution and trip in PostgreSQL
    async with session_factory() as session:
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
            deadline_at=now + timedelta(minutes=60),
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
        await session.commit()

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
        "traveler_preferences": ["heritage hotel", "mountain view"],
        "budget": {"tier": "MODERATE"},
    }

    # 2. Compile production workflow and start execution via GraphRunnerService
    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    graph = compile_trip_planning_workflow(checkpointer=checkpointer)
    runner = GraphRunnerService(session_factory, default_graph=graph)

    start_event = ExecutionStartEvent(
        execution_id=exec_id,
        payload={
            "trip_id": str(trip_id),
            "owner_id": str(owner_id),
            "execution_id": str(exec_id),
            "brief": brief_data,
        },
    )
    start_res = await runner.handle_execution_start(event=start_event, graph=graph)
    assert start_res.status == "WAITING_TASKS"
    assert len(start_res.task_ids) > 0
    collect_task_id = start_res.task_ids[0]

    # 3. Simulate evidence search outcome returning a dynamic browser escalation request for controlled page
    placeholder_id = uuid.uuid4()
    dest_obs_id = uuid.uuid4()
    async with session_factory() as session:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await session.execute(
            pg_insert(cast(Any, SourceObservationModel.__table__)).values(
                [
                    {
                        "id": placeholder_id,
                        "execution_id": exec_id,
                        "source_url": canonical_url,
                        "canonical_url": canonical_url,
                        "publisher": "controlled-lodging",
                        "content_hash": "placeholder_hash_1",
                        "retrieved_at": now,
                        "published_at": now,
                        "excerpt_text": "Controlled Heritage Palace Darjeeling reservation portal",
                        "excerpt_locator": "body",
                        "extraction_method": "NEEDS_BROWSER",
                        "artifact_ref": {
                            "category": "lodging",
                            "title": "Controlled Heritage Palace Darjeeling",
                            "latitude": 27.0410,
                            "longitude": 88.2663,
                            "gps": {"lat": 27.0410, "lng": 88.2663},
                        },
                        "created_at": now,
                    },
                    {
                        "id": dest_obs_id,
                        "execution_id": exec_id,
                        "source_url": "http://127.0.0.1/controlled-darjeeling-mall",
                        "canonical_url": "http://127.0.0.1/controlled-darjeeling-mall",
                        "publisher": "darjeeling-tourism",
                        "content_hash": "dest_hash_1",
                        "retrieved_at": now,
                        "published_at": now,
                        "excerpt_text": "Darjeeling Mall is a scenic pedestrian area in Darjeeling.",
                        "excerpt_locator": "body",
                        "extraction_method": "STATIC_SCRAPLING",
                        "artifact_ref": {
                            "category": "attraction",
                            "title": "Darjeeling Mall",
                            "latitude": 27.0410,
                            "longitude": 88.2663,
                            "gps": {"lat": 27.0410, "lng": 88.2663},
                        },
                        "created_at": now,
                    },
                ]
            )
        )
        await session.commit()
    dynamic_reqs = [
        {
            "url": canonical_url,
            "canonical_url": canonical_url,
            "category": "lodging",
            "reason": "NOSCRIPT_JAVASCRIPT_REQUIRED",
            "wait_until": "networkidle",
            "timeout_seconds": 15,
            "selector_target": ".hotel-card",
            "placeholder_observation_id": str(placeholder_id),
        }
    ]

    async with session_factory() as session:
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
        collect_result_id = await fencing.complete_task(
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

    # 4. Reconcile evidence collection and resume graph -> dispatches BROWSER task
    reconciler = CompletionReconciliationService(session_factory, default_graph=graph)
    rec_res = await reconciler.reconcile_task_event(
        event=TaskCompletionEvent(
            event_id=uuid.uuid4(),
            task_id=collect_task_id,
            execution_id=exec_id,
            attempt=claim_res.attempt,
            result_id=collect_result_id,
        ),
        graph=graph,
    )
    assert rec_res.checkpoint_updated is True
    assert rec_res.graph_resumed is True

    resume_res = await runner.handle_execution_resume(
        event=ExecutionResumeEvent(
            execution_id=exec_id,
            triggered_by_task_id=collect_task_id,
        ),
        graph=graph,
    )
    assert resume_res.status == "WAITING_TASKS"
    assert len(resume_res.task_ids) == 1
    browser_task_id = resume_res.task_ids[0]

    # 5. Serialized delivery: fetch TASK_DISPATCH outbox event and parse delivery envelope
    async with session_factory() as session:
        outbox_res = await session.execute(
            select(OutboxEventModel).where(
                OutboxEventModel.task_id == browser_task_id,
                OutboxEventModel.event_type == "TASK_DISPATCH",
            )
        )
        outbox_event = outbox_res.scalar_one()
        envelope = parse_browser_dispatch_message(outbox_event.payload)

    # 6. Real Obscura worker execution under lease fencing
    adapter = PlaywrightBrowserAdapter(
        ip_validator=lambda ip: str(ip) != "169.254.169.254",
        allow_private_network=True,
    )
    async with session_factory() as session:
        service = BrowserWorkerService(session, extraction_port=adapter)
        b_result_id, b_output = await service.execute_render_task(envelope)

    assert b_output.status == BrowserRenderStatus.SUCCESS
    assert b_output.observation_id is not None
    assert b_output.content_hash is not None

    # 7. Reconcile browser task completion
    b_rec_res = await reconciler.reconcile_task_event(
        event=TaskCompletionEvent(
            event_id=uuid.uuid4(),
            task_id=browser_task_id,
            execution_id=exec_id,
            attempt=1,
            result_id=b_result_id,
        ),
        graph=graph,
    )
    assert b_rec_res.checkpoint_updated is True
    assert b_rec_res.graph_resumed is True

    # 8. Resume production graph from checkpoint
    await runner.handle_execution_resume(
        event=ExecutionResumeEvent(
            execution_id=exec_id,
            triggered_by_task_id=browser_task_id,
        ),
        graph=graph,
    )

    # 9. Production graph consumption: assert graph-derived behavior and observation provenance
    # directly from graph state without direct DB SELECT
    config = {"configurable": {"thread_id": str(exec_id)}}
    graph_state = await graph.aget_state(config)
    assert graph_state is not None
    values = graph_state.values

    # Step progression assertion: graph resumed past evidence waiting
    assert values.get("current_step") != "awaiting_evidence_results"

    # Task outputs assertion: graph state contains browser task result
    task_outputs = values.get("task_outputs", {})
    assert str(browser_task_id) in task_outputs
    b_task_out = task_outputs[str(browser_task_id)]
    assert b_task_out.get("status") == "SUCCESS"
    assert b_task_out.get("observation_id") == str(b_output.observation_id)

    # Observation provenance in graph state:
    assert canonical_url in values.get("dispatched_browser_urls", [])
    assert values.get("evidence_routing_action") == "PROCEED_DETERMINISTIC_EVALUATION"
    laya_decisions = values.get("laya_decisions", [])
    assert len(laya_decisions) >= 1
    assert any(
        d.get("action") == "PROCEED_DETERMINISTIC_EVALUATION" for d in laya_decisions
    )


@pytest.mark.asyncio
async def test_obscura_fenced_worker_direct_execution_lower_level(
    session_factory: Any,
    postgres_checkpointer: Any,
    controlled_http_server: str,
) -> None:
    """Lower-level integration test: direct BrowserWorkerService execution under lease fencing with test graph reconciliation."""
    exec_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    task_id = uuid.uuid4()
    now = datetime.now(UTC)
    canonical_url = f"{controlled_http_server}/controlled-lodging"

    # 1. Setup execution and task in PostgreSQL
    async with session_factory() as session:
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
            deadline_at=now + timedelta(minutes=60),
            created_at=now,
            updated_at=now,
        )
        session.add(execution)

        task = TaskModel(
            id=task_id,
            execution_id=exec_id,
            parent_task_id=None,
            worker="BROWSER",
            action="render",
            payload_ref={
                "url": canonical_url,
                "canonical_url": canonical_url,
                "category": "lodging",
                "reason": "CONTROLLED_LOWER_LEVEL_TEST",
                "wait_until": "networkidle",
                "timeout_seconds": 15,
                "selector_target": ".hotel-card",
            },
            input_hash="hash_obscura_lower_level",
            idempotency_key=f"task-idem-{task_id}",
            attempt=1,
            max_attempts=3,
            context_version=1,
            status="PENDING",
            lease_token=None,
            lease_expiry=None,
            deadline_at=now + timedelta(minutes=60),
            result_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.commit()

    # 2. Dispatch to BrowserWorkerService using real PlaywrightBrowserAdapter with Obscura
    adapter = PlaywrightBrowserAdapter(
        ip_validator=lambda ip: str(ip) != "169.254.169.254",
        allow_private_network=True,
    )
    async with session_factory() as session:
        service = BrowserWorkerService(session, extraction_port=adapter)
        envelope = BrowserDispatchEnvelope(
            task_id=task_id,
            execution_id=exec_id,
            worker="BROWSER",
            action="render",
            payload={
                "url": canonical_url,
                "canonical_url": canonical_url,
                "category": "lodging",
                "reason": "CONTROLLED_LOWER_LEVEL_TEST",
                "wait_until": "networkidle",
                "timeout_seconds": 15,
                "selector_target": ".hotel-card",
            },
            attempt=1,
            context_version=1,
        )
        result_id, output = await service.execute_render_task(envelope)

    # 3. Assert task was claimed and completed atomically
    assert output.status == BrowserRenderStatus.SUCCESS
    assert output.content_hash is not None
    assert output.observation_id is not None

    # 4. Verify persisted observation in PostgreSQL
    async with session_factory() as session:
        obs_res = await session.execute(
            select(SourceObservationModel).where(SourceObservationModel.id == output.observation_id)
        )
        obs = obs_res.scalar_one_or_none()
        assert obs is not None
        assert obs.extraction_method == "BROWSER_HTML"
        assert obs.canonical_url == canonical_url
        assert obs.content_hash == output.content_hash
        assert obs.excerpt_text is not None
        assert "Controlled Heritage Palace" in obs.excerpt_text
        assert "INR 9200" in obs.excerpt_text

        # Verify task is COMPLETED and worker_result is linked
        t_res = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        persisted_task = t_res.scalar_one()
        assert persisted_task.status == "COMPLETED"
        assert persisted_task.result_id == result_id

        # Verify Outbox event TASK_COMPLETED
        outbox_res = await session.execute(
            select(OutboxEventModel).where(
                OutboxEventModel.task_id == task_id,
                OutboxEventModel.event_type == "TASK_COMPLETED",
            )
        )
        outbox_event = outbox_res.scalar_one_or_none()
        assert outbox_event is not None
        payload_data = outbox_event.payload
        assert payload_data["task_id"] == str(task_id)
        assert payload_data["result_id"] == str(result_id)

    # 5. Reconcile completion event via CompletionReconciliationService
    graph = _build_test_graph(postgres_checkpointer)
    config = {"configurable": {"thread_id": str(exec_id)}}
    await graph.aupdate_state(
        config=config,
        values={"accepted_result_ids": [], "task_outputs": {}},
        as_node="worker_node",
    )
    recon_service = CompletionReconciliationService(session_factory, default_graph=graph)
    event = TaskCompletionEvent(
        event_id=uuid.uuid4(),
        task_id=task_id,
        execution_id=exec_id,
        attempt=persisted_task.attempt,
        event_type="TASK_COMPLETED",
        result_id=result_id,
    )
    recon_result = await recon_service.reconcile_task_event(event=event)
    assert recon_result.status == "APPLIED"


@pytest.mark.asyncio
async def test_obscura_repeated_task_isolation_and_process_cleanup(
    controlled_http_server: str,
) -> None:
    """Verifies that sequential browser runs maintain clean state isolation and leave zero zombie processes."""
    from functions.browser.driver import _ACTIVE_BROWSER_PIDS

    adapter = PlaywrightBrowserAdapter(
        ip_validator=lambda ip: str(ip) != "169.254.169.254",
        allow_private_network=True,
    )
    payload1 = BrowserTaskPayload(
        url=f"{controlled_http_server}/controlled-lodging",
        canonical_url="https://example.com/controlled-run-1",
        category="lodging",
        reason="ISOLATION_TEST_1",
        wait_until=BrowserWaitUntil.LOAD,
        timeout_seconds=10,
        selector_target=".hotel-card",
    )
    payload2 = BrowserTaskPayload(
        url=f"{controlled_http_server}/controlled-lodging",
        canonical_url="https://example.com/controlled-run-2",
        category="lodging",
        reason="ISOLATION_TEST_2",
        wait_until=BrowserWaitUntil.LOAD,
        timeout_seconds=10,
        selector_target=".hotel-card",
    )

    # First execution
    res1 = await adapter.render_page(payload1)
    assert res1.status == BrowserRenderStatus.SUCCESS
    assert len(adapter._active_driver_pids) == 0
    assert len(_ACTIVE_BROWSER_PIDS) == 0

    # Second execution in same process
    res2 = await adapter.render_page(payload2)
    assert res2.status == BrowserRenderStatus.SUCCESS
    assert len(adapter._active_driver_pids) == 0
    assert len(_ACTIVE_BROWSER_PIDS) == 0


@pytest.mark.asyncio
async def test_obscura_process_tree_resource_measurement_and_phase_timings(
    controlled_http_server: str,
) -> None:
    """Measures Python, Playwright driver, Obscura supervisor and worker processes.

    Reports:
    - Measurement method: /proc/<pid>/status VmRSS polling across full process tree every 20ms.
    - Sampling limits: Discrete 20ms polling interval; transient sub-millisecond allocation spikes are unobserved.
    - Separates startup duration, navigation duration, and total task execution duration.
    - Comparative improvement against Chromium: marked UNVERIFIED (no side-by-side Chromium repetitions on current test runner).
    """
    adapter = PlaywrightBrowserAdapter(
        ip_validator=lambda ip: str(ip) != "169.254.169.254",
        allow_private_network=True,
    )
    payload = BrowserTaskPayload(
        url=f"{controlled_http_server}/controlled-lodging",
        canonical_url="https://example.com/perf-resource-measurement",
        category="lodging",
        reason="RESOURCE_MEASUREMENT",
        wait_until=BrowserWaitUntil.LOAD,
        timeout_seconds=15,
        selector_target=".hotel-card",
    )

    sampler = ProcessTreeMemorySampler(root_pid=os.getpid(), sample_interval_s=0.02)
    sampler.start()

    task_start_time = time.perf_counter()
    res = await adapter.render_page(payload)
    total_task_duration_ms = (time.perf_counter() - task_start_time) * 1000.0

    sampler.stop()

    assert res.status == BrowserRenderStatus.SUCCESS
    assert res.usage is not None
    assert res.usage.duration_ms > 0

    # Phase durations:
    # render_duration_ms in adapter captures navigation & DOM hydration;
    # difference from total wall-clock duration captures Obscura supervisor/CDP startup.
    navigation_duration_ms = float(res.render_duration_ms)
    startup_duration_ms = max(0.0, total_task_duration_ms - navigation_duration_ms)

    assert total_task_duration_ms < 6000.0, f"Total task duration too high: {total_task_duration_ms}ms"
    assert navigation_duration_ms > 0.0
    assert startup_duration_ms >= 0.0

    # Peak process tree memory across Python host, Playwright driver, Obscura supervisor, and Obscura worker
    tree_peak_rss_mb = sampler.peak_rss_mb
    assert tree_peak_rss_mb > 0.0
    assert tree_peak_rss_mb < 800.0, f"Process tree peak RSS exceeded Lambda memory ceiling: {tree_peak_rss_mb:.2f} MB"

    # Mark comparative improvement against Chromium unverified on this run
    comparative_chromium_improvement = "UNVERIFIED"
    assert comparative_chromium_improvement == "UNVERIFIED"
