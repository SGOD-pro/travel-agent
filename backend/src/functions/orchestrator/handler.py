"""AWS Lambda dispatcher entrypoint for portable asynchronous jobs.

Enforces strict parity with the local JobWorker by delegating to the same JOB_HANDLERS.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from config.db import get_session_factory
from contracts.jobs import JobStatus
from functions.api.repository import SqlAlchemyUnitOfWork, UnitOfWorkPort
from functions.orchestrator.handlers import JOB_HANDLERS


def get_uow() -> UnitOfWorkPort:
    return SqlAlchemyUnitOfWork(get_session_factory())


async def dispatch_job_async(
    job_id: uuid.UUID,
    uow: UnitOfWorkPort,
    worker_id: str = "lambda_worker",
) -> dict[str, Any]:
    """Claims and executes a specific job ID by invoking its registered handler."""
    async with uow:
        job = await uow.jobs.get_by_id(job_id)
        if not job:
            return {"status": "error", "error": f"Job {job_id} not found"}

        if job.status == JobStatus.CANCELLED:
            return {"status": "cancelled", "job_id": str(job_id)}

        handler = JOB_HANDLERS.get(job.type)
        if not handler:
            return {"status": "error", "error": f"Unsupported job type {job.type}"}

        try:
            result = await handler(job, uow)
            await uow.jobs.complete(job.id, worker_id, job.lease_generation)
            await uow.commit()
            return {"status": "completed", "job_id": str(job_id), "result": result}
        except Exception as e:
            await uow.jobs.fail(job.id, worker_id, str(e))
            await uow.commit()
            return {"status": "failed", "job_id": str(job_id), "error": str(e)}


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Standard AWS Lambda entrypoint.

    Accepts:
        {"job_id": "..."}
    or SQS batch event with Records containing body with job_id.
    """
    job_id_str = event.get("job_id")
    if not job_id_str and "Records" in event and len(event["Records"]) > 0:
        import json

        body = json.loads(event["Records"][0]["body"])
        job_id_str = body.get("job_id")

    if not job_id_str:
        return {"statusCode": 400, "body": "Missing job_id in event"}

    job_id = uuid.UUID(job_id_str)
    uow = get_uow()

    return asyncio.run(dispatch_job_async(job_id, uow))
