"""Acceptance database isolation utilities conforming to spec 0006.

Enforces that acceptance testing, seeding, fixture reset, and worker execution
run strictly against an explicitly configured dedicated acceptance database,
failing closed if configuration is missing or points to the application database.
"""

from __future__ import annotations

import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.config import settings

logger = logging.getLogger(__name__)

# Resilient DNS caching for PostgreSQL connectivity
_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port, args, tuple(sorted(kwargs.items())))
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


def normalize_db_connection_target(url: str | None) -> dict[str, Any] | None:
    """Extracts normalized connection parameters (host, port, username, dbname)
    accounting for varying schemes (postgres://, postgresql+asyncpg://) and query parameters."""
    if not url or not url.strip():
        return None
    cleaned = url.strip()
    # Normalize scheme to postgresql:// for standard urlparse
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres://"):
        if cleaned.startswith(prefix):
            cleaned = "postgresql://" + cleaned[len(prefix) :]
            break
    try:
        parsed = urlparse(cleaned)
        host = (parsed.hostname or "").lower()
        port = parsed.port or 5432
        username = parsed.username or ""
        dbname = parsed.path.lstrip("/").split("?")[0]
        return {
            "host": host,
            "port": port,
            "username": username,
            "dbname": dbname,
        }
    except Exception:
        return None


def are_db_urls_equivalent(url1: str | None, url2: str | None) -> bool:
    """Returns True if two connection strings resolve to the exact same database target."""
    norm1 = normalize_db_connection_target(url1)
    norm2 = normalize_db_connection_target(url2)
    if norm1 is None or norm2 is None:
        return False
    return norm1 == norm2


def validate_acceptance_database_url(
    acceptance_url: str | None,
    app_url: str | None,
) -> str:
    """Validates that a dedicated acceptance database URL is explicitly configured
    and does not point to the regular application database. Fails closed."""
    if not acceptance_url or not acceptance_url.strip():
        raise RuntimeError(
            "ACCEPTANCE_DATABASE_URL must be explicitly configured with a dedicated acceptance database URL. "
            "Acceptance execution is strictly isolated and fails closed without it."
        )
    if are_db_urls_equivalent(acceptance_url, app_url):
        raise RuntimeError(
            "ACCEPTANCE_DATABASE_URL cannot point to the same database as DATABASE_URL. "
            "A dedicated acceptance database is required to protect application and development data."
        )
    return acceptance_url.strip()


def get_acceptance_session_factory() -> async_sessionmaker[AsyncSession]:
    """Creates a scoped session factory targeting the validated dedicated acceptance database.
    Fails closed if ACCEPTANCE_DATABASE_URL is missing or reuses the application database."""
    validated_url = validate_acceptance_database_url(
        settings.ACCEPTANCE_DATABASE_URL,
        settings.DATABASE_URL,
    )

    url = validated_url
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

    # Ensure asyncpg driver
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]

    engine = create_async_engine(
        url,
        connect_args=connect_args,
        poolclass=NullPool,
    )
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def ensure_acceptance_schema(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Ensures required LangGraph checkpoint tables exist in the dedicated acceptance database."""
    from sqlalchemy import text

    async with session_factory() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BYTEA NOT NULL,
                metadata BYTEA NOT NULL,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            );
        """))
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS checkpoint_writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BYTEA NOT NULL,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            );
        """))
        await session.commit()

