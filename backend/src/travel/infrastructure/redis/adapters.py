"""Redis cache adapters implementing CachePort.

Provides:
- AsyncRedisAdapter: for local Docker Redis (via redis.asyncio)
- UpstashRedisAdapter: for production Upstash Redis HTTP REST (via upstash_redis.AsyncRedis)
- get_cache_adapter: factory function using configured settings
"""

from __future__ import annotations

from upstash_redis import AsyncRedis as UpstashAsyncRedis

import redis.asyncio as aioredis
from travel.application.ports.cache import CachePort
from travel.infrastructure.config import settings


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
        return bool(count and count > 0)

    async def exists(self, key: str) -> bool:
        count = await self._client.exists(key)
        return bool(count and count > 0)

    async def close(self) -> None:
        await self._client.close()


def get_cache_adapter() -> CachePort:
    """Factory returning the configured cache adapter."""
    if settings.REDIS_BACKEND == "upstash":
        if not settings.UPSTASH_REDIS_URL or not settings.UPSTASH_REDIS_TOKEN:
            raise ValueError(
                "UPSTASH_REDIS_URL and UPSTASH_REDIS_TOKEN must be configured for upstash backend"
            )
        return UpstashRedisAdapter(
            url=settings.UPSTASH_REDIS_URL,
            token=settings.UPSTASH_REDIS_TOKEN,
        )
    return AsyncRedisAdapter(url=settings.REDIS_URL)
