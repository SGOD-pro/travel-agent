"""Redis cache adapters and CachePort.

Provides:
- CachePort: abstract interface
- AsyncRedisAdapter: for local Docker Redis (via redis.asyncio)
- UpstashRedisAdapter: for production Upstash Redis HTTP REST (via upstash_redis.AsyncRedis)
- get_cache_adapter: factory function using configured settings
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import redis.asyncio as aioredis
from upstash_redis import AsyncRedis as UpstashAsyncRedis

from config.config import settings


class CachePort(ABC):
    """Abstract interface for key-value caching operations."""

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """Retrieve a value by key. Returns None if key does not exist."""
        pass

    @abstractmethod
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Store a string value with an optional time-to-live in seconds."""
        pass

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete a key. Returns True if deleted, False if key did not exist."""
        pass

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if a key exists."""
        pass


class AsyncRedisAdapter(CachePort):
    """Local Redis adapter connecting over TCP using redis-py asyncio."""

    def __init__(self, url: str) -> None:
        self._client = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        result = await self._client.get(key)
        return str(result) if result is not None else None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        if ttl_seconds is not None:
            await self._client.set(key, value, ex=ttl_seconds)
        else:
            await self._client.set(key, value)

    async def delete(self, key: str) -> bool:
        count = await self._client.delete(key)
        return count > 0

    async def exists(self, key: str) -> bool:
        count = await self._client.exists(key)
        return count > 0

    async def close(self) -> None:
        await self._client.aclose()


class UpstashRedisAdapter(CachePort):
    """Production Upstash Redis adapter using HTTP REST SDK."""

    def __init__(self, url: str, token: str) -> None:
        self._client = UpstashAsyncRedis(url=url, token=token)

    async def get(self, key: str) -> str | None:
        result = await self._client.get(key)
        return str(result) if result is not None else None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        if ttl_seconds is not None:
            await self._client.set(key, value, ex=ttl_seconds)
        else:
            await self._client.set(key, value)

    async def delete(self, key: str) -> bool:
        count = await self._client.delete(key)
        return count > 0

    async def exists(self, key: str) -> bool:
        count = await self._client.exists(key)
        return count > 0


def get_cache_adapter() -> CachePort:
    """Instantiate appropriate CachePort adapter based on application settings."""
    if settings.REDIS_BACKEND == "upstash" and settings.UPSTASH_REDIS_URL:
        return UpstashRedisAdapter(
            url=settings.UPSTASH_REDIS_URL,
            token=settings.UPSTASH_REDIS_TOKEN,
        )
    return AsyncRedisAdapter(url=settings.REDIS_URL)
