"""Unit and parity tests for JobWorker and Lambda dispatcher."""

import uuid
from datetime import date
from typing import Any

import pytest

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
from travel.runtime.lambda_handler import dispatch_job_async
from travel.workers.worker import JobWorker


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
        self.idempotency_index: dict[tuple[str, str], uuid.UUID] = {}

    async def enqueue(self, job: Job) -> Job:
        key = (job.type, job.idempotency_key)
        if key in self.idempotency_index:
            existing_id = self.idempotency_index[key]
            return self.jobs[existing_id]
        self.jobs[job.id] = job
        self.idempotency_index[key] = job.id
        return job

    async def claim_next(self, worker_id: str, lease_duration_seconds: int = 60) -> Job | None:
        for job in self.jobs.values():
            if job.status == JobStatus.PENDING:
                job.status = JobStatus.RUNNING
                job.owner = worker_id
                job.lease_generation += 1
                return job
        return None

    async def renew_heartbeat(
        self, job_id: uuid.UUID, worker_id: str, lease_generation: int, additional_seconds: int = 60
    ) -> bool:
        return True

    async def complete(self, job_id: uuid.UUID, worker_id: str, lease_generation: int) -> bool:
        job = self.jobs.get(job_id)
        if job:
            job.status = JobStatus.COMPLETED
            job.owner = None
            return True
        return False

    async def fail(self, job_id: uuid.UUID, worker_id: str, error_message: str) -> bool:
        job = self.jobs.get(job_id)
        if job:
            job.status = JobStatus.FAILED
            job.owner = None
            return True
        return False

    async def get_by_id(self, job_id: uuid.UUID) -> Job | None:
        return self.jobs.get(job_id)

    async def cancel(self, job_id: uuid.UUID, reason: str = "") -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
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
def test_setup() -> tuple[MockUnitOfWork, Trip, TripBrief]:
    uow = MockUnitOfWork()
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
    uow.trips.trips[trip_id] = trip
    uow.trips.versions[(trip_id, 1)] = TripVersion(
        trip_id=trip_id, version=1, brief=brief, command_id=uuid.uuid4()
    )
    return uow, trip, brief


@pytest.mark.asyncio
async def test_job_worker_export_lifecycle(
    test_setup: tuple[MockUnitOfWork, Trip, TripBrief],
) -> None:
    uow, trip, brief = test_setup
    artifact_id = uuid.uuid4()

    # Pre-register artifact
    artifact = Artifact(
        id=artifact_id,
        trip_id=trip.id,
        trip_version=1,
        object_key=f"trips/{trip.id}/v1/itinerary.pdf",
    )
    await uow.artifacts.save(artifact)

    # Enqueue export job
    job = Job(
        type="export_itinerary",
        payload={"trip_id": str(trip.id), "version": 1, "artifact_id": str(artifact_id)},
        idempotency_key=f"export_{trip.id}_v1",
    )
    enqueued = await uow.jobs.enqueue(job)
    assert enqueued.status == JobStatus.PENDING

    # Run worker for 1 job
    worker = JobWorker(uow=uow, worker_id="test_worker_1")
    did_process = await worker.process_one_job()
    assert did_process is True

    # Verify job status is completed
    persisted_job = await uow.jobs.get_by_id(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.COMPLETED

    # Verify artifact was marked completed with size and download URL
    updated_artifact = await uow.artifacts.get_by_id(artifact_id)
    assert updated_artifact is not None
    assert updated_artifact.status == ArtifactStatus.COMPLETED
    assert updated_artifact.size_bytes is not None and updated_artifact.size_bytes > 1000
    assert updated_artifact.download_url == f"/api/v1/artifacts/{artifact_id}/download"


@pytest.mark.asyncio
async def test_job_worker_idempotency_deduplication(
    test_setup: tuple[MockUnitOfWork, Trip, TripBrief],
) -> None:
    uow, trip, brief = test_setup

    job1 = Job(
        type="enrichment",
        payload={"trip_id": str(trip.id)},
        idempotency_key="unique_enrich_key",
    )
    enqueued1 = await uow.jobs.enqueue(job1)

    job2 = Job(
        type="enrichment",
        payload={"trip_id": str(trip.id)},
        idempotency_key="unique_enrich_key",
    )
    enqueued2 = await uow.jobs.enqueue(job2)

    # Both returns point to the same job ID
    assert enqueued1.id == enqueued2.id


@pytest.mark.asyncio
async def test_job_worker_cancellation(test_setup: tuple[MockUnitOfWork, Trip, TripBrief]) -> None:
    uow, trip, brief = test_setup

    job = Job(
        type="notification",
        payload={"recipient": "swena_user"},
        idempotency_key="notify_cancel_test",
    )
    await uow.jobs.enqueue(job)

    # Cancel job
    cancelled = await uow.jobs.cancel(job.id)
    assert cancelled is True

    persisted = await uow.jobs.get_by_id(job.id)
    assert persisted is not None
    assert persisted.status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_lambda_dispatcher_parity(test_setup: tuple[MockUnitOfWork, Trip, TripBrief]) -> None:
    uow, trip, brief = test_setup
    artifact_id = uuid.uuid4()

    artifact = Artifact(
        id=artifact_id,
        trip_id=trip.id,
        trip_version=1,
        object_key=f"trips/{trip.id}/v1/itinerary.pdf",
    )
    await uow.artifacts.save(artifact)

    job = Job(
        type="export_itinerary",
        payload={"trip_id": str(trip.id), "version": 1, "artifact_id": str(artifact_id)},
        idempotency_key="lambda_parity_test",
    )
    await uow.jobs.enqueue(job)

    # Execute via Lambda dispatcher function
    result = await dispatch_job_async(job.id, uow, worker_id="lambda_worker_test")
    assert result["status"] == "completed"
    assert result["job_id"] == str(job.id)
    assert result["result"]["size_bytes"] > 1000

    # Verify matching persisted artifact
    updated_artifact = await uow.artifacts.get_by_id(artifact_id)
    assert updated_artifact is not None
    assert updated_artifact.status == ArtifactStatus.COMPLETED
