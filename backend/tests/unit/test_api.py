"""Unit tests for FastAPI HTTP routes."""

import uuid
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
from travel.domain.artifacts import ArtifactStatus
from travel.domain.jobs import Job, JobStatus
from travel.domain.trips import (
    Trip,
    TripBrief,
    TripState,
    TripVersion,
)
from travel.runtime.fastapi.app import app
from travel.runtime.fastapi.routes.trips import get_uow


class InMemoryTripRepository(TripRepositoryPort):
    def __init__(self) -> None:
        self.trips: dict[uuid.UUID, Trip] = {}
        self.versions: dict[tuple[uuid.UUID, int], TripVersion] = {}
        self.commands: dict[tuple[uuid.UUID, uuid.UUID], TripVersion] = {}

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
        # Idempotency
        if (trip_id, command_id) in self.commands:
            return self.commands[(trip_id, command_id)]

        trip = self.trips.get(trip_id)
        current_v = trip.current_version if trip else 0
        if current_v != expected_base_version:
            raise TripVersionConflictError(trip_id, current_v, expected_base_version)

        new_v = expected_base_version + 1
        if trip:
            trip.current_version = new_v

        v_snapshot = TripVersion(
            trip_id=trip_id,
            version=new_v,
            brief=brief,
            command_id=command_id,
        )
        self.versions[(trip_id, new_v)] = v_snapshot
        self.commands[(trip_id, command_id)] = v_snapshot
        return v_snapshot

    async def get_version(self, trip_id: uuid.UUID, version: int) -> TripVersion | None:
        return self.versions.get((trip_id, version))


class InMemoryJobRepository(JobRepositoryPort):
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


class InMemoryArtifactRepository(ArtifactRepositoryPort):
    def __init__(self) -> None:
        self.artifacts: dict[uuid.UUID, Any] = {}

    async def save(self, artifact: Any) -> None:
        self.artifacts[artifact.id] = artifact

    async def get_by_id(self, artifact_id: uuid.UUID) -> Any | None:
        return self.artifacts.get(artifact_id)

    async def get_by_trip(self, trip_id: uuid.UUID) -> list[Any]:
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


class InMemoryUnitOfWork(UnitOfWorkPort):
    def __init__(self) -> None:
        self.trips = InMemoryTripRepository()
        self.jobs = InMemoryJobRepository()
        self.artifacts = InMemoryArtifactRepository()

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@pytest.fixture
def test_uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


@pytest.mark.asyncio
async def test_health_check() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_create_and_get_trip(test_uow: InMemoryUnitOfWork) -> None:
    app.dependency_overrides[get_uow] = lambda: test_uow
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "owner_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [
                    {
                        "name": "Mysuru",
                        "latitude": 12.2958,
                        "longitude": 76.6394,
                        "stay_days": 2,
                    }
                ],
                "start_date": "2026-10-01",
                "end_date": "2026-10-05",
                "adults": 2,
                "preferred_modes": ["car_petrol"],
            },
        }
        res = await client.post("/api/v1/trips", json=payload)
        assert res.status_code == 201
        data = res.json()
        assert data["current_version"] == 1
        assert data["state"] == "draft"
        trip_id = data["id"]

        # Fetch trip
        get_res = await client.get(f"/api/v1/trips/{trip_id}")
        assert get_res.status_code == 200
        assert get_res.json()["id"] == trip_id

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_brief_conflict_409(test_uow: InMemoryUnitOfWork) -> None:
    app.dependency_overrides[get_uow] = lambda: test_uow
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        owner_id = str(uuid.uuid4())
        trip_id = uuid.uuid4()
        test_uow.trips.trips[trip_id] = Trip(
            id=trip_id, owner_id=uuid.UUID(owner_id), current_version=2, state=TripState.DRAFT
        )

        # Attempt to update with outdated expected_base_version = 1 (actual is 2)
        payload = {
            "expected_base_version": 1,
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [
                    {"name": "Coorg", "latitude": 12.4244, "longitude": 75.7382, "stay_days": 1}
                ],
                "start_date": "2026-10-01",
                "end_date": "2026-10-03",
                "adults": 1,
            },
        }
        res = await client.post(f"/api/v1/trips/{trip_id}/brief", json=payload)
        assert res.status_code == 409
        assert "version conflict" in res.json()["detail"].lower()

    app.dependency_overrides.clear()
