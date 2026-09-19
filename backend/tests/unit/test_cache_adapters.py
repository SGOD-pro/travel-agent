"""Unit tests for CachePort Redis adapters (AsyncRedis and UpstashRedis)."""

from unittest.mock import AsyncMock

import pytest

from travel.application.ports.cache import CachePort
from travel.infrastructure.redis.adapters import AsyncRedisAdapter, UpstashRedisAdapter


@pytest.mark.asyncio
async def test_async_redis_adapter_contract() -> None:
    adapter = AsyncRedisAdapter(url="redis://localhost:6379/0")
    assert isinstance(adapter, CachePort)

    # Mock internal aioredis client
    adapter._client = AsyncMock()
    adapter._client.get.return_value = "cached_val"
    adapter._client.delete.return_value = 1
    adapter._client.exists.return_value = 1

    await adapter.set("key1", "val1", ttl_seconds=60)
    adapter._client.set.assert_awaited_once_with("key1", "val1", ex=60)

    val = await adapter.get("key1")
    assert val == "cached_val"

    deleted = await adapter.delete("key1")
    assert deleted is True

    exists = await adapter.exists("key1")
    assert exists is True


@pytest.mark.asyncio
async def test_upstash_redis_adapter_contract() -> None:
    adapter = UpstashRedisAdapter(url="https://test.upstash.io", token="secret")
    assert isinstance(adapter, CachePort)

    # Mock internal upstash async client
    adapter._client = AsyncMock()
    adapter._client.get.return_value = "upstash_val"
    adapter._client.delete.return_value = 1
    adapter._client.exists.return_value = 1

    await adapter.set("key2", "val2", ttl_seconds=120)
    adapter._client.set.assert_awaited_once_with("key2", "val2", ex=120)

    val = await adapter.get("key2")
    assert val == "upstash_val"

    deleted = await adapter.delete("key2")
    assert deleted is True

    exists = await adapter.exists("key2")
    assert exists is True
