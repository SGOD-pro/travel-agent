"""Integration tests for bounded transactional outbox relay slice conforming to spec 0001.

Tests against live PostgreSQL with READ COMMITTED isolation:
1. Successful relay: claim due event, commit claim, publish via transport, fenced delivery confirmation.
2. Concurrent relays: multiple workers using SKIP LOCKED claim disjoint batches without contention.
3. Transport failure: failure records error, increments attempts, and applies capped retry backoff.
4. Exhausted delivery policy: max attempts failure transitions event to failed_at, removing from active retry.
5. Expired relay ownership fencing: stale relay tokens are fenced out from delivery confirmations.
6. Crash before publishing: unfulfilled claim reclaims cleanly after lease expiration.
7. Crash after publishing before confirmation: event ID is strictly preserved across repeated publish.
8. Bounded batch size and invocation duration: relay loop halts cleanly when time budget expires.
9. Provider timeout bound: slow transport calls time out cleanly and trigger retry backoff.
"""

from __future__ import annotations

import asyncio
import re
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.config import settings
from functions.api.models import TripModel
from functions.orchestrator.execution import (
    ControlledTransportAdapter,
    ExecutionModel,
    OutboxEventModel,
    OutboxMessage,
    OutboxRelayService,
)

# In-process DNS cache to prevent transient WSL forwarder socket.gaierror drops
_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
    try:
        res = _orig_getaddrinfo(host, port, *args, **kwargs)
        _dns_cache[key] = res
        return res
    except socket.gaierror:
        if key in _dns_cache:
            return _dns_cache[key]
        raise


socket.getaddrinfo = _resilient_getaddrinfo


@pytest.fixture
async def session_factory():
    url = settings.DATABASE_URL
    connect_args = {}
    if (
        "sslmode=require" in url
        or "ssl=require" in url
        or "aivencloud.com" in url
        or settings.DB_SSL
    ):
        connect_args["ssl"] = "require"
    if "sslmode=" in url:
        url = re.sub(r"[?&]sslmode=[^&]+", "", url)
        if "?" not in url and "&" in url:
            url = url.replace("&", "?", 1)
    test_engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await test_engine.dispose()


