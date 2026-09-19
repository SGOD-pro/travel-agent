"""SQLAlchemy implementation of ArtifactRepositoryPort."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from travel.application.ports.repository import ArtifactRepositoryPort
from travel.domain.artifacts import Artifact, ArtifactStatus
from travel.persistence.models import ArtifactModel


class SqlAlchemyArtifactRepository(ArtifactRepositoryPort):
    """PostgreSQL-backed repository for exported artifacts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, artifact: Any) -> None:
        assert isinstance(artifact, Artifact)
        model = ArtifactModel(
            id=artifact.id,
            trip_id=artifact.trip_id,
            trip_version=artifact.trip_version,
            object_key=artifact.object_key,
            status=artifact.status.value,
            content_type=artifact.content_type,
            size_bytes=artifact.size_bytes,
            download_url=artifact.download_url,
            meta=artifact.metadata,
            created_at=artifact.created_at,
            expires_at=artifact.expires_at,
        )
        self._session.add(model)

    async def get_by_id(self, artifact_id: uuid.UUID) -> Artifact | None:
        model = await self._session.get(ArtifactModel, artifact_id)
        if not model:
            return None
        return Artifact(
            id=model.id,
            trip_id=model.trip_id,
            trip_version=model.trip_version,
            object_key=model.object_key,
            status=ArtifactStatus(model.status),
            content_type=model.content_type,
            size_bytes=model.size_bytes,
            download_url=model.download_url,
            metadata=model.meta,
            created_at=model.created_at,
            expires_at=model.expires_at,
        )

    async def get_by_trip(self, trip_id: uuid.UUID) -> list[Artifact]:
        result = await self._session.execute(
            select(ArtifactModel)
            .where(ArtifactModel.trip_id == trip_id)
            .order_by(ArtifactModel.created_at.desc())
        )
        models = result.scalars().all()
        return [
            Artifact(
                id=m.id,
                trip_id=m.trip_id,
                trip_version=m.trip_version,
                object_key=m.object_key,
                status=ArtifactStatus(m.status),
                content_type=m.content_type,
                size_bytes=m.size_bytes,
                download_url=m.download_url,
                metadata=m.meta,
                created_at=m.created_at,
                expires_at=m.expires_at,
            )
            for m in models
        ]

    async def update_status(
        self,
        artifact_id: uuid.UUID,
        status: str,
        size_bytes: int | None = None,
        download_url: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if size_bytes is not None:
            values["size_bytes"] = size_bytes
        if download_url is not None:
            values["download_url"] = download_url

        await self._session.execute(
            update(ArtifactModel).where(ArtifactModel.id == artifact_id).values(**values)
        )
