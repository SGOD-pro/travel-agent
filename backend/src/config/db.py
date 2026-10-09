"""Database connection and session factory.

Provides async SQLAlchemy engine and sessionmaker connectivity.
No queries or business logic belong here.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = settings.DATABASE_URL
        connect_args: dict[str, Any] = {}
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
        _engine = create_async_engine(
            url,
            echo=settings.DEBUG,
            future=True,
            connect_args=connect_args,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI / runner dependency providing a scoped async database session."""
    factory = get_session_factory()
    async with factory() as session:
        yield session
