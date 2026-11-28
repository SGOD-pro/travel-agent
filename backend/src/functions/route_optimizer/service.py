"""Route optimizer worker service conforming to spec 0005 AC-9.

Executes asynchronous route optimization under lease fencing, validates parent
execution lease fences and cooperative cancellation, computes schedules via
OrToolsRoutingSolver or MultimodalTransitionService, persists routes in PostgreSQL,
and completes tasks atomically under WorkerFencingService.
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from contracts.routing import (
    OptimizedRoute,
    Point,
    RestrictionType,
    RouteLegOutput,
    RouteOptimizerWorkerOutput,
    RouteReplanRequest,
    RouteRequest,
    RouteStatus,
    RouteStopInput,
    ScheduledStopOutput,
    SolverStatus,
    StopType,
    VehicleFuelProfile,
    select_active_daily_window,
)
from contracts.trips import TransportMode
from functions.orchestrator.execution.budget import (
    reconcile_route_fallback_attempt,
    reserve_route_fallback_attempt,
)
from functions.orchestrator.execution.fencing import (
    ExecutionNotFoundError,
    LeaseFencingError,
    ParentCancelledError,
    ParentTerminalError,
    TaskClaimResult,
    TaskQueueMessage,
    WorkerFencingService,
)
from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.places.repository import CanonicalPlacesRepository
from functions.orchestrator.routing.models import RouteModel
from functions.orchestrator.routing.repository import RouteRepository
from functions.route_optimizer.closures import (
    ClosureAvoidanceService,
    RestrictionClaim,
    UnsupportedRestrictionError,
)
from functions.route_optimizer.multimodal import MultimodalTransitionService
from functions.route_optimizer.replan import RouteReplanService
from functions.route_optimizer.solver import OrToolsRoutingSolver
from functions.route_optimizer.valhalla import (
    MatrixResponse,
    ValhallaClient,
    ValhallaConnectionError,
    ValhallaRouteResult,
    ValhallaRoutingError,
    is_valhalla_matrix_distance_limit_error,
    is_valhalla_route_distance_limit_error,
)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensures a datetime is timezone-aware in UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


logger = logging.getLogger(__name__)


class ExecutionDeadlineExceededError(Exception):
    """Raised when task execution begins after or exceeds parent execution deadline."""


@dataclass(frozen=True)
class RouteOptimizerDispatchEnvelope:
    """Parsed dispatch envelope for route optimizer worker tasks."""

    task_id: uuid.UUID
    execution_id: uuid.UUID
    worker: str
    action: str
    payload: dict[str, Any]
    attempt: int
    context_version: int

    def to_task_queue_message(self) -> TaskQueueMessage:
        """Converts to TaskQueueMessage for fencing claim evaluation."""
        return TaskQueueMessage(
            task_id=self.task_id,
            execution_id=self.execution_id,
            attempt=self.attempt,
        )


def parse_optimizer_dispatch_message(
    record_or_body: dict[str, Any] | str,
) -> RouteOptimizerDispatchEnvelope:
    """Parses an SQS record or body into a normalized RouteOptimizerDispatchEnvelope."""
    if isinstance(record_or_body, str):
        body = json.loads(record_or_body)
    elif "body" in record_or_body:
        raw = record_or_body["body"]
        body = json.loads(raw) if isinstance(raw, str) else raw
    else:
        body = record_or_body

    payload_container = body.get("payload") or body.get("payload_ref") or {}
    task_id_raw = body.get("task_id") or payload_container.get("task_id")
    execution_id_raw = body.get("execution_id") or payload_container.get("execution_id")

    if not task_id_raw or not execution_id_raw:
        raise ValueError("Missing required task_id or execution_id in route optimizer message")

    task_id = uuid.UUID(str(task_id_raw))
    execution_id = uuid.UUID(str(execution_id_raw))
    attempt = int(payload_container.get("attempt", body.get("attempt", 0)))
    context_version = int(payload_container.get("context_version", body.get("context_version", 1)))
    worker = str(payload_container.get("worker", body.get("worker", "OPTIMIZER")))
    action = str(payload_container.get("action", body.get("action", "solve")))

    inner_payload = payload_container.get("payload") or payload_container

    return RouteOptimizerDispatchEnvelope(
        task_id=task_id,
        execution_id=execution_id,
        worker=worker,
        action=action,
        payload=inner_payload,
        attempt=attempt,
        context_version=context_version,
    )


class RouteOptimizerWorkerService:
    """Asynchronous route optimizer worker service executing claimed tasks under lease fencing."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        solver: OrToolsRoutingSolver | None = None,
        valhalla_client: Any | None = None,
        closure_service: ClosureAvoidanceService | None = None,
        multimodal_service: MultimodalTransitionService | None = None,
        replan_service: RouteReplanService | None = None,
        route_repo: RouteRepository | None = None,
        fencing_service: WorkerFencingService | None = None,
    ) -> None:
        self._session = session
        self._solver = solver or OrToolsRoutingSolver()
        self._valhalla_client = valhalla_client or ValhallaClient()
        self._closure_service = closure_service or ClosureAvoidanceService()
        self._places_repo = CanonicalPlacesRepository(session)
        self._multimodal_service = multimodal_service or MultimodalTransitionService(
            places_repo=self._places_repo,
            valhalla_client=self._valhalla_client,
            closure_service=self._closure_service,
        )
        self._route_repo = route_repo or RouteRepository(session)
        self._replan_service = replan_service or RouteReplanService(
            solver=self._solver,
            valhalla_client=self._valhalla_client,
            multimodal_service=self._multimodal_service,
            closure_service=self._closure_service,
            route_repo=self._route_repo,
        )
        self._fencing_service = fencing_service or WorkerFencingService(session)
        self._execution_probe_cache: dict[str, ValhallaRouteResult | Exception] = {}
        self._cache_attempt_counts: dict[int, int] = {}

    @staticmethod
    def _build_probe_key(
        locations: list[Point], mode: TransportMode, polys_key: str
    ) -> str:
        coords_part = "->".join(
            f"{round(p.latitude, 6)},{round(p.longitude, 6)}" for p in locations
        )
        return f"{coords_part}:{mode.value}:{polys_key}"

    async def _persist_route_probe_entry(
        self,
        *,
        execution_id: uuid.UUID | None,
        exec_model: ExecutionModel | None,
        probe_key: str,
        payload: dict[str, Any],
    ) -> None:
        """Persists a completed fallback probe outcome for cross attempt reuse."""
        summary_entry = {
            k: v
            for k, v in payload.items()
            if k in ("status", "status_code", "error_code") and v is not None
        }
        if isinstance(self._session, AsyncSession) and execution_id is not None:
            exec_stmt = (
                select(ExecutionModel).where(ExecutionModel.id == execution_id).with_for_update()
            )
            db_exec = (await self._session.execute(exec_stmt)).scalar_one_or_none()
            if db_exec is not None:
                b = dict(db_exec.budget or {})
                rf = dict(b.get("route_fallback") or {})
                probes = dict(rf.get("probes") or {})
                probes[probe_key] = payload
                rf["probes"] = probes
                b["route_fallback"] = rf
                legacy = dict(b.get("route_probes") or {})
                legacy[probe_key] = summary_entry
                b["route_probes"] = legacy
                db_exec.budget = b
                flag_modified(db_exec, "budget")
                await self._session.commit()
                if exec_model is not None:
                    exec_model.budget = b
                return

        if exec_model is not None:
            b = dict(exec_model.budget or {})
            rf = dict(b.get("route_fallback") or {})
            probes = dict(rf.get("probes") or {})
            probes[probe_key] = payload
            rf["probes"] = probes
            b["route_fallback"] = rf
            legacy = dict(b.get("route_probes") or {})
            legacy[probe_key] = summary_entry
            b["route_probes"] = legacy
            exec_model.budget = b
            flag_modified(exec_model, "budget")

    async def _probe_route_waypoints(
        self,
        locations: list[Point],
        *,
        mode: TransportMode,
        client: Any,
        exclude_polygons: list[list[list[float]]] | None = None,
        execution_id: uuid.UUID | None = None,
        exec_model: ExecutionModel | None = None,
        probe_cache: dict[str, ValhallaRouteResult | Exception] | None = None,
        task_id: uuid.UUID | None = None,
        task_attempt: int | None = None,
        lease_token: uuid.UUID | None = None,
    ) -> ValhallaRouteResult:
        """Executes a guarded /route probe with durable execution-wide attempt cap and lease fencing."""
        cache = probe_cache if probe_cache is not None else self._execution_probe_cache

        polys_key = (
            str(sorted([sorted([tuple(p) for p in ring]) for ring in exclude_polygons]))
            if exclude_polygons
            else ""
        )
        probe_key = self._build_probe_key(locations, mode, polys_key)

        now = datetime.now(UTC)
        if exec_model and exec_model.deadline_at and now >= exec_model.deadline_at:
            raise ExecutionDeadlineExceededError(
                f"Execution deadline {exec_model.deadline_at.isoformat()} exceeded before probe"
            )

        cached_probe = cache.get(probe_key)
        if cached_probe is not None:
            if isinstance(cached_probe, Exception):
                raise cached_probe
            return cached_probe

        use_db_budget = isinstance(self._session, AsyncSession) and execution_id is not None
        budget_source: dict[str, Any] | None = (
            dict(exec_model.budget or {}) if exec_model is not None else None
        )
        if use_db_budget:
            db_exec = (
                await self._session.execute(
                    select(ExecutionModel).where(ExecutionModel.id == execution_id)
                )
            ).scalar_one_or_none()
            if db_exec is not None:
                if db_exec.deadline_at and datetime.now(UTC) >= db_exec.deadline_at:
                    raise ExecutionDeadlineExceededError(
                        f"Execution deadline {db_exec.deadline_at.isoformat()} exceeded before probe"
                    )
                budget_source = dict(db_exec.budget or {})
                if exec_model is not None:
                    exec_model.budget = budget_source

        if budget_source:
            rf_probes = (budget_source.get("route_fallback") or {}).get("probes", {})
            legacy_probes = budget_source.get("route_probes", {})
            merged_probes = {**legacy_probes, **rf_probes}
            if probe_key in merged_probes:
                entry = merged_probes[probe_key]
                if entry.get("status") == "SUCCESS":
                    cached_res = ValhallaRouteResult.model_validate(entry["result"])
                    cache[probe_key] = cached_res
                    return cached_res
                elif entry.get("status") == "FAILED":
                    sc = entry.get("status_code")
                    if sc is not None and 400 <= int(sc) < 500 and int(sc) != 429:
                        cached_err = ValhallaRoutingError(
                            entry.get("error_message", "Cached probe failure"),
                            status_code=sc,
                            error_code=entry.get("error_code"),
                            endpoint=entry.get("endpoint", "/route"),
                        )
                        cache[probe_key] = cached_err
                        raise cached_err

        cache_id = id(cache)

        async def _on_before_attempt() -> str | None:
            if use_db_budget:
                assert execution_id is not None
                res_id, denial = await reserve_route_fallback_attempt(
                    self._session,
                    execution_id=execution_id,
                    probe_key=probe_key,
                    task_id=task_id,
                    attempt=task_attempt,
                    lease_token=lease_token,
                    max_attempts_cap=3,
                )
                if denial == "deadline_exceeded":
                    raise ExecutionDeadlineExceededError(
                        "Execution or task deadline exceeded before outbound /route attempt"
                    )
                if denial in (
                    "invalid_lease",
                    "lease_expired",
                    "stale_attempt",
                    "task_not_active",
                    "task_not_found",
                ):
                    raise LeaseFencingError(
                        f"Outbound /route attempt rejected by lease fence: {denial}"
                    )
                if denial == "execution_cancelled":
                    raise ParentCancelledError("Parent execution cancelled before /route attempt")
                if denial == "execution_terminal":
                    raise ParentTerminalError(
                        f"Execution {execution_id} already terminal before /route attempt"
                    )
                if denial == "execution_not_found":
                    raise ExecutionNotFoundError(
                        f"Execution {execution_id} not found before /route attempt"
                    )
                if res_id is None:
                    raise ValhallaRoutingError(
                        "Point to point route probe call cap exceeded: limit of 3 attempts per execution reached",
                        status_code=429,
                        error_code=429,
                        endpoint="/route",
                    )
                return res_id

            # In-memory test / standalone fallback budget tracking
            if exec_model is not None:
                if exec_model.deadline_at and datetime.now(UTC) >= exec_model.deadline_at:
                    raise ExecutionDeadlineExceededError(
                        f"Execution deadline {exec_model.deadline_at.isoformat()} exceeded before probe"
                    )
                b = dict(exec_model.budget or {})
                rf = dict(b.get("route_fallback") or {})
                spent = int(rf.get("spent_attempts", 0))
                if spent >= 3:
                    raise ValhallaRoutingError(
                        "Point to point route probe call cap exceeded: limit of 3 attempts per execution reached",
                        status_code=429,
                        error_code=429,
                        endpoint="/route",
                    )
                rf["spent_attempts"] = spent + 1
                rf["remaining_attempts"] = max(0, 3 - (spent + 1))
                b["route_fallback"] = rf
                exec_model.budget = b
                flag_modified(exec_model, "budget")
                return f"mem:{spent + 1}"

            curr = self._cache_attempt_counts.get(cache_id, 0)
            if curr >= 3:
                raise ValhallaRoutingError(
                    "Point to point route probe call cap exceeded: limit of 3 attempts per execution reached",
                    status_code=429,
                    error_code=429,
                    endpoint="/route",
                )
            self._cache_attempt_counts[cache_id] = curr + 1
            return f"cache:{curr + 1}"

        async def _on_after_attempt(
            res_id: str | None,
            success: bool,
            dispatched: bool,
            outcome: str | None,
        ) -> None:
            if use_db_budget and execution_id is not None and res_id is not None:
                await reconcile_route_fallback_attempt(
                    self._session,
                    execution_id=execution_id,
                    reservation_id=res_id,
                    success=success,
                    task_id=task_id,
                    attempt=task_attempt,
                    lease_token=lease_token,
                    dispatched=dispatched,
                    outcome=outcome,
                )

        is_mock_client = hasattr(client, "assert_called") or hasattr(
            getattr(client, "route", None), "assert_called"
        )
        route_sig_params = inspect.signature(client.route).parameters
        supports_hooks = (
            isinstance(client, ValhallaClient) or "_on_before_attempt" in route_sig_params
        ) and not is_mock_client

        try:
            if supports_hooks:
                route_res = await client.route(
                    locations=locations,
                    mode=mode,
                    exclude_polygons=exclude_polygons,
                    _on_before_attempt=_on_before_attempt,
                    _on_after_attempt=_on_after_attempt,
                )
            else:
                res_id = await _on_before_attempt()
                try:
                    route_res = await client.route(
                        locations=locations,
                        mode=mode,
                        exclude_polygons=exclude_polygons,
                    )
                except ValhallaRoutingError as v_err:
                    await _on_after_attempt(res_id, False, True, type(v_err).__name__)
                    raise
                except Exception as exc:
                    await _on_after_attempt(res_id, False, True, type(exc).__name__)
                    raise
                else:
                    await _on_after_attempt(res_id, True, True, "SUCCESS")

            if not isinstance(route_res, ValhallaRouteResult) and not (
                hasattr(route_res, "total_travel_seconds")
                and isinstance(getattr(route_res, "total_travel_seconds", None), int)
            ):
                raise ValhallaRoutingError(
                    "Point to point route probe failed: provider returned invalid route result",
                    status_code=400,
                    error_code=154,
                    endpoint="/route",
                )
            typed_res: ValhallaRouteResult = (
                route_res
                if isinstance(route_res, ValhallaRouteResult)
                else ValhallaRouteResult.model_validate(route_res)
            )
            cache[probe_key] = typed_res
            await self._persist_route_probe_entry(
                execution_id=execution_id,
                exec_model=exec_model,
                probe_key=probe_key,
                payload={
                    "status": "SUCCESS",
                    "result": typed_res.model_dump(mode="json"),
                },
            )
            return typed_res
        except ValhallaRoutingError as err:
            if not (err.status_code == 429 and err.error_code == 429):
                is_deterministic_4xx = (
                    err.status_code is not None and 400 <= err.status_code < 500
                )
                if is_deterministic_4xx:
                    cache[probe_key] = err
                    await self._persist_route_probe_entry(
                        execution_id=execution_id,
                        exec_model=exec_model,
                        probe_key=probe_key,
                        payload={
                            "status": "FAILED",
                            "error_message": str(err),
                            "status_code": err.status_code,
                            "error_code": err.error_code,
                            "endpoint": err.endpoint or "/route",
                        },
                    )
            raise

    async def _probe_route_point_to_point(
        self,
        from_loc: Point,
        to_loc: Point,
        *,
        mode: TransportMode,
        client: Any,
        exclude_polygons: list[list[list[float]]] | None = None,
        execution_id: uuid.UUID | None = None,
        exec_model: ExecutionModel | None = None,
        probe_cache: dict[str, ValhallaRouteResult | Exception] | None = None,
        task_id: uuid.UUID | None = None,
        task_attempt: int | None = None,
        lease_token: uuid.UUID | None = None,
    ) -> ValhallaRouteResult:
        """Executes point to point /route probe with durable execution-wide cap and deduplication."""
        return await self._probe_route_waypoints(
            [from_loc, to_loc],
            mode=mode,
            client=client,
            exclude_polygons=exclude_polygons,
            execution_id=execution_id,
            exec_model=exec_model,
            probe_cache=probe_cache,
            task_id=task_id,
            task_attempt=task_attempt,
            lease_token=lease_token,
        )

    async def execute_task(
        self,
        message: dict[str, Any] | RouteOptimizerDispatchEnvelope,
    ) -> tuple[uuid.UUID | None, RouteOptimizerWorkerOutput | dict[str, Any]]:
        """Claims and executes a route optimization task under lease fencing (AC-9)."""
        self._execution_probe_cache = {}
        self._cache_attempt_counts = {}
        # 1. Normalize message and claim task under parent-first lease fencing
        if isinstance(message, RouteOptimizerDispatchEnvelope):
            envelope = message
        else:
            envelope = parse_optimizer_dispatch_message(message)

        if envelope.worker.upper() not in ("OPTIMIZER", "ROUTE_OPTIMIZER"):
            raise ValueError(f"Unsupported worker: {envelope.worker}, expected OPTIMIZER")

        if envelope.action not in ("solve", "replan"):
            raise ValueError(f"Unsupported action: {envelope.action}, expected 'solve' or 'replan'")

        task_msg = envelope.to_task_queue_message()

        claim_result: TaskClaimResult = await self._fencing_service.claim_task(
            message=task_msg,
            worker=envelope.worker,
            action=envelope.action,
            expected_context_version=envelope.context_version,
            lease_seconds=120,
        )

        # 2. Check parent execution status (cancellation barrier & deadline check)
        exec_stmt = select(ExecutionModel).where(ExecutionModel.id == claim_result.execution_id)
        exec_res = await self._session.execute(exec_stmt)
        exec_model = exec_res.scalar_one_or_none()

        now = datetime.now(UTC)
        if exec_model is not None:
            if exec_model.status == "CANCELLED" or exec_model.cancelled_at is not None:
                logger.info(
                    "Parent execution %s is cancelled; halting route optimizer task",
                    claim_result.execution_id,
                )
                raise ParentCancelledError(
                    f"Parent execution {claim_result.execution_id} was cancelled"
                )

            if exec_model.deadline_at and now >= exec_model.deadline_at:
                logger.warning(
                    "Execution deadline exceeded for %s",
                    claim_result.execution_id,
                )
                raise ExecutionDeadlineExceededError(
                    f"Execution deadline {exec_model.deadline_at.isoformat()} exceeded at {now.isoformat()}"
                )

        owner_id = exec_model.owner_id if exec_model is not None else None
        trip_id = exec_model.trip_id if exec_model is not None else None

        # 3. Extract payload data
        payload_data = claim_result.payload_ref
        if "payload" in payload_data and isinstance(payload_data["payload"], dict):
            payload_data = payload_data["payload"]

        # 4. Execute solve or replan
        solved_route: OptimizedRoute
        persisted_route_model: RouteModel

        restrictions = await self._load_active_restrictions()

        if envelope.action == "replan":
            route_id_raw = (
                payload_data.get("route_id")
                or payload_data.get("previous_route_id")
                or payload_data.get("id")
            )
            if not route_id_raw:
                raise ValueError("Replan task payload missing required route_id")

            route_id = uuid.UUID(str(route_id_raw))
            replan_req = RouteReplanRequest.model_validate(payload_data)

            persisted_route_model = await self._replan_service.replan_and_persist(
                route_id=route_id,
                owner_id=owner_id or uuid.uuid4(),
                request=replan_req,
                route_repo=self._route_repo,
                restrictions=restrictions,
                execution_id=envelope.execution_id,
            )
            solved_route = persisted_route_model.to_optimized_route()
        else:
            # Action: solve
            route_req = RouteRequest.model_validate(payload_data)

            # Reconcile owner_id and trip_id with parent execution
            effective_owner_id = owner_id or route_req.owner_id or uuid.uuid4()
            effective_trip_id = trip_id or route_req.trip_id

            if route_req.owner_id is None:
                route_req = route_req.model_copy(
                    update={
                        "owner_id": effective_owner_id,
                        "execution_id": claim_result.execution_id,
                        "trip_id": effective_trip_id,
                    }
                )

            is_driving = route_req.transport_mode in (
                TransportMode.CAR_PETROL,
                TransportMode.MOTORCYCLE_PETROL,
            )
            has_pedestrian_stops = any(
                s.is_pedestrian_only or s.vehicle_restricted for s in route_req.stops
            )

            is_travel_workflow = exec_model is not None and exec_model.workflow == "TRAVEL"

            task_probe_cache: dict[str, ValhallaRouteResult | Exception] = {}
            try:
                if is_driving and has_pedestrian_stops:
                    solved_route = await self._multimodal_service.plan_multimodal_route(
                        route_req, restrictions=restrictions
                    )
                else:
                    solved_route = await self._solve_route_with_provider(
                        route_req,
                        restrictions=restrictions,
                        execution_id=claim_result.execution_id,
                        exec_model=exec_model,
                        probe_cache=task_probe_cache,
                        task_id=claim_result.task_id,
                        task_attempt=claim_result.attempt,
                        lease_token=claim_result.lease_token,
                    )
            except ValhallaRoutingError as exc:
                is_matrix_limit = is_valhalla_matrix_distance_limit_error(exc)
                is_route_limit = is_valhalla_route_distance_limit_error(exc)
                is_code_154 = exc.status_code == 400 and exc.error_code == 154
                is_budget_cap = exc.status_code == 429 and exc.error_code == 429
                if is_travel_workflow and (
                    is_matrix_limit or is_route_limit or is_code_154 or is_budget_cap
                ):
                    endpoint_name = exc.endpoint or (
                        "/route" if (is_route_limit or is_budget_cap) else "/sources_to_targets"
                    )
                    if is_budget_cap:
                        safe_reason = (
                            f"Valhalla fallback attempt budget (3 attempts) exhausted on {endpoint_name}: "
                            "cannot supply remaining route geometry"
                        )
                        err_code = 429
                        stat_code = 429
                    else:
                        threshold_str = (
                            "1500 km"
                            if "/route" in endpoint_name
                            else "provider matrix threshold (150 km)"
                        )
                        safe_reason = (
                            f"Valhalla route distance limit exceeded (HTTP 400, error_code 154 on {endpoint_name}): "
                            f"leg distance exceeds provider threshold ({threshold_str})"
                        )
                        err_code = 154
                        stat_code = 400
                    logger.warning(
                        "Valhalla fallback degraded for execution %s (status=%s code=%s on %s): "
                        "completing task under lease with degraded ROUTING_UNAVAILABLE output",
                        claim_result.execution_id,
                        stat_code,
                        err_code,
                        endpoint_name,
                    )
                    degraded_output: dict[str, Any] = {
                        "worker": envelope.worker,
                        "action": envelope.action,
                        "status": "FAILED",
                        "routing_status": "ROUTING_UNAVAILABLE",
                        "error": {
                            "reason": safe_reason,
                            "error_code": err_code,
                            "status_code": stat_code,
                            "endpoint": endpoint_name,
                        },
                        "diagnostics": [
                            "Road routing engine unavailable; stops preserved without driving schedule or route polyline",
                            safe_reason,
                        ],
                    }

                    # Complete task under lease fencing atomically with degraded outcome
                    await self._fencing_service.complete_task(
                        execution_id=claim_result.execution_id,
                        task_id=claim_result.task_id,
                        lease_token=claim_result.lease_token,
                        output=degraded_output,
                        expected_context_version=claim_result.context_version,
                    )

                    # Record job timeline event
                    await record_job_event(
                        self._session,
                        execution_id=claim_result.execution_id,
                        event_type="ROUTE_OPTIMIZED",
                        safe_payload={
                            "status": "ROUTING_UNAVAILABLE",
                            "routing_status": "ROUTING_UNAVAILABLE",
                            "reason": safe_reason,
                        },
                    )

                    return None, degraded_output
                raise

            # Apply closure avoidance and spatial revalidation
            if restrictions:
                solved_route = self._closure_service.apply_and_revalidate(
                    solved_route, restrictions
                )

            if solved_route.owner_id is None or solved_route.owner_id != effective_owner_id:
                solved_route = solved_route.model_copy(update={"owner_id": effective_owner_id})

            if solved_route.status == RouteStatus.INFEASIBLE:
                infeasible_reason = (
                    "MANDATORY_STOP_WINDOW_BREACH"
                    if any(
                        "cannot complete service" in d or "window" in d.lower()
                        for d in solved_route.diagnostics
                    )
                    else (
                        solved_route.diagnostics[0][:64]
                        if solved_route.diagnostics
                        else "MANDATORY_CONSTRAINT_INFEASIBLE"
                    )
                )
                existing_stops_by_name = {
                    s.name: s for s in (list(solved_route.stops) + list(solved_route.dropped_stops))
                }
                infeasible_stops = []
                for idx, s in enumerate(route_req.stops):
                    prev_s = existing_stops_by_name.get(s.name)
                    s_id = getattr(prev_s, "id", None) or getattr(s, "id", None) or uuid.uuid4()
                    infeasible_stops.append(
                        ScheduledStopOutput(
                            id=s_id,
                            stop_id=getattr(s, "id", None) or s_id,
                            place_id=getattr(s, "place_id", None),
                            stop_order=idx + 1,
                            name=s.name,
                            location=s.location,
                            stop_type=getattr(s, "stop_type", StopType.DESTINATION),
                            is_must_visit=getattr(s, "is_must_visit", True),
                            is_completed=False,
                            is_skipped=True,
                            skip_reason=getattr(prev_s, "skip_reason", None) or infeasible_reason,
                            window_start=getattr(s, "window_start", None),
                            window_end=getattr(s, "window_end", None),
                            dwell_seconds=getattr(s, "dwell_seconds", 0),
                        )
                    )
                solved_route = solved_route.model_copy(
                    update={
                        "stops": [],
                        "dropped_stops": infeasible_stops,
                        "legs": [],
                    }
                )

            if route_req.route_id is not None:
                solved_route = solved_route.model_copy(update={"id": route_req.route_id})

            # Persist solved route
            persisted_route_model = await self._route_repo.save_route(
                solved_route,
                owner_id=effective_owner_id,
                trip_id=effective_trip_id,
                execution_id=claim_result.execution_id,
            )

        # 5. Assemble worker output
        worker_output = RouteOptimizerWorkerOutput(
            status=solved_route.status,
            route_id=persisted_route_model.id,
            route_version=persisted_route_model.route_version,
            trip_id=persisted_route_model.trip_id,
            execution_id=claim_result.execution_id,
            total_distance_m=persisted_route_model.total_distance_m,
            total_travel_seconds=persisted_route_model.total_travel_seconds,
            stops_count=len(persisted_route_model.stops),
            legs_count=len(persisted_route_model.legs),
            fuel_amount=persisted_route_model.fuel_amount,
            fuel_currency=persisted_route_model.fuel_currency,
            warnings=list(solved_route.warnings),
            diagnostics=list(solved_route.diagnostics),
        )

        # 6. Complete task under lease fencing atomically
        await self._fencing_service.complete_task(
            execution_id=claim_result.execution_id,
            task_id=claim_result.task_id,
            lease_token=claim_result.lease_token,
            output=worker_output.model_dump(mode="json"),
            expected_context_version=claim_result.context_version,
        )

        # 7. Record job timeline event
        await record_job_event(
            self._session,
            execution_id=claim_result.execution_id,
            event_type="ROUTE_OPTIMIZED",
            safe_payload={
                "status": solved_route.status.value,
                "route_id": str(persisted_route_model.id),
                "route_version": persisted_route_model.route_version,
                "total_distance_m": persisted_route_model.total_distance_m,
                "total_travel_seconds": persisted_route_model.total_travel_seconds,
            },
        )

        return persisted_route_model.id, worker_output

    async def _solve_route_with_provider(
        self,
        request: RouteRequest,
        restrictions: list[RestrictionClaim] | None = None,
        execution_id: uuid.UUID | None = None,
        exec_model: ExecutionModel | None = None,
        probe_cache: dict[str, ValhallaRouteResult | Exception] | None = None,
        task_id: uuid.UUID | None = None,
        task_attempt: int | None = None,
        lease_token: uuid.UUID | None = None,
    ) -> OptimizedRoute:
        """Solves a route request querying ValhallaClient (or injected fixture) for matrices,
        then hydrates actual selected leg road/walking geometries with closure exclusions."""
        client = self._valhalla_client
        if client is None:
            client = ValhallaClient()

        if (
            hasattr(client, "base_url")
            and not client.base_url
            and not hasattr(client, "canned_matrix")
        ):
            raise ValhallaConnectionError(
                "VALHALLA_ENDPOINT_URL is not configured; cannot query external routing service"
            )

        stops = request.stops
        locs = [s.location for s in stops]
        probed_routes: dict[str, ValhallaRouteResult] = {}

        leg_polys: list[list[list[float]]] = []
        if restrictions:
            active_restrs = [r for r in restrictions if r.is_active and r.is_verified]
            leg_polys, _, diags = self._closure_service.extract_valhalla_exclude_polygons(
                active_restrs
            )
            if diags:
                raise UnsupportedRestrictionError(diags[0])

        polys_key = (
            str(sorted([sorted([tuple(p) for p in ring]) for ring in leg_polys]))
            if leg_polys
            else ""
        )

        matrix: MatrixResponse | None = None
        matrix_exc: ValhallaRoutingError | None = None

        is_travel_workflow = exec_model is not None and exec_model.workflow == "TRAVEL"
        matrix_sig_params = inspect.signature(client.sources_to_targets).parameters
        disable_implicit_cell_fallback = "allow_point_to_point_fallback" in matrix_sig_params

        try:
            if disable_implicit_cell_fallback:
                matrix = await client.sources_to_targets(
                    origins=locs,
                    destinations=locs,
                    mode=request.transport_mode,
                    allow_point_to_point_fallback=False,
                )
            else:
                matrix = await client.sources_to_targets(
                    origins=locs,
                    destinations=locs,
                    mode=request.transport_mode,
                )
        except ValhallaRoutingError as exc:
            if (
                is_travel_workflow
                and is_valhalla_matrix_distance_limit_error(exc)
                and len(stops) >= 2
            ):
                matrix_exc = exc
            else:
                raise

        durations_s: list[list[int | None]]
        distances_m: list[list[int | None]]
        is_recovery_path = False
        cluster_hub_stop: RouteStopInput | None = None

        if matrix_exc is not None:
            is_recovery_path = True
            # Matrix distance limit exceeded: attempt bounded /route recovery on mandatory intercity transfer
            logger.info(
                "Valhalla matrix distance limit exceeded on /sources_to_targets; "
                "attempting point to point /route fallback for mandatory intercity transfer"
            )
            origin_loc = stops[0].location
            dest_loc = stops[1].location

            intercity_res = await self._probe_route_point_to_point(
                origin_loc,
                dest_loc,
                mode=request.transport_mode,
                client=client,
                exclude_polygons=leg_polys or None,
                execution_id=execution_id,
                exec_model=exec_model,
                probe_cache=probe_cache,
                task_id=task_id,
                task_attempt=task_attempt,
                lease_token=lease_token,
            )
            probe_key_01 = self._build_probe_key(
                [origin_loc, dest_loc], request.transport_mode, polys_key
            )
            probed_routes[probe_key_01] = intercity_res

            return_res: ValhallaRouteResult | None = None
            if request.is_round_trip:
                return_res = await self._probe_route_point_to_point(
                    dest_loc,
                    origin_loc,
                    mode=request.transport_mode,
                    client=client,
                    exclude_polygons=leg_polys or None,
                    execution_id=execution_id,
                    exec_model=exec_model,
                    probe_cache=probe_cache,
                    task_id=task_id,
                    task_attempt=task_attempt,
                    lease_token=lease_token,
                )
                probe_key_10 = self._build_probe_key(
                    [dest_loc, origin_loc], request.transport_mode, polys_key
                )
                probed_routes[probe_key_10] = return_res

            if len(stops) == 2:
                durations_s = [
                    [0, intercity_res.total_travel_seconds],
                    [return_res.total_travel_seconds if return_res else None, 0],
                ]
                distances_m = [
                    [0, intercity_res.total_distance_m],
                    [return_res.total_distance_m if return_res else None, 0],
                ]
            else:
                cluster_hub_stop = stops[1]
                # Multi stop with local stops at destination
                local_locs = [s.location for s in stops[1:]]
                if disable_implicit_cell_fallback:
                    local_matrix = await client.sources_to_targets(
                        origins=local_locs,
                        destinations=local_locs,
                        mode=request.transport_mode,
                        allow_point_to_point_fallback=False,
                    )
                else:
                    local_matrix = await client.sources_to_targets(
                        origins=local_locs,
                        destinations=local_locs,
                        mode=request.transport_mode,
                    )
                n = len(stops)
                durations_s = [[None for _ in range(n)] for _ in range(n)]
                distances_m = [[None for _ in range(n)] for _ in range(n)]

                # Origin (0) to Destination Entry (1)
                durations_s[0][0] = 0
                distances_m[0][0] = 0
                durations_s[0][1] = intercity_res.total_travel_seconds
                distances_m[0][1] = intercity_res.total_distance_m

                # Local cluster transitions
                for li in range(len(local_locs)):
                    for lj in range(len(local_locs)):
                        durations_s[li + 1][lj + 1] = local_matrix.durations_s[li][lj]
                        distances_m[li + 1][lj + 1] = local_matrix.distances_m[li][lj]

                # Return leg to Origin if round trip
                if return_res is not None:
                    durations_s[1][0] = return_res.total_travel_seconds
                    distances_m[1][0] = return_res.total_distance_m
                    for k in range(2, n):
                        dur_to_hub = durations_s[k][1]
                        dist_to_hub = distances_m[k][1]
                        if dur_to_hub is not None and return_res.total_travel_seconds is not None:
                            durations_s[k][0] = dur_to_hub + return_res.total_travel_seconds
                        if dist_to_hub is not None and return_res.total_distance_m is not None:
                            distances_m[k][0] = dist_to_hub + return_res.total_distance_m

            initial_route = self._solver.solve(request, durations_s, distances_m)
        else:
            assert matrix is not None
            durations_s = [list(r) for r in matrix.durations_s]
            distances_m = [list(r) for r in matrix.distances_m]
            n = len(stops)

            for i in range(n):
                for j in range(n):
                    if i != j and stops[i].is_must_visit and stops[j].is_must_visit:
                        if durations_s[i][j] is None or distances_m[i][j] is None:
                            is_recovery_path = True
                            try:
                                probe_res = await self._probe_route_point_to_point(
                                    stops[i].location,
                                    stops[j].location,
                                    mode=request.transport_mode,
                                    client=client,
                                    exclude_polygons=leg_polys or None,
                                    execution_id=execution_id,
                                    exec_model=exec_model,
                                    probe_cache=probe_cache,
                                    task_id=task_id,
                                    task_attempt=task_attempt,
                                    lease_token=lease_token,
                                )
                                durations_s[i][j] = probe_res.total_travel_seconds
                                distances_m[i][j] = probe_res.total_distance_m
                                p_key = self._build_probe_key(
                                    [stops[i].location, stops[j].location],
                                    request.transport_mode,
                                    polys_key,
                                )
                                probed_routes[p_key] = probe_res
                            except (
                                LeaseFencingError,
                                ExecutionDeadlineExceededError,
                                ParentCancelledError,
                                ParentTerminalError,
                                ExecutionNotFoundError,
                            ):
                                raise
                            except ValhallaRoutingError as probe_err:
                                if probe_err.status_code == 429 or probe_err.error_code in (
                                    429,
                                    154,
                                ):
                                    raise
                                logger.warning(
                                    "Fallback probe failed for required pair %s -> %s: %s",
                                    stops[i].name,
                                    stops[j].name,
                                    probe_err,
                                )
                            except Exception as probe_err:
                                logger.warning(
                                    "Fallback probe failed for required pair %s -> %s: %s",
                                    stops[i].name,
                                    stops[j].name,
                                    probe_err,
                                )

            initial_route = self._solver.solve(request, durations_s, distances_m)

        if (
            initial_route.status not in (RouteStatus.FEASIBLE, RouteStatus.PARTIAL)
            or not initial_route.legs
        ):
            return initial_route

        if hasattr(client, "route"):
            return await self._hydrate_leg_geometries_and_revalidate_schedule(
                initial_route=initial_route,
                request=request,
                client=client,
                restrictions=restrictions,
                probed_routes=probed_routes,
                execution_id=execution_id,
                exec_model=exec_model,
                probe_cache=probe_cache,
                task_id=task_id,
                task_attempt=task_attempt,
                lease_token=lease_token,
                is_recovery_path=is_recovery_path,
                cluster_hub_stop=cluster_hub_stop,
            )
        return initial_route

    async def _hydrate_leg_geometries_and_revalidate_schedule(
        self,
        *,
        initial_route: OptimizedRoute,
        request: RouteRequest,
        client: Any,
        restrictions: list[RestrictionClaim] | None = None,
        probed_routes: dict[str, ValhallaRouteResult] | None = None,
        execution_id: uuid.UUID | None = None,
        exec_model: ExecutionModel | None = None,
        probe_cache: dict[str, ValhallaRouteResult | Exception] | None = None,
        task_id: uuid.UUID | None = None,
        task_attempt: int | None = None,
        lease_token: uuid.UUID | None = None,
        is_recovery_path: bool = False,
        cluster_hub_stop: RouteStopInput | None = None,
    ) -> OptimizedRoute:
        """Hydrates real provider road/walking geometries for selected legs, applies
        supported closure exclusions, and revalidates schedule consistency.
        """
        leg_polys: list[list[list[float]]] = []
        if restrictions:
            active_restrs = [r for r in restrictions if r.is_active and r.is_verified]
            leg_polys, _, diags = self._closure_service.extract_valhalla_exclude_polygons(
                active_restrs
            )
            if diags:
                raise UnsupportedRestrictionError(diags[0])

        polys_key = (
            str(sorted([sorted([tuple(p) for p in ring]) for ring in leg_polys]))
            if leg_polys
            else ""
        )

        stops_by_id = {s.id: s for s in initial_route.stops}
        stops_by_stop_id = {s.stop_id: s for s in initial_route.stops if s.stop_id is not None}
        input_stops_by_id = {s.id: s for s in request.stops}

        local_probed_routes = dict(probed_routes or {})

        origin_stop = initial_route.stops[0]

        # Pre-hydrate Hub -> Local -> Hub in 1 multi-waypoint /route call when returning via cluster hub
        if (
            cluster_hub_stop is not None
            and request.is_round_trip
            and len(initial_route.legs) >= 2
        ):
            for idx in range(len(initial_route.legs) - 1):
                leg_a = initial_route.legs[idx]
                leg_b = initial_route.legs[idx + 1]
                a_from = stops_by_id.get(leg_a.from_stop_id) or stops_by_stop_id.get(
                    leg_a.from_stop_id
                )
                a_to = stops_by_id.get(leg_a.to_stop_id) or stops_by_stop_id.get(leg_a.to_stop_id)
                b_from = stops_by_id.get(leg_b.from_stop_id) or stops_by_stop_id.get(
                    leg_b.from_stop_id
                )
                b_to = stops_by_id.get(leg_b.to_stop_id) or stops_by_stop_id.get(leg_b.to_stop_id)
                if (
                    a_from is not None
                    and a_to is not None
                    and b_from is not None
                    and b_to is not None
                    and a_to.id == b_from.id
                    and b_to.id == origin_stop.id
                    and (
                        a_from.id == cluster_hub_stop.id
                        or a_from.stop_id == cluster_hub_stop.id
                    )
                    and a_to.id != cluster_hub_stop.id
                ):
                    key_fwd = self._build_probe_key(
                        [cluster_hub_stop.location, a_to.location],
                        leg_a.transport_mode,
                        polys_key,
                    )
                    key_rev = self._build_probe_key(
                        [a_to.location, cluster_hub_stop.location],
                        leg_b.transport_mode,
                        polys_key,
                    )
                    if key_fwd not in local_probed_routes or key_rev not in local_probed_routes:
                        multi_res = await self._probe_route_waypoints(
                            [cluster_hub_stop.location, a_to.location, cluster_hub_stop.location],
                            mode=leg_a.transport_mode,
                            client=client,
                            exclude_polygons=leg_polys or None,
                            execution_id=execution_id,
                            exec_model=exec_model,
                            probe_cache=probe_cache,
                            task_id=task_id,
                            task_attempt=task_attempt,
                            lease_token=lease_token,
                        )
                        if len(multi_res.legs) >= 2:
                            local_probed_routes[key_fwd] = ValhallaRouteResult(
                                total_distance_m=multi_res.legs[0].distance_m,
                                total_travel_seconds=multi_res.legs[0].travel_seconds,
                                legs=[multi_res.legs[0]],
                                geometry=multi_res.legs[0].geometry,
                            )
                            local_probed_routes[key_rev] = ValhallaRouteResult(
                                total_distance_m=multi_res.legs[1].distance_m,
                                total_travel_seconds=multi_res.legs[1].travel_seconds,
                                legs=[multi_res.legs[1]],
                                geometry=multi_res.legs[1].geometry,
                            )

        current_time = request.departure_at
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        hydrated_legs: list[RouteLegOutput] = []
        updated_stops: list[ScheduledStopOutput] = []

        total_distance_m: int = 0
        total_travel_s: int = 0
        total_dwell_s: int = 0
        total_wait_s: int = 0

        origin_dep = current_time + timedelta(seconds=origin_stop.dwell_seconds)
        updated_stops.append(
            origin_stop.model_copy(
                update={
                    "planned_arrival_at": current_time,
                    "planned_departure_at": origin_dep,
                    "wait_seconds": 0,
                }
            )
        )
        current_time = origin_dep
        total_dwell_s += origin_stop.dwell_seconds

        for leg in initial_route.legs:
            from_stop = stops_by_id.get(leg.from_stop_id) or stops_by_stop_id.get(leg.from_stop_id)
            to_stop = stops_by_id.get(leg.to_stop_id) or stops_by_stop_id.get(leg.to_stop_id)
            if from_stop is None or to_stop is None:
                continue

            is_cluster_hub_return = (
                cluster_hub_stop is not None
                and request.is_round_trip
                and to_stop.id == origin_stop.id
                and leg.leg_order == len(initial_route.legs) - 1
                and from_stop.id != cluster_hub_stop.id
                and from_stop.stop_id != cluster_hub_stop.id
            )

            if is_cluster_hub_return:
                assert cluster_hub_stop is not None
                key_to_hub = self._build_probe_key(
                    [from_stop.location, cluster_hub_stop.location],
                    leg.transport_mode,
                    polys_key,
                )
                key_hub_to_orig = self._build_probe_key(
                    [cluster_hub_stop.location, origin_stop.location],
                    leg.transport_mode,
                    polys_key,
                )
                seg_to_hub = local_probed_routes.get(key_to_hub)
                if seg_to_hub is None:
                    seg_to_hub = await self._probe_route_waypoints(
                        [from_stop.location, cluster_hub_stop.location],
                        mode=leg.transport_mode,
                        client=client,
                        exclude_polygons=leg_polys or None,
                        execution_id=execution_id,
                        exec_model=exec_model,
                        probe_cache=probe_cache,
                        task_id=task_id,
                        task_attempt=task_attempt,
                        lease_token=lease_token,
                    )
                    local_probed_routes[key_to_hub] = seg_to_hub
                seg_hub_to_orig = local_probed_routes.get(key_hub_to_orig)
                if seg_hub_to_orig is None:
                    seg_hub_to_orig = await self._probe_route_waypoints(
                        [cluster_hub_stop.location, origin_stop.location],
                        mode=leg.transport_mode,
                        client=client,
                        exclude_polygons=leg_polys or None,
                        execution_id=execution_id,
                        exec_model=exec_model,
                        probe_cache=probe_cache,
                        task_id=task_id,
                        task_attempt=task_attempt,
                        lease_token=lease_token,
                    )
                    local_probed_routes[key_hub_to_orig] = seg_hub_to_orig

                geom1 = seg_to_hub.legs[0].geometry if seg_to_hub.legs else seg_to_hub.geometry
                geom2 = (
                    seg_hub_to_orig.legs[0].geometry
                    if seg_hub_to_orig.legs
                    else seg_hub_to_orig.geometry
                )
                coords1 = list(geom1.get("coordinates") or [])
                coords2 = list(geom2.get("coordinates") or [])
                stitched_coords = coords1 + (
                    coords2[1:] if coords1 and coords2 and coords1[-1] == coords2[0] else coords2
                )
                leg_dist = seg_to_hub.total_distance_m + seg_hub_to_orig.total_distance_m
                leg_dur = seg_to_hub.total_travel_seconds + seg_hub_to_orig.total_travel_seconds
                leg_geom = {"type": "LineString", "coordinates": stitched_coords}
            else:
                probe_key = self._build_probe_key(
                    [from_stop.location, to_stop.location],
                    leg.transport_mode,
                    polys_key,
                )
                if probe_key in local_probed_routes:
                    route_res = local_probed_routes[probe_key]
                elif is_recovery_path:
                    route_res = await self._probe_route_waypoints(
                        [from_stop.location, to_stop.location],
                        mode=leg.transport_mode,
                        client=client,
                        exclude_polygons=leg_polys or None,
                        execution_id=execution_id,
                        exec_model=exec_model,
                        probe_cache=probe_cache,
                        task_id=task_id,
                        task_attempt=task_attempt,
                        lease_token=lease_token,
                    )
                    local_probed_routes[probe_key] = route_res
                else:
                    route_res = await client.route(
                        locations=[from_stop.location, to_stop.location],
                        mode=leg.transport_mode,
                        exclude_polygons=leg_polys or None,
                    )
                leg_dist = route_res.total_distance_m
                leg_dur = route_res.total_travel_seconds
                leg_geom = route_res.legs[0].geometry if route_res.legs else route_res.geometry

            leg_dep = current_time
            arr_time = leg_dep + timedelta(seconds=leg_dur)

            is_return_to_origin = (
                request.is_round_trip
                and to_stop.id == origin_stop.id
                and leg.leg_order == len(initial_route.legs) - 1
            )

            wait_s = 0
            if not is_return_to_origin:
                target_input = input_stops_by_id.get(to_stop.id) or (
                    input_stops_by_id.get(to_stop.stop_id) if to_stop.stop_id is not None else None
                )
                dwell = target_input.dwell_seconds if target_input else to_stop.dwell_seconds
                w_start: datetime | None = None
                w_end: datetime | None = None
                if target_input and target_input.daily_windows:
                    active_dw = select_active_daily_window(
                        target_input.daily_windows,
                        arr_time,
                        dwell,
                        preferred_time=_ensure_utc(to_stop.planned_arrival_at)
                        if to_stop.planned_arrival_at
                        else None,
                    )
                    if active_dw:
                        w_start, w_end = active_dw
                elif target_input:
                    w_start = _ensure_utc(target_input.window_start) if target_input.window_start else None
                    w_end = _ensure_utc(target_input.window_end) if target_input.window_end else None

                if w_start and arr_time < w_start:
                    wait_s = int((w_start - arr_time).total_seconds())

                service_start = arr_time + timedelta(seconds=wait_s)
                target_dep = service_start + timedelta(seconds=dwell)

                # Hard service window revalidation: dwell completion before closing
                if target_input and w_end and target_dep > w_end:
                        if target_input.is_must_visit:
                            return initial_route.model_copy(
                                update={
                                    "status": RouteStatus.INFEASIBLE,
                                    "solver_status": SolverStatus.INFEASIBLE,
                                    "actual_return_at": target_dep,
                                    "stops": updated_stops,
                                    "legs": hydrated_legs,
                                    "diagnostics": initial_route.diagnostics
                                    + [
                                        f"SCHEDULE_INFEASIBLE: Service at mandatory stop '{to_stop.name}' finishes at {target_dep.isoformat()}, exceeding closing time {w_end.isoformat()}"
                                    ],
                                    "warnings": initial_route.warnings
                                    + [
                                        f"Service window breached at mandatory stop '{to_stop.name}'"
                                    ],
                                }
                            )
                        else:
                            # Optional stop breached closing time: drop and re-solve with remaining stops
                            reduced_stops = [s for s in request.stops if s.id != target_input.id]
                            reduced_request = request.model_copy(update={"stops": reduced_stops})
                            reduced_route = await self._solve_route_with_provider(
                                reduced_request,
                                restrictions=restrictions,
                                execution_id=execution_id,
                                exec_model=exec_model,
                                probe_cache=probe_cache,
                                task_id=task_id,
                                task_attempt=task_attempt,
                                lease_token=lease_token,
                            )
                            dropped_stop = ScheduledStopOutput(
                                id=target_input.id,
                                stop_id=target_input.id,
                                place_id=target_input.place_id,
                                stop_order=len(reduced_route.stops),
                                name=target_input.name,
                                location=target_input.location,
                                stop_type=target_input.stop_type,
                                is_must_visit=False,
                                is_skipped=True,
                                skip_reason="TIME_WINDOW_OR_DROP_PENALTY",
                                window_start=w_start or target_input.window_start,
                                window_end=w_end or target_input.window_end,
                                daily_windows=target_input.daily_windows,
                                dwell_seconds=target_input.dwell_seconds,
                            )
                            final_status = (
                                RouteStatus.PARTIAL
                                if reduced_route.status == RouteStatus.FEASIBLE
                                else reduced_route.status
                            )
                            final_solver_status = (
                                SolverStatus.FEASIBLE
                                if reduced_route.status == RouteStatus.FEASIBLE
                                else reduced_route.solver_status
                            )
                            return reduced_route.model_copy(
                                update={
                                    "status": final_status,
                                    "solver_status": final_solver_status,
                                    "stops": reduced_route.stops + [dropped_stop],
                                    "dropped_stops": reduced_route.dropped_stops + [dropped_stop],
                                    "diagnostics": reduced_route.diagnostics
                                    + [
                                        f"Optional stop '{to_stop.name}' dropped after detour exceeded closing time {w_end.isoformat()}"
                                    ],
                                    "warnings": reduced_route.warnings
                                    + [
                                        f"Optional stop '{to_stop.name}' dropped due to closure detour"
                                    ],
                                }
                            )

                is_overnight_wait = wait_s > 0 and (
                    arr_time.date() < service_start.date() or wait_s >= 4 * 3600
                )
                stop_arrival = service_start if is_overnight_wait else arr_time

                updated_stops.append(
                    to_stop.model_copy(
                        update={
                            "planned_arrival_at": stop_arrival,
                            "planned_departure_at": target_dep,
                            "wait_seconds": wait_s,
                            "dwell_seconds": dwell,
                            "window_start": w_start or to_stop.window_start,
                            "window_end": w_end or to_stop.window_end,
                            "daily_windows": target_input.daily_windows
                            if target_input
                            else to_stop.daily_windows,
                        }
                    )
                )
                total_dwell_s += dwell
                total_wait_s += wait_s
                current_time = target_dep
            else:
                current_time = arr_time

            hydrated_legs.append(
                leg.model_copy(
                    update={
                        "departure_at": leg_dep,
                        "arrival_at": arr_time,
                        "distance_m": leg_dist,
                        "travel_seconds": leg_dur,
                        "geometry": leg_geom,
                    }
                )
            )
            total_distance_m += leg_dist
            total_travel_s += leg_dur

        # Preserve dropped stops in updated_stops
        for s in initial_route.stops:
            if getattr(s, "is_skipped", False) and s.id not in {us.id for us in updated_stops}:
                updated_stops.append(s)

        actual_return_at: datetime | None = current_time if request.is_round_trip else None
        check_deadline_at = actual_return_at if request.is_round_trip else current_time

        if request.return_deadline is not None and check_deadline_at is not None:
            ret_dl = _ensure_utc(request.return_deadline)
            if ret_dl and check_deadline_at > ret_dl:
                return initial_route.model_copy(
                    update={
                        "status": RouteStatus.INFEASIBLE,
                        "solver_status": SolverStatus.INFEASIBLE,
                        "actual_return_at": check_deadline_at,
                        "stops": updated_stops,
                        "legs": hydrated_legs,
                        "diagnostics": initial_route.diagnostics
                        + [
                            f"SCHEDULE_INFEASIBLE: Route completes at {check_deadline_at.isoformat()}, exceeding return deadline {ret_dl.isoformat()}"
                        ],
                        "warnings": initial_route.warnings + ["Return deadline breached"],
                    }
                )

        fuel_profile = request.fuel_profile or VehicleFuelProfile.default_for_mode(
            request.transport_mode
        )
        driving_dist_m = total_distance_m if request.transport_mode != TransportMode.WALKING else 0
        walking_dist_m = total_distance_m if request.transport_mode == TransportMode.WALKING else 0

        driving_dist_km = (Decimal(str(driving_dist_m)) / Decimal("1000")).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )
        fuel_liters = (driving_dist_km / fuel_profile.mileage_km_per_liter).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        fuel_cost = (fuel_liters * fuel_profile.fuel_price_per_liter).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        fuel_basis = (
            f"fuel_{fuel_profile.mode.value} "
            f"({driving_dist_km}km @ {fuel_profile.mileage_km_per_liter}km/l)"
        )
        total_elapsed_s = total_travel_s + total_dwell_s + total_wait_s

        assumptions = list(initial_route.assumptions)
        if total_wait_s >= 4 * 3600 and not any("Overnight waiting" in a for a in assumptions):
            assumptions.append(
                "Overnight waiting interval with unassigned accommodation: lodging coordinates, verified hours, and connecting road geometry are unassigned"
            )

        return initial_route.model_copy(
            update={
                "stops": updated_stops,
                "legs": hydrated_legs,
                "total_distance_m": total_distance_m,
                "total_travel_seconds": total_travel_s,
                "total_dwell_seconds": total_dwell_s,
                "total_wait_seconds": total_wait_s,
                "total_elapsed_seconds": total_elapsed_s,
                "driving_distance_m": driving_dist_m,
                "walking_distance_m": walking_dist_m,
                "fuel_amount": fuel_cost,
                "fuel_currency": fuel_profile.currency,
                "fuel_basis": fuel_basis,
                "assumptions": assumptions,
                "actual_return_at": actual_return_at,
            }
        )

    async def _load_active_restrictions(self) -> list[RestrictionClaim]:
        """Loads active verified road closure claims from PostgreSQL."""
        try:
            sql = text("""
                SELECT id, geographic_scope, typed_value, valid_from, valid_until, availability
                FROM claims
                WHERE category = 'ROAD_CLOSURE' AND verification = 'VERIFIED'
            """)
            res = await self._session.execute(sql)
            rows = res.mappings().all()
            restrictions: list[RestrictionClaim] = []
            for r in rows:
                typed_val = r["typed_value"] if isinstance(r["typed_value"], dict) else {}
                geom = r["geographic_scope"] or typed_val.get("geometry") or {}
                if geom:
                    restrictions.append(
                        RestrictionClaim(
                            id=r["id"],
                            claim_id=r["id"],
                            restriction_type=RestrictionType.ROAD_CLOSURE,
                            geometry=geom,
                            effective_start=r["valid_from"],
                            effective_end=r["valid_until"],
                            is_active=(r["availability"] in ("ACTIVE", "UNKNOWN")),
                            is_verified=True,
                        )
                    )
            return restrictions
        except Exception as exc:
            logger.error("Authoritative road closure restriction lookup failed: %s", exc)
            raise RuntimeError(f"Authoritative restriction lookup failed: {exc}") from exc
