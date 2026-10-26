"""FastAPI routes for asynchronous route optimization, retrieval, and replanning.

Conforms to spec 0005 AC-9:
- POST /api/v1/optimization/routes: Submits optimization request, returns 202 with job_id.
- GET /api/v1/optimization/routes/{route_id}: Fetches route with strict owner tenant fencing.
- POST /api/v1/optimization/routes/{route_id}/replans: Submits replan request, returns 202 with job_id.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from geoalchemy2.shape import to_shape
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_session
from contracts.routing import (
    AcceptedRouteResponse,
    OptimizedRoute,
    Point,
    RouteMapGeoJsonResponse,
    RouteReplanRequest,
    RouteRequest,
    RouteVersionHistoryResponse,
    RouteVersionItem,
)
from functions.api.idempotency import compute_request_hash
from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import (
    ExecutionModel,
    OutboxEventModel,
)
from functions.orchestrator.routing.geojson import assemble_route_map_geojson
from functions.orchestrator.routing.models import RouteModel
from functions.orchestrator.routing.repository import RouteRepository
from middleware.auth import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/api/v1/optimization/routes", tags=["route-optimization"])


@router.post(
    "",
    response_model=AcceptedRouteResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_route_optimization(
    request: RouteRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AcceptedRouteResponse:
    """Submits an asynchronous route optimization job (AC-9).

    Reserves idempotency record, creates execution and task, stages SQS outbox event,
    and returns 202 Accepted with job status URL.
    """
    now = datetime.now(UTC)
    idemp_key = (
        idempotency_key.strip()
        if idempotency_key and idempotency_key.strip()
        else f"route-opt-{uuid.uuid4()}"
    )
    req_dict = request.model_dump(mode="json")
    req_hash = compute_request_hash(req_dict)

    # 1. Conflict-safe check/reservation on api_idempotency
    insert_sql = text(
        """
        INSERT INTO api_idempotency (
            owner_id, operation, key, request_hash, response_json, status_code, expires_at
        ) VALUES (
            :owner_id, :operation, :key, :request_hash, :response_json, :status_code, :expires_at
        )
        ON CONFLICT (owner_id, operation, key) DO NOTHING
        RETURNING owner_id;
        """
    )
    execution_id = uuid.uuid4()
    resp_data = {
        "job_id": str(execution_id),
        "status": "QUEUED",
        "status_url": f"/api/v1/jobs/{execution_id}",
    }

    res = await session.execute(
        insert_sql,
        {
            "owner_id": current_user.id,
            "operation": "route_optimization:solve",
            "key": idemp_key,
            "request_hash": req_hash,
            "response_json": json.dumps(resp_data),
            "status_code": 202,
            "expires_at": now + timedelta(days=1),
        },
    )
    inserted = res.scalar_one_or_none()

    if inserted is None:
        # Idempotency collision: check if same payload
        select_sql = text(
            """
            SELECT request_hash, response_json, status_code
            FROM api_idempotency
            WHERE owner_id = :owner_id AND operation = :operation AND key = :key;
            """
        )
        existing_res = await session.execute(
            select_sql,
            {
                "owner_id": current_user.id,
                "operation": "route_optimization:solve",
                "key": idemp_key,
            },
        )
        existing_row = existing_res.first()
        if existing_row and existing_row.request_hash == req_hash:
            cached_json = (
                json.loads(existing_row.response_json)
                if isinstance(existing_row.response_json, str)
                else existing_row.response_json
            )
            return AcceptedRouteResponse(
                job_id=uuid.UUID(cached_json["job_id"]),
                status=cached_json.get("status", "QUEUED"),
                status_url=cached_json["status_url"],
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Idempotency conflict for key '{idemp_key}' with differing payload",
        )

    # 2. Create execution record
    deadline = now + timedelta(seconds=request.time_limit_seconds + 300)
    execution = ExecutionModel(
        id=execution_id,
        owner_id=current_user.id,
        trip_id=request.trip_id,
        workflow="ROUTE_OPTIMIZER_GRAPH",
        status="CREATED",
        context_version=1,
        deadline_at=deadline,
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    await session.flush()

    # 3. Stage EXECUTION_START outbox event (GraphRunnerService owns task/outbox creation)
    start_payload = {
        "execution_id": str(execution_id),
        "workflow": "ROUTE_OPTIMIZER_GRAPH",
        "request": req_dict,
        "owner_id": str(current_user.id),
        "trip_id": str(request.trip_id) if request.trip_id else None,
    }
    outbox_event = OutboxEventModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        aggregate_id=execution_id,
        task_id=None,
        kind="EXECUTION_START",
        event_type="EXECUTION_START",
        payload_ref=start_payload,
        payload=start_payload,
        due_at=now,
        attempts=0,
        created_at=now,
    )
    session.add(outbox_event)
    await session.flush()

    # 4. Record job timeline event
    await record_job_event(
        session,
        execution_id=execution_id,
        event_type="JOB_ACCEPTED",
        safe_payload={
            "status": "CREATED",
            "workflow": "ROUTE_OPTIMIZER_GRAPH",
            "trip_id": str(request.trip_id) if request.trip_id else None,
            "stops_count": len(request.stops),
        },
    )

    await session.commit()

    return AcceptedRouteResponse(
        job_id=execution_id,
        status="QUEUED",
        status_url=f"/api/v1/jobs/{execution_id}",
    )


@router.get(
    "/by-job/{job_id}",
    response_model=OptimizedRoute,
    status_code=status.HTTP_200_OK,
)
async def get_route_by_job(
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> OptimizedRoute:
    """Retrieves an optimized route by execution/job ID with tenant fencing."""
    route_repo = RouteRepository(session)
    route_model = await route_repo.get_latest_route_by_execution_id(
        job_id, owner_id=current_user.id
    )

    if route_model is None:
        stmt = select(RouteModel.owner_id).where(RouteModel.execution_id == job_id)
        existing_owner = (await session.execute(stmt)).scalars().first()
        if existing_owner is not None and existing_owner != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access forbidden: job {job_id} belongs to another tenant",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No route found for job {job_id}",
        )

    return route_model.to_optimized_route()


@router.get(
    "/{route_id}/map",
    response_model=RouteMapGeoJsonResponse,
    status_code=status.HTTP_200_OK,
)
async def get_route_map(
    route_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> RouteMapGeoJsonResponse:
    """Retrieves RFC 7946 GeoJSON Map for an optimized route with tenant fencing."""
    route_repo = RouteRepository(session)
    route_model = await route_repo.get_route_by_id(route_id, owner_id=current_user.id)

    if route_model is None:
        stmt = select(RouteModel.owner_id).where(RouteModel.id == route_id)
        existing_owner = (await session.execute(stmt)).scalar_one_or_none()
        if existing_owner is not None and existing_owner != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access forbidden: route {route_id} belongs to another tenant",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Route {route_id} not found",
        )

    return assemble_route_map_geojson(route_model)


@router.get(
    "/{route_id}/versions",
    response_model=RouteVersionHistoryResponse,
    status_code=status.HTTP_200_OK,
)
async def get_route_versions(
    route_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> RouteVersionHistoryResponse:
    """Returns chronological route version lineage and deltas conforming to AC-7, AC-9."""
    repo = RouteRepository(session)
    existing_route = await repo.get_route_by_id(route_id, owner_id=current_user.id)

    if existing_route is None:
        stmt = select(RouteModel.owner_id).where(RouteModel.id == route_id)
        existing_owner = (await session.execute(stmt)).scalar_one_or_none()
        if existing_owner is not None and existing_owner != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access forbidden: route {route_id} belongs to another tenant",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Route {route_id} not found",
        )

    lineage = await repo.get_route_lineage(route_id, owner_id=current_user.id)
    if not lineage:
        lineage = [existing_route]

    versions_by_id = {v.id: v for v in lineage}
    version_items: list[RouteVersionItem] = []

    for idx, v in enumerate(lineage):
        parent_v = (
            versions_by_id.get(v.parent_route_id)
            if v.parent_route_id
            else (lineage[idx - 1] if idx > 0 else None)
        )

        dist_delta = (v.total_distance_m - parent_v.total_distance_m) if parent_v else None
        dur_delta = (v.total_travel_seconds - parent_v.total_travel_seconds) if parent_v else None

        # Calculate dropped stop IDs
        dropped_ids: list[uuid.UUID] = []
        if parent_v:
            parent_active = {s.name: s.id for s in (parent_v.stops or []) if not s.is_skipped}
            current_active_names = {s.name for s in (v.stops or []) if not s.is_skipped}
            dropped_names = set(parent_active.keys()) - current_active_names

            v_skipped_ids = [s.id for s in (v.stops or []) if s.is_skipped]
            omitted_parent_ids = [
                parent_active[name]
                for name in dropped_names
                if not any(s.name == name for s in (v.stops or []))
            ]
            dropped_ids = v_skipped_ids + omitted_parent_ids
        else:
            dropped_ids = [s.id for s in (v.stops or []) if s.is_skipped]

        # Detect replan origin
        replan_origin = None
        replan_stop = next((s for s in (v.stops or []) if s.name == "Current Location"), None)
        if replan_stop and replan_stop.location:
            try:
                pt = to_shape(replan_stop.location)
                replan_origin = Point(latitude=pt.y, longitude=pt.x)
            except Exception:
                replan_origin = None

        version_items.append(
            RouteVersionItem(
                route_id=v.id,
                route_version=v.route_version or 1,
                parent_route_id=v.parent_route_id,
                created_at=v.created_at,
                status=v.status,
                total_distance_m=v.total_distance_m,
                total_travel_seconds=v.total_travel_seconds,
                distance_delta_m=dist_delta,
                duration_delta_seconds=dur_delta,
                dropped_stop_ids=dropped_ids,
                replan_origin=replan_origin,
            )
        )

    return RouteVersionHistoryResponse(
        root_route_id=lineage[0].id,
        current_route_id=route_id,
        versions=version_items,
    )


@router.get(
    "/{route_id}",
    response_model=OptimizedRoute,
    status_code=status.HTTP_200_OK,
)
async def get_route(
    route_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> OptimizedRoute:
    """Retrieves an optimized route by ID with strict tenant ownership fencing (AC-9)."""
    route_repo = RouteRepository(session)
    route_model = await route_repo.get_route_by_id(route_id, owner_id=current_user.id)

    if route_model is None:
        # Check if route exists under another owner (for explicit 403 vs 404)
        stmt = select(RouteModel.owner_id).where(RouteModel.id == route_id)
        existing_owner = (await session.execute(stmt)).scalar_one_or_none()
        if existing_owner is not None and existing_owner != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access forbidden: route {route_id} belongs to another tenant",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Route {route_id} not found",
        )

    return route_model.to_optimized_route()


@router.post(
    "/{route_id}/replans",
    response_model=AcceptedRouteResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_route_replan(
    route_id: uuid.UUID,
    request: RouteReplanRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AcceptedRouteResponse:
    """Submits an asynchronous mid-trip replanning job for an existing route (AC-8, AC-9)."""
    # 1. Tenant ownership validation and atomic row lock
    stmt = select(RouteModel).where(RouteModel.id == route_id).with_for_update()
    res = await session.execute(stmt)
    existing_route = res.scalar_one_or_none()

    if existing_route is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Route {route_id} not found",
        )

    if existing_route.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access forbidden: route {route_id} belongs to another tenant",
        )

    # 2. Stale parent route check (AC-6 single-child replan policy)
    child_stmt = (
        select(RouteModel.id)
        .where(RouteModel.parent_route_id == route_id)
        .limit(1)
    )
    existing_child = (await session.execute(child_stmt)).scalar_one_or_none()
    if existing_child is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Route {route_id} is a stale version and has already been replanned into child route {existing_child}",
        )

    # 3. Optimistic concurrency check against expected_previous_version (AC-6)
    if (
        request.expected_previous_version is not None
        and request.expected_previous_version != existing_route.route_version
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Route version mismatch: expected version {request.expected_previous_version}, current route version is {existing_route.route_version}",
        )

    now = datetime.now(UTC)
    idemp_key = (
        idempotency_key.strip()
        if idempotency_key and idempotency_key.strip()
        else f"route-replan-{uuid.uuid4()}"
    )
    req_dict = {
        "route_id": str(route_id),
        "action": "replan",
        **request.model_dump(mode="json"),
    }
    req_hash = compute_request_hash(req_dict)

    execution_id = uuid.uuid4()
    resp_data = {
        "job_id": str(execution_id),
        "status": "QUEUED",
        "status_url": f"/api/v1/jobs/{execution_id}",
    }

    # 4. Atomic replan reservation on route_id to enforce single active replan
    replan_lock_sql = text(
        """
        INSERT INTO api_idempotency (
            owner_id, operation, key, request_hash, response_json, status_code, expires_at
        ) VALUES (
            :owner_id, :operation, :key, :request_hash, :response_json, :status_code, :expires_at
        )
        ON CONFLICT (owner_id, operation, key) DO NOTHING
        RETURNING owner_id;
        """
    )
    res_lock = await session.execute(
        replan_lock_sql,
        {
            "owner_id": current_user.id,
            "operation": "route_optimization:replan_reservation",
            "key": str(route_id),
            "request_hash": req_hash,
            "response_json": json.dumps(resp_data),
            "status_code": 202,
            "expires_at": now + timedelta(days=1),
        },
    )
    lock_inserted = res_lock.scalar_one_or_none()

    if lock_inserted is None:
        check_lock_sql = text(
            """
            SELECT request_hash, response_json, status_code
            FROM api_idempotency
            WHERE owner_id = :owner_id AND operation = :operation AND key = :key;
            """
        )
        existing_res = await session.execute(
            check_lock_sql,
            {
                "owner_id": current_user.id,
                "operation": "route_optimization:replan_reservation",
                "key": str(route_id),
            },
        )
        existing_row = existing_res.first()
        if existing_row and existing_row.request_hash == req_hash:
            cached_json = (
                json.loads(existing_row.response_json)
                if isinstance(existing_row.response_json, str)
                else existing_row.response_json
            )
            return AcceptedRouteResponse(
                job_id=uuid.UUID(cached_json["job_id"]),
                status=cached_json.get("status", "QUEUED"),
                status_url=cached_json["status_url"],
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A competing replan operation is already active or in flight for route {route_id}",
        )

    # 4. Create execution record under ROUTE_OPTIMIZER_GRAPH
    deadline = now + timedelta(seconds=request.time_limit_seconds + 300)
    execution = ExecutionModel(
        id=execution_id,
        owner_id=current_user.id,
        trip_id=existing_route.trip_id,
        workflow="ROUTE_OPTIMIZER_GRAPH",
        status="CREATED",
        context_version=1,
        deadline_at=deadline,
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    await session.flush()

    # 5. Stage EXECUTION_START outbox event (GraphRunnerService owns task dispatch and checkpoint commit)
    start_payload = {
        "execution_id": str(execution_id),
        "workflow": "ROUTE_OPTIMIZER_GRAPH",
        "request": req_dict,
        "owner_id": str(current_user.id),
        "trip_id": str(existing_route.trip_id) if existing_route.trip_id else None,
    }
    outbox_event = OutboxEventModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        aggregate_id=execution_id,
        task_id=None,
        kind="EXECUTION_START",
        event_type="EXECUTION_START",
        payload_ref=start_payload,
        payload=start_payload,
        due_at=now,
        attempts=0,
        created_at=now,
    )
    session.add(outbox_event)
    await session.flush()

    # 6. Record job timeline event
    await record_job_event(
        session,
        execution_id=execution_id,
        event_type="JOB_ACCEPTED",
        safe_payload={
            "status": "CREATED",
            "workflow": "ROUTE_OPTIMIZER_GRAPH",
            "action": "replan",
            "parent_route_id": str(route_id),
            "previous_version": existing_route.route_version,
            "trip_id": str(existing_route.trip_id) if existing_route.trip_id else None,
            "completed_stops_count": len(request.completed_stop_ids),
        },
    )

    await session.commit()

    return AcceptedRouteResponse(
        job_id=execution_id,
        status="QUEUED",
        status_url=f"/api/v1/jobs/{execution_id}",
    )