async def create_test_execution_and_outbox_event(
    session_factory,
    *,
    execution_id: uuid.UUID | None = None,
    kind: str = "TASK_DISPATCH",
    payload: dict[str, Any] | None = None,
    due_at: datetime | None = None,
    attempts: int = 0,
    delivered_at: datetime | None = None,
    failed_at: datetime | None = None,
    lease_token: uuid.UUID | None = None,
    lease_expiry: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper creating a test execution and outbox event record."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    exec_id = execution_id or uuid.uuid4()
    event_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as session:
        # Check if execution already exists
        existing_exec = (
            await session.execute(select(ExecutionModel).where(ExecutionModel.id == exec_id))
        ).scalar_one_or_none()

        if existing_exec is None:
            trip = TripModel(
                id=trip_id,
                owner_id=owner_id,
                current_version=1,
                state="draft",
                created_at=now,
                updated_at=now,
            )
            session.add(trip)

            execution = ExecutionModel(
                id=exec_id,
                owner_id=owner_id,
                trip_id=trip_id,
                workflow="TRAVEL",
                status="RUNNING",
                context_version=1,
                deadline_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
            session.add(execution)

        payload_data = payload or {"action": "collect", "worker": "EVIDENCE"}
        outbox = OutboxEventModel(
            id=event_id,
            execution_id=exec_id,
            aggregate_id=exec_id,
            task_id=None,
            kind=kind,
            event_type=kind,
            payload_ref=payload_data,
            payload=payload_data,
            due_at=due_at or now,
            attempts=attempts,
            delivered_at=delivered_at,
            failed_at=failed_at,
            last_error=None,
            lease_token=lease_token,
            lease_expiry=lease_expiry,
            created_at=now,
        )
        session.add(outbox)
        await session.commit()

    return exec_id, event_id


@pytest.mark.asyncio
async def test_successful_relay_claim_publish_and_delivery_confirmation(session_factory) -> None:
    """Verifies that due events are claimed, committed, published via transport, and marked delivered."""
    execution_id, event_id = await create_test_execution_and_outbox_event(session_factory)
    transport = ControlledTransportAdapter()
    service = OutboxRelayService(session_factory, transport, batch_size=10, lease_seconds=30)

    relay_token = uuid.uuid4()

    # 1. Claim due batch
    batch = await service.claim_due_batch(
        relay_token=relay_token,
        execution_id=execution_id,
    )
    assert any(m.event_id == event_id for m in batch)
    msg = next(m for m in batch if m.event_id == event_id)
    assert msg.execution_id == execution_id
    assert msg.attempts == 0

    # Verify claim is durably committed in PostgreSQL before transport is called
    async with session_factory() as session:
        row = (
            await session.execute(
                select(OutboxEventModel).where(OutboxEventModel.id == event_id)
            )
        ).scalar_one()
        assert row.lease_token == relay_token
        assert row.lease_expiry is not None
        assert row.lease_expiry > datetime.now(UTC)
        assert row.delivered_at is None

    # 2. Process batch through transport
    batch_res = await service.process_batch(batch, relay_token=relay_token)
    assert batch_res.delivered_count >= 1

    # 3. Verify message was received by transport
    assert any(m.event_id == event_id for m in transport.published)

    # 4. Verify delivered status in PostgreSQL
    async with session_factory() as session:
        row = (
            await session.execute(
                select(OutboxEventModel).where(OutboxEventModel.id == event_id)
            )
        ).scalar_one()
        assert row.delivered_at is not None
        assert row.attempts == 1
        assert row.lease_token is None
        assert row.lease_expiry is None


@pytest.mark.asyncio
async def test_concurrent_relays_skip_locked(session_factory) -> None:
    """Verifies that multiple concurrent relay workers claim disjoint batches with zero double-delivery."""
    shared_exec_id = uuid.uuid4()
    total_events = 15
    event_ids: list[uuid.UUID] = []
    for _ in range(total_events):
        _, eid = await create_test_execution_and_outbox_event(
            session_factory,
            execution_id=shared_exec_id,
        )
        event_ids.append(eid)

    transport = ControlledTransportAdapter()
    concurrency = 5

    async def run_worker() -> list[OutboxMessage]:
        svc = OutboxRelayService(session_factory, transport, batch_size=5, lease_seconds=30)
        token = uuid.uuid4()
        claimed = await svc.claim_due_batch(
            relay_token=token,
            execution_id=shared_exec_id,
        )
        await svc.process_batch(claimed, relay_token=token)
        return claimed

    results = await asyncio.gather(*(run_worker() for _ in range(concurrency)))

    # Verify all claimed event IDs are completely disjoint
    all_claimed_ids = [m.event_id for batch in results for m in batch if m.event_id in event_ids]
    assert len(all_claimed_ids) == len(set(all_claimed_ids))
    assert len(all_claimed_ids) == total_events

    # Verify all 15 events are delivered in database
    async with session_factory() as session:
        delivered_rows = (
            await session.execute(
                select(OutboxEventModel).where(
                    OutboxEventModel.id.in_(event_ids),
                    OutboxEventModel.delivered_at.is_not(None),
                )
            )
        ).scalars().all()
        assert len(delivered_rows) == total_events


@pytest.mark.asyncio
async def test_transport_failure_capped_exponential_backoff(session_factory) -> None:
    """Verifies that transport failure increments attempts and schedules retry with exponential backoff."""
    execution_id, event_id = await create_test_execution_and_outbox_event(session_factory, attempts=0)

    transport = ControlledTransportAdapter()
    transport.fail_event_ids.add(event_id)
    service = OutboxRelayService(
        session_factory,
        transport,
        base_backoff_seconds=30,
        max_backoff_seconds=60,
        max_attempts=5,
    )

    relay_token = uuid.uuid4()
    batch = await service.claim_due_batch(
        relay_token=relay_token,
        execution_id=execution_id,
    )
    assert any(m.event_id == event_id for m in batch)

    batch_res = await service.process_batch(batch, relay_token=relay_token)
    assert batch_res.retry_scheduled_count >= 1

    # Verify state in database
    async with session_factory() as session:
        row = (
            await session.execute(
                select(OutboxEventModel).where(OutboxEventModel.id == event_id)
            )
        ).scalar_one()
        assert row.delivered_at is None
        assert row.attempts == 1
        assert row.last_error is not None
        assert "Controlled transport network failure" in row.last_error
        # due_at updated into the future
        assert row.due_at > datetime.now(UTC)
        assert row.lease_token is None
        assert row.lease_expiry is None


@pytest.mark.asyncio
async def test_explicit_exhausted_delivery_policy(session_factory) -> None:
    """Verifies that an event reaching max_attempts is permanently marked failed_at and excluded from retries."""
    execution_id, event_id = await create_test_execution_and_outbox_event(session_factory, attempts=4)

    transport = ControlledTransportAdapter()
    transport.fail_event_ids.add(event_id)
    service = OutboxRelayService(
        session_factory,
        transport,
        max_attempts=5,
    )

    relay_token = uuid.uuid4()
    batch = await service.claim_due_batch(
        relay_token=relay_token,
        execution_id=execution_id,
    )
    assert any(m.event_id == event_id for m in batch)

    batch_res = await service.process_batch(batch, relay_token=relay_token)
    assert batch_res.exhausted_count >= 1

    # Verify database state has failed_at set
    async with session_factory() as session:
        row = (
            await session.execute(
                select(OutboxEventModel).where(OutboxEventModel.id == event_id)
            )
        ).scalar_one()
        assert row.delivered_at is None
        assert row.failed_at is not None
        assert row.attempts == 5
        assert row.last_error is not None
        assert row.lease_token is None

    # Next claim run must completely ignore the exhausted event
    fresh_token = uuid.uuid4()
    next_batch = await service.claim_due_batch(
        relay_token=fresh_token,
        execution_id=execution_id,
    )
    assert not any(m.event_id == event_id for m in next_batch)


@pytest.mark.asyncio
async def test_expired_relay_ownership_stale_write_fenced(session_factory) -> None:
    """Verifies that a late worker whose lease expired is fenced out from confirming delivery."""
    now = datetime.now(UTC)
    worker_a_token = uuid.uuid4()
    expired_lease = now - timedelta(seconds=5)

    execution_id, event_id = await create_test_execution_and_outbox_event(
        session_factory,
        lease_token=worker_a_token,
        lease_expiry=expired_lease,
    )

    transport = ControlledTransportAdapter()
    service = OutboxRelayService(session_factory, transport, lease_seconds=30)

    # Worker B claims the expired event
    worker_b_token = uuid.uuid4()
    batch_b = await service.claim_due_batch(
        relay_token=worker_b_token,
        execution_id=execution_id,
    )
    assert any(m.event_id == event_id for m in batch_b)

    # Worker A wakes up late and attempts to confirm delivery -> FENCED OUT!
    fenced_confirmed = await service.confirm_delivery(
        event_id=event_id,
        relay_token=worker_a_token,
    )
    assert fenced_confirmed is False

    # Worker B publishes and confirms delivery -> SUCCESS!
    confirmed_b = await service.confirm_delivery(
        event_id=event_id,
        relay_token=worker_b_token,
    )
    assert confirmed_b is True

    async with session_factory() as session:
        row = (
            await session.execute(
                select(OutboxEventModel).where(OutboxEventModel.id == event_id)
            )
        ).scalar_one()
        assert row.delivered_at is not None
        assert row.attempts == 1


@pytest.mark.asyncio
async def test_crash_before_publishing_reclaim(session_factory) -> None:
    """Verifies that an event claimed by a worker that crashes before publishing is cleanly reclaimed after expiry."""
    now = datetime.now(UTC)
    crashed_worker_token = uuid.uuid4()
    expired_lease = now - timedelta(seconds=1)

    execution_id, event_id = await create_test_execution_and_outbox_event(
        session_factory,
        lease_token=crashed_worker_token,
        lease_expiry=expired_lease,
        attempts=0,
    )

    transport = ControlledTransportAdapter()
    service = OutboxRelayService(session_factory, transport, lease_seconds=30)

    # Worker 2 reclaims and delivers
    worker_2_token = uuid.uuid4()
    batch = await service.claim_due_batch(
        relay_token=worker_2_token,
        execution_id=execution_id,
    )
    assert any(m.event_id == event_id for m in batch)

    batch_res = await service.process_batch(batch, relay_token=worker_2_token)
    assert batch_res.delivered_count >= 1

    assert any(m.event_id == event_id for m in transport.published)


@pytest.mark.asyncio
async def test_crash_after_publishing_before_confirmation_preserves_event_id(session_factory) -> None:
    """Verifies that when a crash occurs after publish but before confirmation, re-delivery preserves exact event_id."""
    execution_id, event_id = await create_test_execution_and_outbox_event(session_factory)

    transport = ControlledTransportAdapter()
    service = OutboxRelayService(session_factory, transport, lease_seconds=1)

    worker_1_token = uuid.uuid4()
    batch_1 = await service.claim_due_batch(
        relay_token=worker_1_token,
        execution_id=execution_id,
    )
    msg_1 = next(m for m in batch_1 if m.event_id == event_id)

    # Simulate: Worker 1 publishes message to transport
    await transport.publish(msg_1)
    assert len(transport.published) == 1
    assert transport.published[0].event_id == event_id

    # Simulate: Worker 1 crashes before calling confirm_delivery!
    # Artificially expire lease in database
    async with session_factory() as session:
        row = (
            await session.execute(
                select(OutboxEventModel).where(OutboxEventModel.id == event_id)
            )
        ).scalar_one()
        row.lease_expiry = datetime.now(UTC) - timedelta(seconds=5)
        await session.commit()

    # Worker 2 runs and reclaims the unconfirmed event
    worker_2_token = uuid.uuid4()
    batch_2 = await service.claim_due_batch(
        relay_token=worker_2_token,
        execution_id=execution_id,
    )
    assert any(m.event_id == event_id for m in batch_2)

    await service.process_batch(batch_2, relay_token=worker_2_token)
    assert len(transport.published) == 2

    # CRITICAL: Verify both published messages have the EXACT same event_id
    assert transport.published[0].event_id == event_id
    assert transport.published[1].event_id == event_id
    assert transport.published[0].execution_id == transport.published[1].execution_id

    # Verify event is now delivered
    async with session_factory() as session:
        row = (
            await session.execute(
                select(OutboxEventModel).where(OutboxEventModel.id == event_id)
            )
        ).scalar_one()
        assert row.delivered_at is not None


@pytest.mark.asyncio
async def test_bounded_invocation_duration_and_batch_size(session_factory) -> None:
    """Verifies that the relay loop respects the invocation duration budget and halts cleanly."""
    shared_exec_id = uuid.uuid4()
    # Seed 10 events under shared execution
    for _ in range(10):
        await create_test_execution_and_outbox_event(
            session_factory,
            execution_id=shared_exec_id,
        )

    transport = ControlledTransportAdapter()
    # Inject 0.1s delay per publish
    transport.delay_seconds = 0.1

    # Max runtime of 0.25 seconds: cannot process all 10 events
    service = OutboxRelayService(
        session_factory,
        transport,
        batch_size=2,
        max_runtime_seconds=0.25,
    )

    summary = await service.run_relay_loop(
        max_runtime_seconds=0.25,
        execution_id=shared_exec_id,
    )
    assert summary.terminated_by_timeout is True
    assert summary.batches_processed >= 1
    assert summary.total_delivered < 10


@pytest.mark.asyncio
async def test_provider_timeout_bounded(session_factory) -> None:
    """Verifies that transport calls exceeding provider_timeout_seconds are timed out and scheduled for retry."""
    execution_id, event_id = await create_test_execution_and_outbox_event(session_factory)

    transport = ControlledTransportAdapter()
    # Delay exceeds provider timeout
    transport.delay_seconds = 0.5

    service = OutboxRelayService(
        session_factory,
        transport,
        provider_timeout_seconds=0.1,  # Short timeout
        base_backoff_seconds=30,
    )

    token = uuid.uuid4()
    batch = await service.claim_due_batch(
        relay_token=token,
        execution_id=execution_id,
    )
    assert any(m.event_id == event_id for m in batch)

    batch_res = await service.process_batch(batch, relay_token=token)
    assert batch_res.retry_scheduled_count >= 1

    # Verify error recorded in database
    async with session_factory() as session:
        row = (
            await session.execute(
                select(OutboxEventModel).where(OutboxEventModel.id == event_id)
            )
        ).scalar_one()
        assert row.delivered_at is None
        assert row.attempts == 1
        assert row.due_at > datetime.now(UTC)
