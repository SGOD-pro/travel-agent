"""Domain models for Durable Jobs and Transactional Outbox Events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(BaseModel):
    """Durable background job claimed via database lease."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    type: str = Field(..., description="Job identifier/handler type")
    run_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = Field(default=JobStatus.PENDING)
    available_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    lease_until: datetime | None = None
    lease_generation: int = Field(default=0, ge=0)
    owner: str | None = None
    idempotency_key: str = Field(..., description="Unique key for deduplication")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_claimable(self, now: datetime) -> bool:
        if self.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return False
        if self.available_at > now:
            return False
        if self.lease_until is None or self.lease_until < now:
            return True
        return False


class OutboxEvent(BaseModel):
    """Transactional outbox event published alongside aggregate changes."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    aggregate_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    delivered_at: datetime | None = None
    attempts: int = Field(default=0, ge=0)
