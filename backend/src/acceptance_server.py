"""Acceptance backend server for local end-to-end browser testing.

Spins up the real FastAPI backend on port 8000 connected strictly to an isolated
dedicated PostgreSQL acceptance database, with an in-process LocalAcceptanceWorker
injecting MockValhallaClient to process RouteOptimizerGraph, worker solving, and
reconciliation outbox events automatically.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession

from acceptance_database import (
    get_acceptance_session_factory,
    validate_acceptance_database_url,
)
from acceptance_seeder import seed_acceptance_fixtures
from acceptance_worker import LocalAcceptanceWorker
from config.config import settings
from config.db import get_session
from functions.api.app import create_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("acceptance_server")

# Resilient DNS caching for PostgreSQL connectivity
_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", 20266): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("165.232.185.214", 20266),
        )
    ],
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", "20266"): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("165.232.185.214", 20266),
        )
    ],
}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
    try:
        res = _orig_getaddrinfo(host, port, *args, **kwargs)
        _dns_cache[key] = res
        return res
    except socket.gaierror:
        for cached_key, cached_val in _dns_cache.items():
            if cached_key[0] == host:
                return cached_val
        raise


socket.getaddrinfo = _resilient_getaddrinfo


@asynccontextmanager
async def lifespan(app: Any):
    # Fails closed if ACCEPTANCE_DATABASE_URL is missing or reuses application database
    session_factory = get_acceptance_session_factory()
    if getattr(settings, "ENVIRONMENT", "local") == "production":
        logger.warning("Acceptance server running in production environment; startup seeding disabled.")
    elif os.environ.get("SKIP_ACCEPTANCE_SEEDING", "").lower() in ("1", "true"):
        logger.info("SKIP_ACCEPTANCE_SEEDING is set; skipping acceptance fixture seeding.")
    else:
        try:
            await seed_acceptance_fixtures(session_factory)
        except Exception as e:
            logger.warning("Acceptance fixture seeding warning: %s", e)
    worker = LocalAcceptanceWorker(session_factory)
    worker_task = worker.start(poll_interval_s=0.2)
    logger.info("LocalAcceptanceWorker started for browser acceptance.")
    try:
        yield
    finally:
        worker.stop()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        logger.info("LocalAcceptanceWorker stopped.")


app = create_app()
app.router.lifespan_context = lifespan

# Ensure every acceptance HTTP component uses the same isolated acceptance database
acceptance_session_factory = get_acceptance_session_factory()


async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
    async with acceptance_session_factory() as session:
        yield session


app.dependency_overrides[get_session] = _override_get_session


@app.post("/api/v1/test/reset-replan-fixtures")
async def reset_replan_fixtures():
    """Surgically resets child replans and reservations for owned acceptance test entities."""
    from fastapi import HTTPException, status

    if getattr(settings, "ENVIRONMENT", "local") == "production":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Fixture reset endpoint is strictly forbidden in production environments",
        )

    validate_acceptance_database_url(
        settings.ACCEPTANCE_DATABASE_URL,
        settings.DATABASE_URL,
    )

    from sqlalchemy import delete, select

    from acceptance_seeder import CANONICAL_ROUTE_IDS, OWNED_USER_IDS
    from functions.api.models import ApiIdempotencyModel
    from functions.orchestrator.execution.models import (
        ExecutionModel,
        JobEventModel,
        OutboxEventModel,
        TaskModel,
        WorkerResultModel,
    )
    from functions.orchestrator.routing.models import (
        RouteLegModel,
        RouteModel,
        RouteRestrictionModel,
        RouteStopModel,
    )

    sf = get_acceptance_session_factory()
    async with sf() as session:
        stmt = select(RouteModel.id).where(
            RouteModel.owner_id.in_(OWNED_USER_IDS),
            RouteModel.parent_route_id.in_(CANONICAL_ROUTE_IDS),
        )
        child_ids = list((await session.execute(stmt)).scalars().all())

        # Collect child route execution IDs BEFORE deleting child routes
        child_exec_ids = []
        if child_ids:
            exec_stmt = select(RouteModel.execution_id).where(
                RouteModel.id.in_(child_ids),
                RouteModel.execution_id.is_not(None),
            )
            child_exec_ids = list((await session.execute(exec_stmt)).scalars().all())

            await session.execute(delete(RouteLegModel).where(RouteLegModel.route_id.in_(child_ids)))
            await session.execute(delete(RouteStopModel).where(RouteStopModel.route_id.in_(child_ids)))
            await session.execute(
                delete(RouteRestrictionModel).where(RouteRestrictionModel.route_id.in_(child_ids))
            )
            await session.execute(delete(RouteModel).where(RouteModel.id.in_(child_ids)))

        if child_exec_ids:
            await session.execute(
                delete(JobEventModel).where(JobEventModel.execution_id.in_(child_exec_ids))
            )
            await session.execute(
                delete(OutboxEventModel).where(OutboxEventModel.execution_id.in_(child_exec_ids))
            )
            await session.execute(delete(TaskModel).where(TaskModel.execution_id.in_(child_exec_ids)))
            await session.execute(
                delete(WorkerResultModel).where(WorkerResultModel.execution_id.in_(child_exec_ids))
            )
            await session.execute(delete(ExecutionModel).where(ExecutionModel.id.in_(child_exec_ids)))

        await session.execute(
            delete(ApiIdempotencyModel).where(
                ApiIdempotencyModel.owner_id.in_(OWNED_USER_IDS),
                ApiIdempotencyModel.operation == "route_optimization:replan_reservation",
                ApiIdempotencyModel.key.in_([str(r) for r in CANONICAL_ROUTE_IDS]),
            )
        )
        await session.commit()
    return {
        "status": "ok",
        "cleaned_child_ids": [str(c) for c in child_ids],
        "cleaned_child_exec_ids": [str(e) for e in child_exec_ids],
    }


if __name__ == "__main__":
    uvicorn.run("acceptance_server:app", host="127.0.0.1", port=8000, log_level="info")
