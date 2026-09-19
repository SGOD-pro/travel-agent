"""Unit tests for Durable Jobs and Outbox invariants."""

import uuid
from datetime import UTC, datetime, timedelta

from travel.domain.jobs import Job, JobStatus, OutboxEvent


def test_job_claimable_states() -> None:
    now = datetime.now(UTC)
    job = Job(
        type="enrichment",
        idempotency_key="key-1",
        status=JobStatus.PENDING,
        available_at=now - timedelta(seconds=10),
    )
    assert job.is_claimable(now) is True

    # Future available_at -> not claimable
    job.available_at = now + timedelta(minutes=5)
    assert job.is_claimable(now) is False

    # Active lease -> not claimable
    job.available_at = now - timedelta(seconds=10)
    job.lease_until = now + timedelta(seconds=30)
    assert job.is_claimable(now) is False

    # Expired lease -> claimable (reclaimed by recovery worker)
    job.lease_until = now - timedelta(seconds=5)
    assert job.is_claimable(now) is True

    # Completed -> not claimable
    job.status = JobStatus.COMPLETED
    assert job.is_claimable(now) is False


def test_outbox_event_creation() -> None:
    agg_id = uuid.uuid4()
    event = OutboxEvent(
        aggregate_id=agg_id,
        event_type="TripBriefUpdated",
        payload={"version": 2},
    )
    assert event.delivered_at is None
    assert event.attempts == 0
    assert event.aggregate_id == agg_id
