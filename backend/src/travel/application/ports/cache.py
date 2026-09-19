"""Cache and Ephemeral Storage Port.

Defines the contract for caching and session state, independent of whether
backed by Upstash Redis (production HTTP) or Local Redis (asyncio TCP).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


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
