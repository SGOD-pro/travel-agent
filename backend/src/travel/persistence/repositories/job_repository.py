"""SQLAlchemy implementation of JobRepositoryPort."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from travel.application.ports.repository import JobRepositoryPort
from travel.domain.jobs import Job, JobStatus
from travel.persistence.models import JobModel


class SqlAlchemyJobRepository(JobRepositoryPort):
    """PostgreSQL-backed job queue with SKIP LOCKED claiming and lease fencing."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, job: Job) -> Job:
        existing = await self._session.execute(
            select(JobModel).where(
                JobModel.type == job.type,
                JobModel.idempotency_key == job.idempotency_key,
            )
        )
        model = existing.scalar_one_or_none()
        if model:
            return Job(
                id=model.id,
                type=model.type,
                run_id=model.run_id,
                payload=model.payload,
                status=JobStatus(model.status),
                available_at=model.available_at,
                attempts=model.attempts,
                max_attempts=model.max_attempts,
                lease_until=model.lease_until,
                lease_generation=model.lease_generation,
                owner=model.owner,
                idempotency_key=model.idempotency_key,
                created_at=model.created_at,
                updated_at=model.updated_at,
            )

        model = JobModel(
            id=job.id,
            type=job.type,
            run_id=job.run_id,
            payload=job.payload,
            status=job.status.value,
            available_at=job.available_at,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            lease_until=job.lease_until,
            lease_generation=job.lease_generation,
            owner=job.owner,
            idempotency_key=job.idempotency_key,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
        self._session.add(model)
        return job

    async def claim_next(self, worker_id: str, lease_duration_seconds: int = 60) -> Job | None:
        now = datetime.now(UTC)
        claimable_filter = and_(
            JobModel.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
            JobModel.available_at <= now,
            or_(JobModel.lease_until.is_(None), JobModel.lease_until < now),
        )

        stmt = (
            select(JobModel)
            .where(claimable_filter)
            .order_by(JobModel.available_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if not model:
            return None

        lease_expiry = now + timedelta(seconds=lease_duration_seconds)
        model.status = JobStatus.RUNNING.value
        model.attempts += 1
        model.lease_until = lease_expiry
        model.lease_generation += 1
        model.owner = worker_id
        model.updated_at = now

        return Job(
            id=model.id,
            type=model.type,
            run_id=model.run_id,
            payload=model.payload,
            status=JobStatus.RUNNING,
            available_at=model.available_at,
            attempts=model.attempts,
            max_attempts=model.max_attempts,
            lease_until=model.lease_until,
            lease_generation=model.lease_generation,
            owner=model.owner,
            idempotency_key=model.idempotency_key,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def renew_heartbeat(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        lease_generation: int,
        additional_seconds: int = 60,
    ) -> bool:
        now = datetime.now(UTC)
        new_expiry = now + timedelta(seconds=additional_seconds)

        stmt = (
            update(JobModel)
            .where(
                JobModel.id == job_id,
                JobModel.owner == worker_id,
                JobModel.lease_generation == lease_generation,
                JobModel.status == JobStatus.RUNNING.value,
            )
            .values(lease_until=new_expiry, updated_at=now)
        )
        result = await self._session.execute(stmt)
        assert isinstance(result, CursorResult)
        return bool(result.rowcount > 0)

    async def complete(self, job_id: uuid.UUID, worker_id: str, lease_generation: int) -> bool:
        now = datetime.now(UTC)
        stmt = (
            update(JobModel)
            .where(
                JobModel.id == job_id,
                JobModel.owner == worker_id,
                JobModel.lease_generation == lease_generation,
            )
            .values(status=JobStatus.COMPLETED.value, lease_until=None, updated_at=now)
        )
        result = await self._session.execute(stmt)
        assert isinstance(result, CursorResult)
        return bool(result.rowcount > 0)

    async def fail(self, job_id: uuid.UUID, worker_id: str, error_message: str) -> bool:
        now = datetime.now(UTC)
        model = await self._session.get(JobModel, job_id)
        if not model or model.owner != worker_id:
            return False

        if model.attempts >= model.max_attempts:
            model.status = JobStatus.FAILED.value
        else:
            # Exponential backoff
            delay = 2 ** min(model.attempts, 6) * 5
            model.status = JobStatus.PENDING.value
            model.available_at = now + timedelta(seconds=delay)

        model.lease_until = None
        model.updated_at = now
        return True

    async def get_by_id(self, job_id: uuid.UUID) -> Job | None:
        model = await self._session.get(JobModel, job_id)
        if not model:
            return None
        return Job(
            id=model.id,
            type=model.type,
            run_id=model.run_id,
            payload=model.payload,
            status=JobStatus(model.status),
            available_at=model.available_at,
            attempts=model.attempts,
            max_attempts=model.max_attempts,
            lease_until=model.lease_until,
            lease_generation=model.lease_generation,
            owner=model.owner,
            idempotency_key=model.idempotency_key,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def cancel(self, job_id: uuid.UUID, reason: str = "") -> bool:
        now = datetime.now(UTC)
        model = await self._session.get(JobModel, job_id)
        if not model:
            return False
        if model.status in {
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        }:
            return False
        model.status = JobStatus.CANCELLED.value
        model.lease_until = None
        model.updated_at = now
        return True
