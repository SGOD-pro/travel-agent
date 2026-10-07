"""Repository interfaces and re-exports for API module."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any

from contracts.artifacts import Artifact, ArtifactStatus
from contracts.jobs import Job
from contracts.trips import Trip, TripBrief, TripVersion


class TripVersionConflictError(Exception):
    """Raised when an update conflicts with the current trip version."""

    def __init__(self, trip_id: uuid.UUID, current_version: int, expected_version: int) -> None:
        super().__init__(
            f"Trip {trip_id} version conflict: current is {current_version}, expected {expected_version}"
        )
        self.trip_id = trip_id
        self.current_version = current_version
        self.expected_version = expected_version


class TripRepositoryPort(ABC):
    """Repository port for Trips."""

    @abstractmethod
    async def get_by_id(self, trip_id: uuid.UUID) -> Trip | None:
        pass

    @abstractmethod
    async def save(self, trip: Trip) -> None:
        pass

    @abstractmethod
    async def commit_version(
        self,
        trip_id: uuid.UUID,
        expected_base_version: int,
        brief: TripBrief,
        command_id: uuid.UUID,
    ) -> TripVersion:
        pass

    @abstractmethod
    async def get_version(self, trip_id: uuid.UUID, version: int) -> TripVersion | None:
        pass


class JobRepositoryPort(ABC):
    """Repository port for durable jobs."""

    @abstractmethod
    async def enqueue(self, job: Job) -> Job:
        pass

    @abstractmethod
    async def claim_next(self, worker_id: str, lease_seconds: int = 60) -> Job | None:
        pass

    @abstractmethod
    async def complete(self, job_id: uuid.UUID, worker_id: str, lease_generation: int) -> None:
        pass

    @abstractmethod
    async def fail(self, job_id: uuid.UUID, worker_id: str, error_message: str) -> None:
        pass

    @abstractmethod
    async def get_by_id(self, job_id: uuid.UUID) -> Job | None:
        pass


class ArtifactRepositoryPort(ABC):
    """Repository port for PDF and visual exports."""

    @abstractmethod
    async def save(self, artifact: Artifact) -> None:
        pass

    @abstractmethod
    async def get_by_id(self, artifact_id: uuid.UUID) -> Artifact | None:
        pass

    @abstractmethod
    async def update_status(
        self,
        artifact_id: uuid.UUID,
        status: ArtifactStatus,
        url: str | None = None,
        file_size_bytes: int | None = None,
        error_message: str | None = None,
    ) -> None:
        pass


class UnitOfWorkPort(ABC):
    """Transactional boundary unit of work."""

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
        pass

    @abstractmethod
    async def rollback(self) -> None:
        pass


from functions.api.artifact_repository import SqlAlchemyArtifactRepository  # noqa: E402
from functions.api.job_repository import SqlAlchemyJobRepository  # noqa: E402
from functions.api.trip_repository import SqlAlchemyTripRepository  # noqa: E402
from functions.api.unit_of_work import SqlAlchemyUnitOfWork  # noqa: E402

__all__ = [
    "TripVersionConflictError",
    "TripRepositoryPort",
    "JobRepositoryPort",
    "ArtifactRepositoryPort",
    "UnitOfWorkPort",
    "SqlAlchemyTripRepository",
    "SqlAlchemyJobRepository",
    "SqlAlchemyArtifactRepository",
    "SqlAlchemyUnitOfWork",
]
