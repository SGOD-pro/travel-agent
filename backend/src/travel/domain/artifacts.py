"""Domain models for Exported Artifacts conforming to docs/DATABASE-SCHEMA.md."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ArtifactStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class Artifact(BaseModel):
    """Exported travel artifact (PDF, JSON, etc.) bound to a trip version."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    trip_id: uuid.UUID
    trip_version: int = Field(..., ge=1)
    object_key: str = Field(..., description="Unique storage object path or S3 key")
    status: ArtifactStatus = Field(default=ArtifactStatus.PENDING)
    content_type: str = Field(default="application/pdf")
    size_bytes: int | None = None
    download_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    def mark_completed(self, size_bytes: int, download_url: str) -> None:
        self.status = ArtifactStatus.COMPLETED
        self.size_bytes = size_bytes
        self.download_url = download_url

    def mark_failed(self, error: str) -> None:
        self.status = ArtifactStatus.FAILED
        self.metadata["error"] = error
