"""SQLAlchemy implementation of UnitOfWorkPort."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from travel.application.ports.unit_of_work import UnitOfWorkPort
from travel.persistence.repositories.artifact_repository import SqlAlchemyArtifactRepository
from travel.persistence.repositories.job_repository import SqlAlchemyJobRepository
from travel.persistence.repositories.trip_repository import SqlAlchemyTripRepository


class SqlAlchemyUnitOfWork(UnitOfWorkPort):
    """Coordinates transactional persistence across repositories."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.trips = SqlAlchemyTripRepository(self._session)
        self.jobs = SqlAlchemyJobRepository(self._session)
        self.artifacts = SqlAlchemyArtifactRepository(self._session)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if exc_type is not None:
                await self.rollback()
            else:
                await self.commit()
        finally:
            if self._session:
                await self._session.close()

    async def commit(self) -> None:
        if self._session:
            await self._session.commit()

    async def rollback(self) -> None:
        if self._session:
            await self._session.rollback()
