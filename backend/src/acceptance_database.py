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
_dns_cache: dict[tuple[Any, ...], Any] = {
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", 20266): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("139.59.42.218", 20266),
        )
    ],
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", "20266"): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("139.59.42.218", 20266),
        )
    ],
}


def _clean_host(host: Any) -> str:
    """Normalizes hostname for consistent cache lookups."""
    if host is None:
        return ""
    return str(host).strip("[]").lower().rstrip(".")


def _normalize_port(port: Any) -> int:
    """Normalizes port representation to an integer port (0 if omitted, empty, or unresolvable)."""
    if port is None or port == "":
        return 0
    try:
        return int(port)
    except (ValueError, TypeError):
        try:
            return socket.getservbyname(str(port))
        except Exception:
            return 0


def _format_addr_info_with_port(addr_info: list[Any], port: Any) -> list[Any]:
    """Reconstructs getaddrinfo tuples preserving family, type, proto, canonname, and IP,
    while setting the sockaddr port to the caller's requested port."""
    target_port = _normalize_port(port)
    formatted = []
    for item in addr_info:
        family, socktype, proto, canonname, sockaddr = item
        if isinstance(sockaddr, tuple):
            if len(sockaddr) == 2:
                new_sockaddr = (sockaddr[0], target_port)
            elif len(sockaddr) == 4:
                new_sockaddr = (sockaddr[0], target_port, sockaddr[2], sockaddr[3])
            else:
                new_sockaddr = sockaddr
        else:
            new_sockaddr = sockaddr
        formatted.append((family, socktype, proto, canonname, new_sockaddr))
    return formatted


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _format_addr_info_with_port(_dns_cache[key], port)

    cleaned_host = _clean_host(host)
    for cached_key, cached_val in _dns_cache.items():
        if _clean_host(cached_key[0]) == cleaned_host:
            res = _format_addr_info_with_port(cached_val, port)
            _dns_cache[key] = res
            return res

    try:
        res = _orig_getaddrinfo(host, port, *args, **kwargs)
        _dns_cache[key] = res
        return res
    except socket.gaierror as e:
        for cached_key, cached_val in _dns_cache.items():
            if _clean_host(cached_key[0]) == cleaned_host:
                res = _format_addr_info_with_port(cached_val, port)
                _dns_cache[key] = res
                return res
        raise e


socket.getaddrinfo = _resilient_getaddrinfo


import ipaddress


def is_loopback_host(host: str) -> bool:
    """Returns True if the host represents a loopback or local binding address."""
    cleaned = host.strip("[]").lower().rstrip(".")
    if cleaned in ("localhost", "localhost.localdomain", "::1", "0.0.0.0", "::"):
        return True
    try:
        ip = ipaddress.ip_address(cleaned)
        return ip.is_loopback or ip.is_unspecified
    except ValueError:
        return False


import concurrent.futures

_resolver_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def resolve_host_ips(host: str, timeout: float = 1.0) -> set[str]:
    """Resolves host string to a set of IP addresses using IP literal parsing and DNS caching.
    Safely times out on unresolvable hosts to avoid blocking."""
    cleaned = host.strip("[]").lower().rstrip(".")
    ips: set[str] = set()
    try:
        ip = ipaddress.ip_address(cleaned)
        ips.add(str(ip))
        return ips
    except ValueError:
        pass

    # Check cached entries first
    for cached_key, cached_val in _dns_cache.items():
        if _clean_host(cached_key[0]) == cleaned:
            for item in cached_val:
                sockaddr = item[4]
                if sockaddr and sockaddr[0]:
                    ips.add(str(sockaddr[0]))
            if ips:
                return ips

    try:
        future = _resolver_executor.submit(_orig_getaddrinfo, cleaned, 0)
        addr_info = future.result(timeout=timeout)
        for item in addr_info:
            sockaddr = item[4]
            if sockaddr and sockaddr[0]:
                ips.add(str(sockaddr[0]))
        if addr_info:
            _dns_cache[(cleaned, 0)] = addr_info
    except Exception:
        pass
    return ips


