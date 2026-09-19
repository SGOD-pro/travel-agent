"""Unit of work port for transactional consistency across aggregates and outbox."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from travel.application.ports.repository import (
    ArtifactRepositoryPort,
    JobRepositoryPort,
    TripRepositoryPort,
)


class UnitOfWorkPort(ABC):
    """Coordinates atomic transactional persistence across repositories."""

    trips: TripRepositoryPort
    jobs: JobRepositoryPort
    artifacts: ArtifactRepositoryPort

    async def __aenter__(self) -> UnitOfWorkPort:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None:
            await self.rollback()
        else:
            await self.commit()

    @abstractmethod
    async def commit(self) -> None:
        """Commit the current transaction."""
        pass

    @abstractmethod
    async def rollback(self) -> None:
        """Rollback the current transaction."""
        pass
