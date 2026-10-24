"""Route optimizer worker service conforming to spec 0005 AC-9.

Executes asynchronous route optimization under lease fencing, validates parent
execution lease fences and cooperative cancellation, computes schedules via
OrToolsRoutingSolver or MultimodalTransitionService, persists routes in PostgreSQL,
and completes tasks atomically under WorkerFencingService.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.routing import (
    OptimizedRoute,
    RestrictionType,
    RouteOptimizerWorkerOutput,
    RouteReplanRequest,
    RouteRequest,
)
from contracts.trips import TransportMode
from functions.orchestrator.execution.fencing import (
    ParentCancelledError,
    TaskClaimResult,
    TaskQueueMessage,
    WorkerFencingService,
)
from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.places.repository import CanonicalPlacesRepository
from functions.orchestrator.routing.models import RouteModel
from functions.orchestrator.routing.repository import RouteRepository
from functions.route_optimizer.closures import ClosureAvoidanceService, RestrictionClaim
from functions.route_optimizer.multimodal import MultimodalTransitionService
from functions.route_optimizer.replan import RouteReplanService
from functions.route_optimizer.solver import OrToolsRoutingSolver
from functions.route_optimizer.valhalla import (
    ValhallaClient,
    ValhallaConnectionError,
)

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
        self._valhalla_client = valhalla_client
        self._closure_service = closure_service or ClosureAvoidanceService()
        self._places_repo = CanonicalPlacesRepository(session)
        self._multimodal_service = multimodal_service or MultimodalTransitionService(
            places_repo=self._places_repo,
            valhalla_client=self._valhalla_client,
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

    async def execute_task(
        self,
        message: dict[str, Any] | RouteOptimizerDispatchEnvelope,
    ) -> tuple[uuid.UUID, RouteOptimizerWorkerOutput]:
        """Claims and executes a route optimization task under lease fencing (AC-9)."""
        # 1. Normalize message and claim task under parent-first lease fencing
        if isinstance(message, RouteOptimizerDispatchEnvelope):
            envelope = message
        else:
            envelope = parse_optimizer_dispatch_message(message)

        if envelope.worker.upper() not in ("OPTIMIZER", "ROUTE_OPTIMIZER"):
            raise ValueError(
                f"Unsupported worker: {envelope.worker}, expected OPTIMIZER"
            )

        if envelope.action not in ("solve", "replan"):
            raise ValueError(
                f"Unsupported action: {envelope.action}, expected 'solve' or 'replan'"
            )

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

            if is_driving and has_pedestrian_stops:
                solved_route = await self._multimodal_service.plan_multimodal_route(route_req)
            else:
                solved_route = await self._solve_route_with_provider(route_req)

            # Apply closure avoidance and spatial revalidation
            if restrictions:
                solved_route = self._closure_service.apply_and_revalidate(
                    solved_route, restrictions
                )

            if solved_route.owner_id is None or solved_route.owner_id != effective_owner_id:
                solved_route = solved_route.model_copy(update={"owner_id": effective_owner_id})

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

    async def _solve_route_with_provider(self, request: RouteRequest) -> OptimizedRoute:
        """Solves a route request querying ValhallaClient (or injected fixture) for matrices."""
        client = self._valhalla_client
        if client is None:
            client = ValhallaClient()

        if hasattr(client, "base_url") and not client.base_url and not hasattr(client, "canned_matrix"):
            raise ValhallaConnectionError(
                "VALHALLA_ENDPOINT_URL is not configured; cannot query external routing service"
            )

        locs = [s.location for s in request.stops]
        matrix = await client.sources_to_targets(
            origins=locs,
            destinations=locs,
            mode=request.transport_mode,
        )
        return self._solver.solve(request, matrix.durations_s, matrix.distances_m)

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
            logger.warning("Could not query road closure claims: %s", exc)
            return []