def are_hosts_equivalent(host1: str, host2: str) -> bool:
    """Determines if two host representations refer to the same physical host or alias.
    Fails closed (returns True) if target host identity cannot be determined safely."""
    h1 = host1.strip("[]").lower().rstrip(".")
    h2 = host2.strip("[]").lower().rstrip(".")

    if not h1 or not h2:
        return True

    # Exact normalized string match (e.g. case / trailing dot variants)
    if h1 == h2:
        return True

    # Both loopback / local addresses (localhost, 127.0.0.1, 127.0.0.2, ::1, 0.0.0.0)
    if is_loopback_host(h1) and is_loopback_host(h2):
        return True

    # Resolved IP intersection
    ips1 = resolve_host_ips(h1)
    ips2 = resolve_host_ips(h2)

    # Fail closed: if neither or only one host can be resolved safely, target identity is indeterminate
    if not ips1 or not ips2:
        return True

    return bool(ips1 & ips2)


def normalize_db_connection_target(url: str | None) -> dict[str, Any] | None:
    """Extracts normalized connection parameters (host, port, dbname)
    accounting for varying schemes (postgres://, postgresql+asyncpg://) and query parameters.
    Fails closed (returns None) if host or dbname is missing or cannot be safely determined."""
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
        host = (parsed.hostname or "").strip("[]").lower().rstrip(".")
        if not host:
            return None
        port = 5432 if parsed.port is None else parsed.port
        if not (1 <= port <= 65535):
            return None
        raw_dbname = parsed.path.lstrip("/").split("?")[0]
        if not raw_dbname or not raw_dbname.strip():
            return None
        dbname = raw_dbname.strip().lower()
        return {
            "host": host,
            "port": port,
            "dbname": dbname,
        }
    except Exception:
        return None


def are_db_urls_equivalent(url1: str | None, url2: str | None) -> bool:
    """Returns True if two connection strings resolve to the exact same database target.
    Target equivalence is determined by (host, port, dbname) and host aliases.
    PostgreSQL usernames and credentials are NOT used to differentiate targets because
    different users access the same physical database.
    Fails closed (returns True) if either target cannot be determined safely."""
    norm1 = normalize_db_connection_target(url1)
    norm2 = normalize_db_connection_target(url2)
    if norm1 is None or norm2 is None:
        # Fails closed: if target cannot be safely determined, assume equivalence to prevent bypass
        return True

    # Different database names on the same host are distinct
    if norm1["dbname"] != norm2["dbname"]:
        return False

    # Different ports on the same host are distinct
    if norm1["port"] != norm2["port"]:
        return False

    # Check host equivalence including loopback and IP aliases
    return are_hosts_equivalent(norm1["host"], norm2["host"])


def validate_isolated_database_url(
    target_url: str | None,
    app_url: str | None,
    *,
    label: str = "Acceptance database",
) -> str:
    """Validates that a dedicated isolated database URL is explicitly configured
    and does not point to the regular application database. Fails closed."""
    env_name = "ACCEPTANCE_DATABASE_URL" if "acceptance" in label.lower() else "TEST_DATABASE_URL"
    if not target_url or not target_url.strip():
        raise RuntimeError(
            f"{env_name} must be explicitly configured with a dedicated isolated database URL. "
            f"{label} execution is strictly isolated and fails closed without it."
        )
    norm_target = normalize_db_connection_target(target_url)
    if norm_target is None:
        raise RuntimeError(
            f"{env_name} is invalid or its target cannot be determined safely."
        )
    norm_app = normalize_db_connection_target(app_url)
    if norm_app is None:
        raise RuntimeError(
            "DATABASE_URL is invalid or its target cannot be determined safely."
        )
    if are_db_urls_equivalent(target_url, app_url):
        raise RuntimeError(
            f"{env_name} cannot point to the same database as DATABASE_URL. "
            "A dedicated isolated database is required to protect application and development data."
        )
    return target_url.strip()


def validate_acceptance_database_url(
    acceptance_url: str | None,
    app_url: str | None,
) -> str:
    """Validates that a dedicated acceptance database URL is explicitly configured
    and does not point to the regular application database. Fails closed."""
    return validate_isolated_database_url(
        acceptance_url,
        app_url,
        label="Acceptance database",
    )


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
        "timeout": 60,
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

