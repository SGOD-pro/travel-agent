"""Repository ports for Trip and Job aggregates."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any

from travel.domain.jobs import Job
from travel.domain.trips import Trip, TripBrief, TripVersion


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
        """Fetch trip aggregate by ID."""
        pass

    @abstractmethod
    async def save(self, trip: Trip) -> None:
        """Create or update a trip."""
        pass

    @abstractmethod
    async def commit_version(
        self,
        trip_id: uuid.UUID,
        expected_base_version: int,
        brief: TripBrief,
        command_id: uuid.UUID,
    ) -> TripVersion:
        """Atomically commit a new version vN+1 if current_version == expected_base_version.

        If command_id was already applied for this trip, returns the existing version idempotently.
        Raises TripVersionConflictError if expected_base_version != current_version.
        """
        pass

    @abstractmethod
    async def get_version(self, trip_id: uuid.UUID, version: int) -> TripVersion | None:
        """Fetch a specific version snapshot."""
        pass


class JobRepositoryPort(ABC):
    """Repository port for durable jobs."""

    @abstractmethod
    async def enqueue(self, job: Job) -> Job:
        """Enqueue a new job with idempotency key deduplication."""
        pass

    @abstractmethod
    async def claim_next(self, worker_id: str, lease_duration_seconds: int = 60) -> Job | None:
        """Atomically claim the next available job using FOR UPDATE SKIP LOCKED."""
        pass

    @abstractmethod
    async def renew_heartbeat(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        lease_generation: int,
        additional_seconds: int = 60,
    ) -> bool:
        """Extend lease duration if caller still owns the active lease generation."""
        pass

    @abstractmethod
    async def get_by_id(self, job_id: uuid.UUID) -> Job | None:
        """Fetch job by ID."""
        pass

    @abstractmethod
    async def complete(self, job_id: uuid.UUID, worker_id: str, lease_generation: int) -> bool:
        """Mark job as completed if worker still owns the active lease generation."""
        pass

    @abstractmethod
    async def fail(self, job_id: uuid.UUID, worker_id: str, error_message: str) -> bool:
        """Mark job as failed or increment attempts for retry."""
        pass

    @abstractmethod
    async def cancel(self, job_id: uuid.UUID, reason: str = "") -> bool:
        """Cancel a pending or claimed job."""
        pass


class ArtifactRepositoryPort(ABC):
    """Repository port for exported artifacts."""

    @abstractmethod
    async def save(self, artifact: Any) -> None:
        """Create or update an artifact."""
        pass

    @abstractmethod
    async def get_by_id(self, artifact_id: uuid.UUID) -> Any | None:
        """Fetch artifact by ID."""
        pass

    @abstractmethod
    async def get_by_trip(self, trip_id: uuid.UUID) -> list[Any]:
        """Fetch all artifacts associated with a trip."""
        pass

    @abstractmethod
    async def update_status(
        self,
        artifact_id: uuid.UUID,
        status: str,
        size_bytes: int | None = None,
        download_url: str | None = None,
    ) -> None:
        """Update artifact status, size, and download URL."""
        pass
