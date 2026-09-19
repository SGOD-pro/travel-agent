"""Unit tests for FastAPI Export and Artifact endpoints."""

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from travel.application.ports.repository import (
    ArtifactRepositoryPort,
    JobRepositoryPort,
    TripRepositoryPort,
    TripVersionConflictError,
)
from travel.application.ports.unit_of_work import UnitOfWorkPort
from travel.domain.artifacts import Artifact, ArtifactStatus
from travel.domain.jobs import Job, JobStatus
from travel.domain.trips import DestinationPoint, TransportMode, Trip, TripBrief, TripVersion
from travel.runtime.fastapi.app import app
from travel.runtime.fastapi.routes.exports import get_uow


class InMemoryTripRepo(TripRepositoryPort):
    def __init__(self) -> None:
        self.trips: dict[uuid.UUID, Trip] = {}
        self.versions: dict[tuple[uuid.UUID, int], TripVersion] = {}

    async def get_by_id(self, trip_id: uuid.UUID) -> Trip | None:
        return self.trips.get(trip_id)

    async def save(self, trip: Trip) -> None:
        self.trips[trip.id] = trip

    async def commit_version(
        self,
        trip_id: uuid.UUID,
        expected_base_version: int,
        brief: TripBrief,
        command_id: uuid.UUID,
    ) -> TripVersion:
        trip = self.trips.get(trip_id)
        current_v = trip.current_version if trip else 0
        if current_v != expected_base_version:
            raise TripVersionConflictError(trip_id, current_v, expected_base_version)
        new_v = expected_base_version + 1
        if trip:
            trip.current_version = new_v
        v = TripVersion(trip_id=trip_id, version=new_v, brief=brief, command_id=command_id)
        self.versions[(trip_id, new_v)] = v
        return v

    async def get_version(self, trip_id: uuid.UUID, version: int) -> TripVersion | None:
        return self.versions.get((trip_id, version))


class InMemoryJobRepo(JobRepositoryPort):
    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, Job] = {}

    async def enqueue(self, job: Job) -> Job:
        self.jobs[job.id] = job
        return job

    async def claim_next(self, worker_id: str, lease_duration_seconds: int = 60) -> Job | None:
        return None

    async def renew_heartbeat(
        self, job_id: uuid.UUID, worker_id: str, lease_generation: int, additional_seconds: int = 60
    ) -> bool:
        return True

    async def complete(self, job_id: uuid.UUID, worker_id: str, lease_generation: int) -> bool:
        return True

    async def fail(self, job_id: uuid.UUID, worker_id: str, error_message: str) -> bool:
        return True

    async def get_by_id(self, job_id: uuid.UUID) -> Job | None:
        return self.jobs.get(job_id)

    async def cancel(self, job_id: uuid.UUID, reason: str = "") -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.status = JobStatus.CANCELLED
        return True


class InMemoryArtifactRepo(ArtifactRepositoryPort):
    def __init__(self) -> None:
        self.artifacts: dict[uuid.UUID, Artifact] = {}

    async def save(self, artifact: Any) -> None:
        assert isinstance(artifact, Artifact)
        self.artifacts[artifact.id] = artifact

    async def get_by_id(self, artifact_id: uuid.UUID) -> Artifact | None:
        return self.artifacts.get(artifact_id)

    async def get_by_trip(self, trip_id: uuid.UUID) -> list[Artifact]:
        return [a for a in self.artifacts.values() if a.trip_id == trip_id]

    async def update_status(
        self,
        artifact_id: uuid.UUID,
        status: str,
        size_bytes: int | None = None,
        download_url: str | None = None,
    ) -> None:
        artifact = self.artifacts.get(artifact_id)
        if artifact:
            artifact.status = ArtifactStatus(status)
            if size_bytes is not None:
                artifact.size_bytes = size_bytes
            if download_url is not None:
                artifact.download_url = download_url


class MockUnitOfWork(UnitOfWorkPort):
    def __init__(self) -> None:
        self.trips = InMemoryTripRepo()
        self.jobs = InMemoryJobRepo()
        self.artifacts = InMemoryArtifactRepo()

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@pytest.fixture
def mock_uow() -> MockUnitOfWork:
    return MockUnitOfWork()


@pytest.mark.asyncio
async def test_request_export_and_get_artifact(mock_uow: MockUnitOfWork) -> None:
    app.dependency_overrides[get_uow] = lambda: mock_uow

    trip_id = uuid.uuid4()
    brief = TripBrief(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        destinations=[
            DestinationPoint(name="Mysuru", latitude=12.2958, longitude=76.6394, stay_days=2)
        ],
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 5),
        adults=2,
        rooms=1,
        preferred_modes=[TransportMode.CAR_PETROL],
    )
    trip = Trip(id=trip_id, owner_id=uuid.uuid4(), current_version=1)
    await mock_uow.trips.save(trip)
    mock_uow.trips.versions[(trip_id, 1)] = TripVersion(
        trip_id=trip_id, version=1, brief=brief, command_id=uuid.uuid4()
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Request Export
        res = await client.post(
            f"/api/v1/trips/{trip_id}/exports",
            json={"trip_version": 1, "format": "pdf"},
        )
        assert res.status_code == 202
        data = res.json()
        artifact_id = data["artifact_id"]
        job_id = data["job_id"]
        assert data["status"] == "pending"

        # Check Artifact Status
        art_res = await client.get(f"/api/v1/artifacts/{artifact_id}")
        assert art_res.status_code == 200
        art_data = art_res.json()
        assert art_data["status"] == "pending"
        assert art_data["content_type"] == "application/pdf"

        # Cancel Job
        cancel_res = await client.post(f"/api/v1/jobs/{job_id}/cancel")
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "cancelled"

    app.dependency_overrides.clear()
