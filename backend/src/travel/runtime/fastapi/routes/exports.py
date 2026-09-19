"""FastAPI routes for trip exports and artifact downloads."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from travel.application.ports.unit_of_work import UnitOfWorkPort
from travel.domain.artifacts import Artifact, ArtifactStatus
from travel.domain.jobs import Job, JobStatus
from travel.infrastructure.config import settings
from travel.persistence.unit_of_work import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["exports"])

engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_uow() -> UnitOfWorkPort:
    return SqlAlchemyUnitOfWork(async_session_factory)


UowDep = Annotated[UnitOfWorkPort, Depends(get_uow)]


class ExportTripRequest(BaseModel):
    trip_version: int = Field(default=1, ge=1)
    format: str = Field(default="pdf", pattern="^(pdf|json)$")


class ExportJobResponse(BaseModel):
    artifact_id: uuid.UUID
    job_id: uuid.UUID
    status: str
    trip_id: uuid.UUID
    trip_version: int


class ArtifactResponse(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    trip_version: int
    object_key: str
    status: str
    content_type: str
    size_bytes: int | None = None
    download_url: str | None = None


class CancelJobResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    cancelled: bool


@router.post(
    "/trips/{trip_id}/exports",
    response_model=ExportJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_trip_export(
    trip_id: uuid.UUID,
    request: ExportTripRequest,
    uow: UowDep,
) -> ExportJobResponse:
    """Enqueues an asynchronous export job and returns pending artifact tracking tokens."""
    async with uow:
        trip = await uow.trips.get_by_id(trip_id)
        if not trip:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Trip {trip_id} not found",
            )

        snapshot = await uow.trips.get_version(trip_id, request.trip_version)
        if not snapshot:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Trip {trip_id} version {request.trip_version} does not exist",
            )

        artifact_id = uuid.uuid4()
        object_key = (
            f"trips/{trip_id}/v{request.trip_version}/itinerary_{artifact_id}.{request.format}"
        )

        artifact = Artifact(
            id=artifact_id,
            trip_id=trip_id,
            trip_version=request.trip_version,
            object_key=object_key,
            status=ArtifactStatus.PENDING,
            content_type="application/pdf" if request.format == "pdf" else "application/json",
        )
        await uow.artifacts.save(artifact)

        job = Job(
            type="export_itinerary",
            payload={
                "trip_id": str(trip_id),
                "version": request.trip_version,
                "artifact_id": str(artifact_id),
                "format": request.format,
            },
            status=JobStatus.PENDING,
            idempotency_key=f"export_{trip_id}_v{request.trip_version}_{artifact_id}",
        )
        enqueued_job = await uow.jobs.enqueue(job)
        await uow.commit()

    return ExportJobResponse(
        artifact_id=artifact_id,
        job_id=enqueued_job.id,
        status="pending",
        trip_id=trip_id,
        trip_version=request.trip_version,
    )


@router.get("/artifacts/{artifact_id}", response_model=ArtifactResponse)
async def get_artifact(
    artifact_id: uuid.UUID,
    uow: UowDep,
) -> ArtifactResponse:
    """Fetches status and metadata of an exported artifact."""
    async with uow:
        artifact = await uow.artifacts.get_by_id(artifact_id)
        if not artifact:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Artifact {artifact_id} not found",
            )

    return ArtifactResponse(
        id=artifact.id,
        trip_id=artifact.trip_id,
        trip_version=artifact.trip_version,
        object_key=artifact.object_key,
        status=artifact.status.value,
        content_type=artifact.content_type,
        size_bytes=artifact.size_bytes,
        download_url=artifact.download_url,
    )


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: uuid.UUID,
    uow: UowDep,
) -> FileResponse:
    """Downloads the generated artifact file."""
    async with uow:
        artifact = await uow.artifacts.get_by_id(artifact_id)
        if not artifact:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Artifact {artifact_id} not found",
            )
        if artifact.status != ArtifactStatus.COMPLETED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Artifact {artifact_id} is not yet ready (current status: {artifact.status.value})",
            )

    storage_dir = Path(os.getenv("ARTIFACT_STORAGE_DIR", "/tmp/swena_artifacts"))
    file_path = storage_dir / f"{artifact_id}.pdf"
    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact file payload not found in storage",
        )

    return FileResponse(
        path=file_path,
        media_type=artifact.content_type,
        filename=f"SWENA_Itinerary_Trip_{artifact.trip_id}_v{artifact.trip_version}.pdf",
    )


@router.post("/jobs/{job_id}/cancel", response_model=CancelJobResponse)
async def cancel_job(
    job_id: uuid.UUID,
    uow: UowDep,
) -> CancelJobResponse:
    """Cancels a pending or running job."""
    async with uow:
        cancelled = await uow.jobs.cancel(job_id, reason="User cancellation request")
        await uow.commit()

    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} cannot be cancelled (may be completed or does not exist)",
        )

    return CancelJobResponse(
        job_id=job_id,
        status="cancelled",
        cancelled=True,
    )
