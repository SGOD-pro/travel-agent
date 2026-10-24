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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_session
from contracts.routing import (
    AcceptedRouteResponse,
    OptimizedRoute,
    RouteReplanRequest,
    RouteRequest,
)
from functions.api.idempotency import compute_request_hash
from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import (
    ExecutionModel,
    OutboxEventModel,
    TaskModel,
)
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
    idemp_key = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else f"route-opt-{uuid.uuid4()}"
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
            cached_json = json.loads(existing_row.response_json)
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
        workflow="ROUTE_OPTIMIZATION",
        status="CREATED",
        context_version=1,
        deadline_at=deadline,
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    await session.flush()

    # 3. Create task record
    task_id = uuid.uuid4()
    task = TaskModel(
        id=task_id,
        execution_id=execution_id,
        worker="OPTIMIZER",
        action="solve",
        status="PENDING",
        attempt=0,
        payload_ref=req_dict,
        input_hash=req_hash,
        idempotency_key=f"task-{task_id}",
        deadline_at=deadline,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    await session.flush()

    # 4. Stage TASK_DISPATCH outbox event
    dispatch_payload = {
        "task_id": str(task_id),
        "execution_id": str(execution_id),
        "worker": "OPTIMIZER",
        "action": "solve",
        "attempt": 0,
        "context_version": 1,
        "payload": req_dict,
    }
    outbox_event = OutboxEventModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        aggregate_id=execution_id,
        task_id=task_id,
        kind="TASK_DISPATCH",
        event_type="TASK_DISPATCH",
        payload_ref=dispatch_payload,
        payload=dispatch_payload,
        due_at=now,
        attempts=0,
        created_at=now,
    )
    session.add(outbox_event)
    await session.flush()

    # 5. Record job timeline event
    await record_job_event(
        session,
        execution_id=execution_id,
        event_type="JOB_ACCEPTED",
        safe_payload={
            "status": "CREATED",
            "workflow": "ROUTE_OPTIMIZATION",
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
    # 1. Tenant ownership validation
    stmt = select(RouteModel).where(RouteModel.id == route_id)
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

    now = datetime.now(UTC)
    idemp_key = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else f"route-replan-{uuid.uuid4()}"
    req_dict = {
        "route_id": str(route_id),
        **request.model_dump(mode="json"),
    }
    req_hash = compute_request_hash(req_dict)

    # 2. Idempotency reservation
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

    res_idemp = await session.execute(
        insert_sql,
        {
            "owner_id": current_user.id,
            "operation": f"route_optimization:replan:{route_id}",
            "key": idemp_key,
            "request_hash": req_hash,
            "response_json": json.dumps(resp_data),
            "status_code": 202,
            "expires_at": now + timedelta(days=1),
        },
    )
    inserted = res_idemp.scalar_one_or_none()

    if inserted is None:
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
                "operation": f"route_optimization:replan:{route_id}",
                "key": idemp_key,
            },
        )
        existing_row = existing_res.first()
        if existing_row and existing_row.request_hash == req_hash:
            cached_json = json.loads(existing_row.response_json)
            return AcceptedRouteResponse(
                job_id=uuid.UUID(cached_json["job_id"]),
                status=cached_json.get("status", "QUEUED"),
                status_url=cached_json["status_url"],
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Idempotency conflict for key '{idemp_key}' with differing payload",
        )

    # 3. Create execution record
    deadline = now + timedelta(seconds=request.time_limit_seconds + 300)
    execution = ExecutionModel(
        id=execution_id,
        owner_id=current_user.id,
        trip_id=existing_route.trip_id,
        workflow="ROUTE_REPLAN",
        status="CREATED",
        context_version=1,
        deadline_at=deadline,
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    await session.flush()

    # 4. Create task record
    task_id = uuid.uuid4()
    task = TaskModel(
        id=task_id,
        execution_id=execution_id,
        worker="OPTIMIZER",
        action="replan",
        status="PENDING",
        attempt=0,
        payload_ref=req_dict,
        input_hash=req_hash,
        idempotency_key=f"task-{task_id}",
        deadline_at=deadline,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    await session.flush()

    # 5. Stage TASK_DISPATCH outbox event
    dispatch_payload = {
        "task_id": str(task_id),
        "execution_id": str(execution_id),
        "worker": "OPTIMIZER",
        "action": "replan",
        "attempt": 0,
        "context_version": 1,
        "payload": req_dict,
    }
    outbox_event = OutboxEventModel(
        id=uuid.uuid4(),
        execution_id=execution_id,
        aggregate_id=execution_id,
        task_id=task_id,
        kind="TASK_DISPATCH",
        event_type="TASK_DISPATCH",
        payload_ref=dispatch_payload,
        payload=dispatch_payload,
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
            "workflow": "ROUTE_REPLAN",
            "previous_route_id": str(route_id),
            "completed_stops_count": len(request.completed_stop_ids),
        },
    )

    await session.commit()

    return AcceptedRouteResponse(
        job_id=execution_id,
        status="QUEUED",
        status_url=f"/api/v1/jobs/{execution_id}",
    )
