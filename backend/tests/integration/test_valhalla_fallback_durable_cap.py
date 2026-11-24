"""Integration regression tests for durable three attempt cap across worker crashes and redelivery.

Verifies against isolated acceptance PostgreSQL:
1. Durable execution budget reservation commits BEFORE each outbound /route attempt on recovery path.
2. Crashes after or during an outbound /route attempt do NOT refund uncertain attempts on recovery.
3. Redelivery to a fresh worker instance honors the shared 3 attempt cap including retries.
4. Normal short trip routing (matrix succeeds with no nulls) does NOT charge the fallback budget.
5. Sends are rejected before any outbound HTTP call when lease or deadline is invalid.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.routing import Point, RouteRequest, RouteStopInput, StopType
from functions.api.models import TripModel
from functions.orchestrator.execution.budget import reserve_route_fallback_attempt
from functions.orchestrator.execution.fencing import DeadlineExceededError, LeaseFencingError
from functions.orchestrator.execution.models import (
    ExecutionModel,
    JobEventModel,
    OutboxEventModel,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.execution.recovery import TaskRecoveryService
from functions.orchestrator.routing.models import RouteModel
from functions.route_optimizer.service import (
    ExecutionDeadlineExceededError,
    RouteOptimizerDispatchEnvelope,
    RouteOptimizerWorkerService,
)
from functions.route_optimizer.valhalla import ValhallaClient


def _encode_polyline6(coords_lat_lon: list[tuple[float, float]]) -> str:
    """Encodes list of (lat, lon) pairs into Valhalla polyline6."""
    out: list[str] = []
    prev_lat = 0
    prev_lon = 0
    for lat, lon in coords_lat_lon:
        ilat = round(lat * 1e6)
        ilon = round(lon * 1e6)
        for v in (ilat - prev_lat, ilon - prev_lon):
            s = ~(v << 1) if v < 0 else (v << 1)
            while s >= 0x20:
                out.append(chr((0x20 | (s & 0x1F)) + 63))
                s >>= 5
            out.append(chr(s + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


class _SimulatedCrashError(BaseException):
    """Simulates worker process termination mid-task."""


async def _cleanup_execution(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    trip_id: uuid.UUID,
) -> None:
    async with session_factory() as cleanup:
        await cleanup.execute(delete(RouteModel).where(RouteModel.execution_id == execution_id))
        await cleanup.execute(
            delete(WorkerResultModel).where(WorkerResultModel.execution_id == execution_id)
        )
        await cleanup.execute(
            delete(OutboxEventModel).where(OutboxEventModel.execution_id == execution_id)
        )
        await cleanup.execute(
            delete(JobEventModel).where(JobEventModel.execution_id == execution_id)
        )
        await cleanup.execute(delete(TaskModel).where(TaskModel.execution_id == execution_id))
        await cleanup.execute(delete(ExecutionModel).where(ExecutionModel.id == execution_id))
        await cleanup.execute(delete(TripModel).where(TripModel.id == trip_id))
        await cleanup.commit()


@pytest.mark.asyncio
async def test_durable_three_attempt_cap_across_crash_and_redelivery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Replays crash case 1A: 503 retry + crash on 3rd HTTP /route attempt, then redelivery."""
    now = datetime.now(UTC)
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()

    outbound_route_calls: list[dict[str, Any]] = []
    crash_enabled = {"active": True}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sources_to_targets"):
            return httpx.Response(
                400,
                json={"error_code": 154, "error": "Path distance exceeds max distance limit"},
            )
        if path.endswith("/route"):
            outbound_route_calls.append({"idx": len(outbound_route_calls) + 1})
            call_num = len(outbound_route_calls)
            if call_num == 1:
                # Transient 503 consumes 1st attempt; retry will consume 2nd attempt
                return httpx.Response(503, json={"error": "transient error"})
            if crash_enabled["active"] and call_num == 3:
                # Crash mid-flight on 3rd outbound HTTP call (2nd probe)
                raise _SimulatedCrashError("Worker crashed mid-probe")
            import json as _json

            body = _json.loads(request.content.decode())
            locs = body.get("locations", [])
            shape = _encode_polyline6(
                [(p["lat"], p["lon"]) for p in locs]
            )
            return httpx.Response(
                200,
                json={
                    "trip": {
                        "summary": {"length": 250.0, "time": 18000},
                        "legs": [
                            {
                                "summary": {"length": 250.0, "time": 18000},
                                "shape": shape,
                                "maneuvers": [],
                            }
                        ],
                    }
                },
            )
        return httpx.Response(404)

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=now,
        is_round_trip=True,
        stops=[
            RouteStopInput(
                name="Origin",
                location=Point(latitude=12.9716, longitude=77.5946),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="Hub",
                location=Point(latitude=12.4244, longitude=75.7382),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
            ),
        ],
    )

    try:
        async with session_factory() as s:
            async with s.begin():
                s.add(
                    TripModel(
                        id=trip_id,
                        owner_id=owner_id,
                        current_version=1,
                        state="planning",
                        created_at=now,
                        updated_at=now,
                    )
                )
                s.add(
                    ExecutionModel(
                        id=execution_id,
                        owner_id=owner_id,
                        trip_id=trip_id,
                        workflow="TRAVEL",
                        status="WAITING_TASKS",
                        context_version=1,
                        deadline_at=now + timedelta(hours=1),
                        budget={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                s.add(
                    TaskModel(
                        id=task_id,
                        execution_id=execution_id,
                        worker="OPTIMIZER",
                        action="solve",
                        payload_ref=req.model_dump(mode="json"),
                        input_hash="h_cap_1a",
                        idempotency_key=f"{execution_id}:opt1a",
                        attempt=0,
                        max_attempts=3,
                        context_version=1,
                        status="PENDING",
                        deadline_at=now + timedelta(hours=1),
                        created_at=now,
                        updated_at=now,
                    )
                )

        # Worker 1 runs and crashes on 3rd outbound HTTP attempt
        async with session_factory() as s1:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http1:
                vclient1 = ValhallaClient(base_url="http://valhalla.local", http_client=http1)
                worker1 = RouteOptimizerWorkerService(s1, valhalla_client=vclient1)
                env1 = RouteOptimizerDispatchEnvelope(
                    task_id=task_id,
                    execution_id=execution_id,
                    worker="OPTIMIZER",
                    action="solve",
                    payload=req.model_dump(mode="json"),
                    attempt=0,
                    context_version=1,
                )
                with pytest.raises(_SimulatedCrashError):
                    await worker1.execute_task(env1)
                await s1.rollback()

        assert len(outbound_route_calls) == 3

        # Simulate lease expiry and TaskRecoveryService redelivery
        async with session_factory() as s_exp:
            t_row = (
                await s_exp.execute(select(TaskModel).where(TaskModel.id == task_id))
            ).scalar_one()
            t_row.lease_expiry = now - timedelta(seconds=5)
            await s_exp.commit()

        recovery = TaskRecoveryService(session_factory, base_backoff_seconds=1)
        rec = await recovery.recover_expired_leases()
        assert task_id in rec.recovered_retried

        # Ensure uncertain crashed attempt was NOT refunded after recovery
        async with session_factory() as s_chk:
            exec_mid = (
                await s_chk.execute(
                    select(ExecutionModel).where(ExecutionModel.id == execution_id)
                )
            ).scalar_one()
            rf_mid = (exec_mid.budget or {}).get("route_fallback", {})
            assert rf_mid.get("uncertain_attempts", 0) >= 1
            assert rf_mid.get("remaining_attempts", 3) == 0

            # Clear backoff expiry so fresh worker can claim immediately
            t_row2 = (
                await s_chk.execute(select(TaskModel).where(TaskModel.id == task_id))
            ).scalar_one()
            t_row2.lease_expiry = None
            await s_chk.commit()

        # Worker 2 (fresh instance): attempts must obey the 3-attempt execution-wide cap
        crash_enabled["active"] = False
        async with session_factory() as s2:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http2:
                vclient2 = ValhallaClient(base_url="http://valhalla.local", http_client=http2)
                worker2 = RouteOptimizerWorkerService(s2, valhalla_client=vclient2)
                env2 = RouteOptimizerDispatchEnvelope(
                    task_id=task_id,
                    execution_id=execution_id,
                    worker="OPTIMIZER",
                    action="solve",
                    payload=req.model_dump(mode="json"),
                    attempt=1,
                    context_version=1,
                )
                route_id, output = await worker2.execute_task(env2)

        assert len(outbound_route_calls) <= 3
        out_dict = output if isinstance(output, dict) else output.model_dump(mode="json")
        assert route_id is None
        assert out_dict["routing_status"] == "ROUTING_UNAVAILABLE"
    finally:
        await _cleanup_execution(session_factory, execution_id=execution_id, trip_id=trip_id)


@pytest.mark.asyncio
async def test_durable_three_attempt_cap_with_null_matrix_and_retries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Replays crash case 1B: 3x3 null matrix with 2 retries on probe 1, crash on 4th /route, redelivery."""
    now = datetime.now(UTC)
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()

    outbound_route_calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        path = request.url.path
        body = _json.loads(request.content.decode()) if request.content else {}
        if path.endswith("/sources_to_targets"):
            n = len(body.get("sources", []))
            rows = [
                [{"distance": 0.0, "time": 0} if i == j else None for j in range(n)]
                for i in range(n)
            ]
            return httpx.Response(200, json={"sources_to_targets": rows})
        if path.endswith("/route"):
            outbound_route_calls.append(len(outbound_route_calls) + 1)
            idx = len(outbound_route_calls)
            if idx in (1, 2):
                return httpx.Response(503, json={"error": "overload"})
            locs = body.get("locations", [])
            shape = _encode_polyline6([(p["lat"], p["lon"]) for p in locs])
            return httpx.Response(
                200,
                json={
                    "trip": {
                        "summary": {"length": 40.0, "time": 1800},
                        "legs": [
                            {
                                "summary": {"length": 40.0, "time": 1800},
                                "shape": shape,
                                "maneuvers": [],
                            }
                        ],
                    }
                },
            )
        return httpx.Response(404)

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=now,
        is_round_trip=False,
        stops=[
            RouteStopInput(
                name="A",
                location=Point(latitude=12.9, longitude=77.5),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="B",
                location=Point(latitude=12.7, longitude=77.3),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="C",
                location=Point(latitude=12.5, longitude=77.1),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
            ),
        ],
    )

    try:
        async with session_factory() as s:
            async with s.begin():
                s.add(
                    TripModel(
                        id=trip_id,
                        owner_id=owner_id,
                        current_version=1,
                        state="planning",
                        created_at=now,
                        updated_at=now,
                    )
                )
                s.add(
                    ExecutionModel(
                        id=execution_id,
                        owner_id=owner_id,
                        trip_id=trip_id,
                        workflow="TRAVEL",
                        status="WAITING_TASKS",
                        context_version=1,
                        deadline_at=now + timedelta(hours=1),
                        budget={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                s.add(
                    TaskModel(
                        id=task_id,
                        execution_id=execution_id,
                        worker="OPTIMIZER",
                        action="solve",
                        payload_ref=req.model_dump(mode="json"),
                        input_hash="h_cap_1b",
                        idempotency_key=f"{execution_id}:opt1b",
                        attempt=0,
                        max_attempts=3,
                        context_version=1,
                        status="PENDING",
                        deadline_at=now + timedelta(hours=1),
                        created_at=now,
                        updated_at=now,
                    )
                )

        # Worker 1 runs: 2 retries + 1 success = 3 HTTP /route calls; 4th attempt is blocked BEFORE send
        async with session_factory() as s1:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http1:
                v1 = ValhallaClient(base_url="http://valhalla.local", http_client=http1)
                w1 = RouteOptimizerWorkerService(s1, valhalla_client=v1)
                await w1.execute_task(
                    RouteOptimizerDispatchEnvelope(
                        task_id=task_id,
                        execution_id=execution_id,
                        worker="OPTIMIZER",
                        action="solve",
                        payload=req.model_dump(mode="json"),
                        attempt=0,
                        context_version=1,
                    )
                )

        assert len(outbound_route_calls) <= 3

        # Simulate redelivery (reset task to PENDING with attempt=1) -> Worker 2 still cannot exceed 3 total
        async with session_factory() as s_prep:
            t_row = (
                await s_prep.execute(select(TaskModel).where(TaskModel.id == task_id))
            ).scalar_one()
            t_row.status = "PENDING"
            t_row.lease_token = None
            t_row.lease_expiry = None
            await s_prep.commit()

        async with session_factory() as s2:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http2:
                v2 = ValhallaClient(base_url="http://valhalla.local", http_client=http2)
                w2 = RouteOptimizerWorkerService(s2, valhalla_client=v2)
                route_id_2, out_2 = await w2.execute_task(
                    RouteOptimizerDispatchEnvelope(
                        task_id=task_id,
                        execution_id=execution_id,
                        worker="OPTIMIZER",
                        action="solve",
                        payload=req.model_dump(mode="json"),
                        attempt=1,
                        context_version=1,
                    )
                )

        assert len(outbound_route_calls) <= 3
        assert route_id_2 is None
        out_dict = out_2 if isinstance(out_2, dict) else out_2.model_dump(mode="json")
        assert out_dict["routing_status"] == "ROUTING_UNAVAILABLE"
    finally:
        await _cleanup_execution(session_factory, execution_id=execution_id, trip_id=trip_id)


@pytest.mark.asyncio
async def test_normal_short_trip_routing_does_not_charge_fallback_budget(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies normal short-trip routing with complete matrix does not consume route_fallback attempts."""
    now = datetime.now(UTC)
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()

    route_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal route_calls
        import json as _json

        path = request.url.path
        body = _json.loads(request.content.decode()) if request.content else {}
        if path.endswith("/sources_to_targets"):
            n = len(body.get("sources", []))
            rows = [
                [
                    {"distance": 0.0, "time": 0} if i == j else {"distance": 15.0, "time": 1200}
                    for j in range(n)
                ]
                for i in range(n)
            ]
            return httpx.Response(200, json={"sources_to_targets": rows})
        if path.endswith("/route"):
            route_calls += 1
            locs = body.get("locations", [])
            shape = _encode_polyline6([(p["lat"], p["lon"]) for p in locs])
            return httpx.Response(
                200,
                json={
                    "trip": {
                        "summary": {"length": 15.0, "time": 1200},
                        "legs": [
                            {
                                "summary": {"length": 15.0, "time": 1200},
                                "shape": shape,
                                "maneuvers": [],
                            }
                        ],
                    }
                },
            )
        return httpx.Response(404)

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=now,
        is_round_trip=True,
        stops=[
            RouteStopInput(
                name="S0",
                location=Point(latitude=12.90, longitude=77.50),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="S1",
                location=Point(latitude=12.92, longitude=77.52),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="S2",
                location=Point(latitude=12.94, longitude=77.54),
                stop_type=StopType.WAYPOINT,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="S3",
                location=Point(latitude=12.96, longitude=77.56),
                stop_type=StopType.WAYPOINT,
                is_must_visit=True,
            ),
        ],
    )

    try:
        async with session_factory() as s:
            async with s.begin():
                s.add(
                    TripModel(
                        id=trip_id,
                        owner_id=owner_id,
                        current_version=1,
                        state="planning",
                        created_at=now,
                        updated_at=now,
                    )
                )
                s.add(
                    ExecutionModel(
                        id=execution_id,
                        owner_id=owner_id,
                        trip_id=trip_id,
                        workflow="TRAVEL",
                        status="WAITING_TASKS",
                        context_version=1,
                        deadline_at=now + timedelta(hours=1),
                        budget={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                s.add(
                    TaskModel(
                        id=task_id,
                        execution_id=execution_id,
                        worker="OPTIMIZER",
                        action="solve",
                        payload_ref=req.model_dump(mode="json"),
                        input_hash="h_short",
                        idempotency_key=f"{execution_id}:optshort",
                        attempt=0,
                        max_attempts=3,
                        context_version=1,
                        status="PENDING",
                        deadline_at=now + timedelta(hours=1),
                        created_at=now,
                        updated_at=now,
                    )
                )

        async with session_factory() as s1:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http1:
                v1 = ValhallaClient(base_url="http://valhalla.local", http_client=http1)
                w1 = RouteOptimizerWorkerService(s1, valhalla_client=v1)
                route_id, _ = await w1.execute_task(
                    RouteOptimizerDispatchEnvelope(
                        task_id=task_id,
                        execution_id=execution_id,
                        worker="OPTIMIZER",
                        action="solve",
                        payload=req.model_dump(mode="json"),
                        attempt=0,
                        context_version=1,
                    )
                )

        assert route_id is not None
        # 4 hydration legs executed without hitting the 3-attempt fallback cap
        assert route_calls == 4

        async with session_factory() as s_chk:
            ex = (
                await s_chk.execute(
                    select(ExecutionModel).where(ExecutionModel.id == execution_id)
                )
            ).scalar_one()
            rf = (ex.budget or {}).get("route_fallback", {})
            assert rf.get("spent_attempts", 0) == 0
            assert rf.get("remaining_attempts", 3) == 3
    finally:
        await _cleanup_execution(session_factory, execution_id=execution_id, trip_id=trip_id)


@pytest.mark.asyncio
async def test_rejects_send_without_valid_lease_or_expired_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies outbound recovery sends are fenced when lease is invalid/expired or execution deadline has passed."""
    now = datetime.now(UTC)
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    task_id = uuid.uuid4()
    active_lease = uuid.uuid4()
    wrong_lease = uuid.uuid4()

    route_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal route_calls
        if request.url.path.endswith("/sources_to_targets"):
            return httpx.Response(
                400,
                json={"error_code": 154, "error": "Path distance exceeds limit"},
            )
        if request.url.path.endswith("/route"):
            route_calls += 1
            return httpx.Response(503, json={"error": "should_not_send"})
        return httpx.Response(404)

    req = RouteRequest(
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        departure_at=now,
        is_round_trip=False,
        stops=[
            RouteStopInput(
                name="Origin",
                location=Point(latitude=12.9716, longitude=77.5946),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="Madikeri",
                location=Point(latitude=12.4244, longitude=75.7382),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
            ),
        ],
    )

    try:
        async with session_factory() as s:
            async with s.begin():
                s.add(
                    TripModel(
                        id=trip_id,
                        owner_id=owner_id,
                        current_version=1,
                        state="planning",
                        created_at=now,
                        updated_at=now,
                    )
                )
                s.add(
                    ExecutionModel(
                        id=execution_id,
                        owner_id=owner_id,
                        trip_id=trip_id,
                        workflow="TRAVEL",
                        status="WAITING_TASKS",
                        context_version=1,
                        deadline_at=now - timedelta(minutes=5),
                        budget={},
                        created_at=now - timedelta(hours=1),
                        updated_at=now,
                    )
                )
                s.add(
                    TaskModel(
                        id=task_id,
                        execution_id=execution_id,
                        worker="OPTIMIZER",
                        action="solve",
                        payload_ref=req.model_dump(mode="json"),
                        input_hash="h_fence_rej",
                        idempotency_key=f"{execution_id}:optfence",
                        attempt=1,
                        max_attempts=3,
                        context_version=1,
                        status="RUNNING",
                        lease_token=active_lease,
                        lease_expiry=now + timedelta(minutes=5),
                        deadline_at=now - timedelta(minutes=5),
                        created_at=now - timedelta(hours=1),
                        updated_at=now,
                    )
                )

        # 1. Wrong lease token rejects reservation before any HTTP send
        async with session_factory() as s1:
            res_id, denial = await reserve_route_fallback_attempt(
                s1,
                execution_id=execution_id,
                task_id=task_id,
                lease_token=wrong_lease,
                probe_key="k_wrong_lease",
                attempt=1,
            )
            assert res_id is None
            assert denial is not None

        # 2. Worker execution with expired execution deadline sends 0 outbound /route requests
        async with session_factory() as s2:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http2:
                vclient2 = ValhallaClient(base_url="http://valhalla.local", http_client=http2)
                worker2 = RouteOptimizerWorkerService(s2, valhalla_client=vclient2)
                env = RouteOptimizerDispatchEnvelope(
                    task_id=task_id,
                    execution_id=execution_id,
                    worker="OPTIMIZER",
                    action="solve",
                    payload=req.model_dump(mode="json"),
                    attempt=1,
                    context_version=1,
                )
                with pytest.raises(
                    (ExecutionDeadlineExceededError, DeadlineExceededError, LeaseFencingError)
                ):
                    await worker2.execute_task(env)

        assert route_calls == 0
    finally:
        await _cleanup_execution(session_factory, execution_id=execution_id, trip_id=trip_id)
