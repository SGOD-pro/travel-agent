"""Acceptance backend server for local end-to-end browser testing.

Spins up the real FastAPI backend on port 8000 connected to live PostgreSQL,
with an in-process LocalAcceptanceWorker injecting MockValhallaClient to process
RouteOptimizerGraph, worker solving, and reconciliation outbox events automatically.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from acceptance_seeder import seed_acceptance_fixtures
from acceptance_worker import LocalAcceptanceWorker
from config.config import settings
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


def get_live_session_factory() -> async_sessionmaker[AsyncSession]:
    url = settings.DATABASE_URL
    connect_args: dict[str, Any] = {
        "command_timeout": 60,
        "timeout": 30,
        "statement_cache_size": 0,
    }
    if (
        "sslmode=require" in url
        or "ssl=require" in url
        or "aivencloud.com" in url
        or settings.DB_SSL
    ):
        connect_args["ssl"] = "require"
    if "sslmode=" in url:
        url = re.sub(r"[?&]sslmode=[^&]+", "", url)
        if "?" not in url and "&" in url:
            url = url.replace("&", "?", 1)

    engine = create_async_engine(
        url,
        connect_args=connect_args,
        poolclass=NullPool,
    )
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: Any):
    session_factory = get_live_session_factory()
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


@app.post("/api/v1/test/reset-replan-fixtures")
async def reset_replan_fixtures():
    """Surgically resets child replans and reservations for owned acceptance test entities."""
    from config.config import settings
    from fastapi import HTTPException, status

    if getattr(settings, "ENVIRONMENT", "local") == "production":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Fixture reset endpoint is strictly forbidden in production environments",
        )

    from sqlalchemy import delete, select
    from acceptance_seeder import CANONICAL_ROUTE_IDS, OWNED_USER_IDS
    from functions.api.models import ApiIdempotencyModel
    from functions.orchestrator.routing.models import (
        RouteLegModel,
        RouteModel,
        RouteRestrictionModel,
        RouteStopModel,
    )

    sf = get_live_session_factory()
    async with sf() as session:
        stmt = select(RouteModel.id).where(RouteModel.parent_route_id.in_(CANONICAL_ROUTE_IDS))
        child_ids = list((await session.execute(stmt)).scalars().all())
        if child_ids:
            await session.execute(delete(RouteLegModel).where(RouteLegModel.route_id.in_(child_ids)))
            await session.execute(delete(RouteStopModel).where(RouteStopModel.route_id.in_(child_ids)))
            await session.execute(
                delete(RouteRestrictionModel).where(RouteRestrictionModel.route_id.in_(child_ids))
            )
            await session.execute(delete(RouteModel).where(RouteModel.id.in_(child_ids)))

        await session.execute(
            delete(ApiIdempotencyModel).where(
                ApiIdempotencyModel.operation.in_(
                    ("route_optimization:replan", "route_optimization:replan_reservation")
                ),
                ApiIdempotencyModel.owner_id.in_(OWNED_USER_IDS),
            )
        )
        await session.commit()
    return {"status": "ok", "cleaned_child_ids": [str(c) for c in child_ids]}


if __name__ == "__main__":
    uvicorn.run("acceptance_server:app", host="127.0.0.1", port=8000, log_level="info")

