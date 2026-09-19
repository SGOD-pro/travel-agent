"""Durable background JobWorker process.

Executes database-leased jobs with SKIP LOCKED concurrency, heartbeats, and idempotency.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from travel.application.ports.unit_of_work import UnitOfWorkPort
from travel.domain.jobs import Job, JobStatus
from travel.workers.handlers import JOB_HANDLERS

logger = logging.getLogger(__name__)


class JobWorker:
    """Portable worker claiming and processing jobs from the database queue."""

    def __init__(
        self,
        uow: UnitOfWorkPort,
        worker_id: str | None = None,
        poll_interval_seconds: float = 1.0,
        lease_duration_seconds: int = 60,
    ) -> None:
        self._uow = uow
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_duration_seconds = lease_duration_seconds
        self._running = False

    async def execute_job(self, job: Job) -> dict[str, Any]:
        """Executes a single job instance through its registered handler."""
        handler = JOB_HANDLERS.get(job.type)
        if not handler:
            raise ValueError(f"No registered handler for job type: '{job.type}'")

        return await handler(job, self._uow)

    async def process_one_job(self) -> bool:
        """Claims and processes a single job if available. Returns True if a job was processed."""
        async with self._uow:
            job = await self._uow.jobs.claim_next(
                worker_id=self.worker_id,
                lease_duration_seconds=self.lease_duration_seconds,
            )
            await self._uow.commit()

        if not job:
            return False

        logger.info(f"[{self.worker_id}] Claimed job {job.id} (type: {job.type})")

        # Check if cancelled before execution
        if job.status == JobStatus.CANCELLED:
            logger.info(f"[{self.worker_id}] Job {job.id} was cancelled. Skipping.")
            return True

        try:
            await self.execute_job(job)

            async with self._uow:
                # Mark completed
                await self._uow.jobs.complete(
                    job_id=job.id,
                    worker_id=self.worker_id,
                    lease_generation=job.lease_generation,
                )
                await self._uow.commit()

            logger.info(f"[{self.worker_id}] Job {job.id} completed successfully")
            return True

        except Exception as e:
            logger.error(f"[{self.worker_id}] Job {job.id} failed: {e}", exc_info=True)
            async with self._uow:
                await self._uow.jobs.fail(
                    job_id=job.id,
                    worker_id=self.worker_id,
                    error_message=str(e),
                )
                await self._uow.commit()
            return True

    async def run(self, max_jobs: int | None = None) -> int:
        """Runs the worker loop until stopped or max_jobs reached."""
        self._running = True
        jobs_processed = 0

        while self._running:
            did_work = await self.process_one_job()
            if did_work:
                jobs_processed += 1
                if max_jobs and jobs_processed >= max_jobs:
                    break
            else:
                await asyncio.sleep(self.poll_interval_seconds)

        return jobs_processed

    def stop(self) -> None:
        """Signals worker to stop running."""
        self._running = False
