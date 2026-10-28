"""Shared pytest fixtures for integration tests."""

from __future__ import annotations

import re
import socket
import time
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.config import settings
from functions.orchestrator.execution.checkpointer import TransactionBoundCheckpointer

import os

from acceptance_database import (
    _resilient_getaddrinfo,
    are_db_urls_equivalent,
    validate_isolated_database_url,
)

socket.getaddrinfo = _resilient_getaddrinfo


def get_isolated_test_database_url() -> str:
    """Returns the validated isolated test database URL.
    Fails closed if configuration is missing or points to the application database."""
    test_url = (
        os.environ.get("TEST_DATABASE_URL")
        or getattr(settings, "TEST_DATABASE_URL", None)
        or settings.ACCEPTANCE_DATABASE_URL
    )
    return validate_isolated_database_url(
        test_url,
        settings.DATABASE_URL,
        label="Integration test database",
    )


@pytest.fixture
async def session_factory():
    url = get_isolated_test_database_url()
    connect_args = {"command_timeout": 60, "timeout": 60}
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
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    test_engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await test_engine.dispose()


@pytest.fixture
async def postgres_checkpointer(session_factory):
    test_url = get_isolated_test_database_url()
    if are_db_urls_equivalent(test_url, settings.DATABASE_URL):
        raise RuntimeError(
            "Destructive postgres_checkpointer fixture cannot run against the application database. "
            "An explicitly configured isolated test database is required."
        )
    async with session_factory() as session:
        await session.execute(text("DROP TABLE IF EXISTS checkpoint_writes;"))
        await session.execute(text("DROP TABLE IF EXISTS checkpoints;"))
        await session.execute(text("""
            CREATE TABLE checkpoints (
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
            CREATE TABLE checkpoint_writes (
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

    checkpointer = TransactionBoundCheckpointer(session_factory=session_factory)
    await checkpointer.setup()
    yield checkpointer
