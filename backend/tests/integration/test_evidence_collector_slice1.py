"""Integration and behavioral tests for 0002 Slice 1 conforming to spec 0002.

Verifies:
1. Offline fixture isolation without live network access or SerpAPI keys.
2. Actual TASK_DISPATCH serialized envelope parsing distinguishing outbox attempts from task attempts.
3. Atomic completion boundary committing source_observations, worker_results, tasks, and outbox_events.
4. Clean observation rollback when complete_task rejects fencing (stale lease, expired lease, cancellation, deadline).
5. Unchanged replay snapshot reuse returning existing persisted row IDs with zero DO UPDATE mutations.
6. Changed content preservation under the same URL creating distinct snapshots.
7. Distinct URL attribution with identical content hashes.
8. Local budget iteration bounds and usage reporting.
9. Dedicated collector SQS Lambda consumer execution.
10. Regression: normalize_url preserves non-tracking params (source_id, ref_id, reference).
11. Regression: ExecutionEngineError produces a batchItemFailure instead of silent ack.
12. Regression: savepoint failure inside complete_task rolls back staged observations.
13. Regression: budget-bounded call persists its observations in the database.
"""

from __future__ import annotations

import json
import unittest.mock
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from functions.api.models import TripModel
from functions.evidence_collector.consumer import process_batch
from functions.evidence_collector.fixtures import (
    FixtureSearchAdapter,
    RawObservation,
    compute_content_hash,
    normalize_url,
)
from functions.evidence_collector.models import SourceObservationModel
from functions.evidence_collector.service import (
    EvidenceCollectorService,
    TaskDispatchEnvelope,
    parse_task_dispatch_envelope,
)
from functions.orchestrator.execution.fencing import (
    DeadlineExceededError,
    ExecutionModel,
    LeaseFencingError,
    OutboxEventModel,
    ParentCancelledError,
    TaskModel,
    TaskQueueMessage,
    WorkerResultModel,
)


async def create_test_evidence_task(
    session_factory: Any,
    *,
    execution_id: uuid.UUID | None = None,
    worker: str = "EVIDENCE",
    action: str = "collect",
    context_version: int = 1,
    attempt: int = 0,
    max_attempts: int = 3,
    status: str = "PENDING",
    lease_token: uuid.UUID | None = None,
    lease_expiry: datetime | None = None,
    parent_status: str = "CREATED",
    parent_cancelled_at: datetime | None = None,
    deadline_minutes: int = 60,
    task_deadline_minutes: int = 60,
    payload: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper creating a test execution and task record."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    task_id = uuid.uuid4()
    now = datetime.now(UTC)

    default_payload = {
        "queries": ["pujor Kolkata traffic advisory 2026", "Dakshineswar temple opening timings"],
        "claim_scope": {"locality": "Kolkata", "travel_date": "2026-10-20"},
        "allowed_sources": ["official", "attributable_local"],
        "budget": {
            "remaining_tool_calls": 5,
            "remaining_cost_minor": 500,
            "currency": "INR",
            "remaining_iterations": 2,
        },
    }

    async with session_factory() as session:
        if execution_id is None:
            execution_id = uuid.uuid4()
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
                id=execution_id,
                owner_id=owner_id,
                trip_id=trip_id,
                workflow="TRAVEL",
                status=parent_status,
                context_version=context_version,
                deadline_at=now + timedelta(minutes=deadline_minutes),
                cancelled_at=parent_cancelled_at,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)

        task = TaskModel(
            id=task_id,
            execution_id=execution_id,
            parent_task_id=None,
            worker=worker,
            action=action,
            payload_ref=payload if payload is not None else default_payload,
            input_hash="test_sha256_hash",
            idempotency_key=f"task-idem-{task_id}",
            attempt=attempt,
            max_attempts=max_attempts,
            context_version=context_version,
            status=status,
            lease_token=lease_token,
            lease_expiry=lease_expiry,
            deadline_at=now + timedelta(minutes=task_deadline_minutes),
            result_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.commit()

    return execution_id, task_id


# -----------------------------------------------------------------------------
# 1. Test offline fixture isolation
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_offline_fixture_isolation() -> None:
    """Verifies that fixture adapters operate completely offline with SHA256 hashes and URL normalization."""
    adapter = FixtureSearchAdapter()
    assert adapter.is_test_fixture is True

    results = await adapter.search("Kolkata traffic advisory", limit=2)
    assert len(results) >= 1
    first = results[0]

    # Verify SHA256 hashing and URL normalization
    expected_hash = compute_content_hash(first.excerpt_text)
    assert first.content_hash == expected_hash
    assert first.canonical_url == normalize_url(first.source_url)
    assert not first.canonical_url.endswith("/")

    # Verify tracking parameter removal
    dirty_url = "https://example.com/advisory?utm_source=twitter&utm_medium=social&id=123"
    norm_url = normalize_url(dirty_url)
    assert "utm_source" not in norm_url
    assert "utm_medium" not in norm_url
    assert "id=123" in norm_url


# -----------------------------------------------------------------------------
# 2. Test actual TASK_DISPATCH serialized envelope parsing
# -----------------------------------------------------------------------------
def test_task_dispatch_envelope_parsing_distinguishes_attempts() -> None:
    """Verifies exact extraction paths for outbox delivery attempts versus task execution attempt."""
    serialized_sqs_body = {
        "event_id": "33333333-3333-4333-8333-333333333333",
        "execution_id": "11111111-1111-4111-8111-111111111111",
        "task_id": "22222222-2222-4222-8222-222222222222",
        "kind": "TASK_DISPATCH",
        "payload": {
            "schema_version": "1.0",
            "execution_id": "11111111-1111-4111-8111-111111111111",
            "task_id": "22222222-2222-4222-8222-222222222222",
            "worker": "EVIDENCE",
            "action": "collect",
            "attempt": 2,  # Task attempt counter from tasks.attempt
            "context_version": 4,
            "payload": {
                "queries": ["temple timings"],
            },
        },
        "attempts": 5,  # Outbox delivery transport retries from outbox_events.attempts
        "created_at": "2026-10-10T12:00:00Z",
    }

    envelope = parse_task_dispatch_envelope(serialized_sqs_body)

    # Outbox relay delivery attempts must strictly reflect top level attempts
    assert envelope.outbox_delivery_attempts == 5
    # Task execution attempt must strictly reflect body["payload"]["attempt"]
    assert envelope.task_attempt == 2
    # Context version must reflect body["payload"]["context_version"]
    assert envelope.context_version == 4
    assert envelope.worker == "EVIDENCE"
    assert envelope.action == "collect"
    assert envelope.execution_id == uuid.UUID("11111111-1111-4111-8111-111111111111")
    assert envelope.task_id == uuid.UUID("22222222-2222-4222-8222-222222222222")


# -----------------------------------------------------------------------------
# 3. Test atomic completion boundary
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fenced_atomic_completion_boundary(session_factory) -> None:
    """Verifies that source_observations, worker_results, task completion, and outbox_events commit atomically."""
    execution_id, task_id = await create_test_evidence_task(session_factory)

    envelope = TaskDispatchEnvelope(
        event_id=uuid.uuid4(),
        execution_id=execution_id,
        task_id=task_id,
        kind="TASK_DISPATCH",
        outbox_delivery_attempts=0,
        task_attempt=0,
        context_version=1,
        worker="EVIDENCE",
        action="collect",
        inner_payload={},
    )

    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        result_id, obs_ids = await service.execute_collection_task(
            envelope,
            adapter=FixtureSearchAdapter(),
        )
        assert result_id is not None
        assert len(obs_ids) >= 1

    # Verify atomic state in database
    async with session_factory() as session:
        # 1. Observations persisted
        obs_res = await session.execute(
            select(SourceObservationModel).where(SourceObservationModel.execution_id == execution_id)
        )
        observations = obs_res.scalars().all()
        assert len(observations) == len(obs_ids)
        for obs in observations:
            assert obs.id in obs_ids

        # 2. Worker result persisted with output observations linking persisted IDs
        wr_res = await session.execute(
            select(WorkerResultModel).where(WorkerResultModel.id == result_id)
        )
        worker_result = wr_res.scalar_one()
        assert worker_result.status == "SUCCESS"
        output_obs = worker_result.output_ref["observations"]
        assert len(output_obs) == len(obs_ids)
        persisted_str_ids = {str(o.id) for o in observations}
        for item in output_obs:
            assert item["observation_id"] in persisted_str_ids

        # 3. Task transitioned to COMPLETED
        t_res = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        task = t_res.scalar_one()
        assert task.status == "COMPLETED"
        assert task.result_id == result_id
        assert task.lease_token is None

        # 4. Outbox event TASK_COMPLETED emitted
        ob_res = await session.execute(
            select(OutboxEventModel).where(
                OutboxEventModel.task_id == task_id,
                OutboxEventModel.kind == "TASK_COMPLETED",
            )
        )
        outbox = ob_res.scalar_one()
        assert outbox.payload_ref["result_id"] == str(result_id)


# -----------------------------------------------------------------------------
# 4. Test observation rollback when complete_task rejects fencing
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_observation_rollback_on_stale_or_expired_lease(session_factory) -> None:
    """Verifies that uncommitted source_observations are rolled back when lease token is stale or expired."""
    execution_id, task_id = await create_test_evidence_task(session_factory)

    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        claim_msg = TaskQueueMessage(task_id=task_id, execution_id=execution_id, attempt=0)
        claim_result = await service.fencing_service.claim_task(
            message=claim_msg,
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
            lease_seconds=300,
        )

        # Forge stale lease token
        stale_claim = claim_result.model_copy(update={"lease_token": uuid.uuid4()})

        test_obs = [
            RawObservation.create(
                source_url="https://example.com/uncommitted-test",
                publisher="example.com",
                excerpt_text="This observation must never be committed to PostgreSQL.",
            )
        ]

        with pytest.raises(LeaseFencingError, match="stale or expired lease token"):
            await service.stage_observations_and_complete(
                claim_result=stale_claim,
                observations=test_obs,
            )

    # Verify that PostgreSQL has ZERO observations recorded
    async with session_factory() as session:
        obs_res = await session.execute(
            select(SourceObservationModel).where(
                SourceObservationModel.execution_id == execution_id,
                SourceObservationModel.source_url == "https://example.com/uncommitted-test",
            )
        )
        assert obs_res.scalar_one_or_none() is None

        # Verify task is still in CLAIMED status, not COMPLETED
        task_res = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        task = task_res.scalar_one()
        assert task.status == "CLAIMED"


@pytest.mark.asyncio
async def test_observation_rollback_on_cancelled_parent(session_factory) -> None:
    """Verifies observation rollback when parent execution is cancelled before completion."""
    execution_id, task_id = await create_test_evidence_task(session_factory)

    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        claim_msg = TaskQueueMessage(task_id=task_id, execution_id=execution_id, attempt=0)
        claim_result = await service.fencing_service.claim_task(
            message=claim_msg,
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
            lease_seconds=300,
        )

    # Cancel execution behind the worker's back in a separate transaction
    async with session_factory() as session:
        exec_row = (await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))).scalar_one()
        exec_row.status = "CANCELLED"
        exec_row.cancelled_at = datetime.now(UTC)
        await session.commit()

    # Worker attempts to complete with staged observations
    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        test_obs = [
            RawObservation.create(
                source_url="https://example.com/cancelled-test",
                publisher="example.com",
                excerpt_text="Cancelled execution observation must roll back.",
            )
        ]

        with pytest.raises(ParentCancelledError, match="cancelled"):
            await service.stage_observations_and_complete(
                claim_result=claim_result,
                observations=test_obs,
            )

    # Verify observations rolled back
    async with session_factory() as session:
        obs_res = await session.execute(
            select(SourceObservationModel).where(
                SourceObservationModel.execution_id == execution_id,
                SourceObservationModel.source_url == "https://example.com/cancelled-test",
            )
        )
        assert obs_res.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_observation_rollback_on_passed_deadline(session_factory) -> None:
    """Verifies observation rollback when task deadline passes before completion."""
    execution_id, task_id = await create_test_evidence_task(
        session_factory,
        task_deadline_minutes=1,
    )

    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        claim_msg = TaskQueueMessage(task_id=task_id, execution_id=execution_id, attempt=0)
        claim_result = await service.fencing_service.claim_task(
            message=claim_msg,
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
            lease_seconds=300,
        )

    # Expire task deadline in database
    async with session_factory() as session:
        task_row = (await session.execute(select(TaskModel).where(TaskModel.id == task_id))).scalar_one()
        task_row.deadline_at = datetime.now(UTC) - timedelta(seconds=10)
        await session.commit()

    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        test_obs = [
            RawObservation.create(
                source_url="https://example.com/deadline-test",
                publisher="example.com",
                excerpt_text="Deadline passed observation must roll back.",
            )
        ]

        with pytest.raises(DeadlineExceededError, match="deadline passed"):
            await service.stage_observations_and_complete(
                claim_result=claim_result,
                observations=test_obs,
            )

    # Verify observations rolled back
    async with session_factory() as session:
        obs_res = await session.execute(
            select(SourceObservationModel).where(
                SourceObservationModel.execution_id == execution_id,
                SourceObservationModel.source_url == "https://example.com/deadline-test",
            )
        )
        assert obs_res.scalar_one_or_none() is None


# -----------------------------------------------------------------------------
# 5. Test unchanged replay snapshot reuse returns existing persisted row IDs
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unchanged_replay_snapshot_reuse_returns_existing_id(session_factory) -> None:
    """Verifies that replaying an identical observation reuses existing snapshot and returns existing ID."""
    execution_id, task_id_1 = await create_test_evidence_task(session_factory)
    _, task_id_2 = await create_test_evidence_task(session_factory, execution_id=execution_id)

    test_obs = RawObservation.create(
        source_url="https://example.com/replayed-notice",
        publisher="example.com",
        excerpt_text="Identical text collected across two attempts.",
    )

    # First attempt stages and completes
    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        claim_1 = await service.fencing_service.claim_task(
            message=TaskQueueMessage(task_id=task_id_1, execution_id=execution_id, attempt=0),
            worker="EVIDENCE",
            action="collect",
        )
        res_id_1, obs_ids_1 = await service.stage_observations_and_complete(
            claim_result=claim_1,
            observations=[test_obs],
        )
        assert len(obs_ids_1) == 1
        original_obs_id = obs_ids_1[0]

    # Second attempt (replay) collects identical content for same URL and execution
    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        claim_2 = await service.fencing_service.claim_task(
            message=TaskQueueMessage(task_id=task_id_2, execution_id=execution_id, attempt=0),
            worker="EVIDENCE",
            action="collect",
        )
        res_id_2, obs_ids_2 = await service.stage_observations_and_complete(
            claim_result=claim_2,
            observations=[test_obs],
        )
        assert len(obs_ids_2) == 1
        replayed_obs_id = obs_ids_2[0]

    # Verify ID reuse and exactly 1 row in PostgreSQL (no duplicate snapshot inserted)
    assert original_obs_id == replayed_obs_id

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(SourceObservationModel).where(
                    SourceObservationModel.execution_id == execution_id,
                    SourceObservationModel.canonical_url == test_obs.canonical_url,
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == original_obs_id


# -----------------------------------------------------------------------------
# 6. Test changed content preserves both snapshots under same URL
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_changed_content_preserves_both_snapshots(session_factory) -> None:
    """Verifies that changed content under the same canonical URL creates a new snapshot without clobbering the old."""
    execution_id, task_id_1 = await create_test_evidence_task(session_factory)
    _, task_id_2 = await create_test_evidence_task(session_factory, execution_id=execution_id)

    url = "https://example.com/dynamic-schedule"
    obs_version_1 = RawObservation.create(
        source_url=url,
        publisher="example.com",
        excerpt_text="Schedule version 1: road open from 08:00.",
    )
    obs_version_2 = RawObservation.create(
        source_url=url,
        publisher="example.com",
        excerpt_text="Schedule version 2: road closed unexpectedly for maintenance.",
    )

    # First version
    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        claim_1 = await service.fencing_service.claim_task(
            message=TaskQueueMessage(task_id=task_id_1, execution_id=execution_id, attempt=0),
            worker="EVIDENCE",
            action="collect",
        )
        _, obs_ids_1 = await service.stage_observations_and_complete(
            claim_result=claim_1,
            observations=[obs_version_1],
        )
        assert len(obs_ids_1) == 1

    # Second version (changed content)
    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        claim_2 = await service.fencing_service.claim_task(
            message=TaskQueueMessage(task_id=task_id_2, execution_id=execution_id, attempt=0),
            worker="EVIDENCE",
            action="collect",
        )
        _, obs_ids_2 = await service.stage_observations_and_complete(
            claim_result=claim_2,
            observations=[obs_version_2],
        )
        assert len(obs_ids_2) == 1

    # Both snapshots exist in database under the same canonical URL
    assert obs_ids_1[0] != obs_ids_2[0]

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(SourceObservationModel).where(
                    SourceObservationModel.execution_id == execution_id,
                    SourceObservationModel.canonical_url == obs_version_1.canonical_url,
                )
            )
        ).scalars().all()
        assert len(rows) == 2
        hashes = {r.content_hash for r in rows}
        assert obs_version_1.content_hash in hashes
        assert obs_version_2.content_hash in hashes


# -----------------------------------------------------------------------------
# 7. Test distinct URL attribution with identical content
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_distinct_url_attribution_with_identical_content(session_factory) -> None:
    """Verifies that two different URLs hosting identical content insert separate attributable rows."""
    execution_id, task_id = await create_test_evidence_task(session_factory)

    identical_text = "Standard statewide public holiday declaration for Durga Puja 2026."
    shared_hash = compute_content_hash(identical_text)

    obs_source_a = RawObservation.create(
        source_url="https://portal-a.gov.in/notice",
        publisher="portal-a.gov.in",
        excerpt_text=identical_text,
        content_hash=shared_hash,
    )
    obs_source_b = RawObservation.create(
        source_url="https://news-syndicate-b.com/holiday-bulletin",
        publisher="news-syndicate-b.com",
        excerpt_text=identical_text,
        content_hash=shared_hash,
    )

    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        claim = await service.fencing_service.claim_task(
            message=TaskQueueMessage(task_id=task_id, execution_id=execution_id, attempt=0),
            worker="EVIDENCE",
            action="collect",
        )
        _, obs_ids = await service.stage_observations_and_complete(
            claim_result=claim,
            observations=[obs_source_a, obs_source_b],
        )
        assert len(obs_ids) == 2
        assert obs_ids[0] != obs_ids[1]

    # Verify both URLs are distinct rows in PostgreSQL with the shared hash
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(SourceObservationModel).where(
                    SourceObservationModel.execution_id == execution_id,
                    SourceObservationModel.content_hash == shared_hash,
                )
            )
        ).scalars().all()
        assert len(rows) == 2
        urls = {r.canonical_url for r in rows}
        assert obs_source_a.canonical_url in urls
        assert obs_source_b.canonical_url in urls


# -----------------------------------------------------------------------------
# 8. Test local budget iteration bounds
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_local_budget_iteration_limit(session_factory) -> None:
    """Verifies that a task with remaining_tool_calls = 1 stops after 1 query and reports budget_exhausted."""
    execution_id, task_id = await create_test_evidence_task(
        session_factory,
        payload={
            "queries": ["query 1", "query 2", "query 3"],
            "budget": {
                "remaining_tool_calls": 1,
                "remaining_cost_minor": 100,
                "currency": "INR",
            },
        },
    )

    envelope = TaskDispatchEnvelope(
        event_id=uuid.uuid4(),
        execution_id=execution_id,
        task_id=task_id,
        kind="TASK_DISPATCH",
        outbox_delivery_attempts=0,
        task_attempt=0,
        context_version=1,
        worker="EVIDENCE",
        action="collect",
        inner_payload={},
    )

    adapter = FixtureSearchAdapter()
    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        result_id, _ = await service.execute_collection_task(envelope, adapter=adapter)

    assert adapter.call_count == 1

    async with session_factory() as session:
        wr = (await session.execute(select(WorkerResultModel).where(WorkerResultModel.id == result_id))).scalar_one()
        assert wr.output_ref["budget_exhausted"] is True
        assert wr.usage["tool_calls_made"] == 1


# -----------------------------------------------------------------------------
# 9. Test dedicated collector SQS Lambda consumer execution
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_consumer_sqs_batch_processing(session_factory) -> None:
    """Verifies that process_batch parses SQS records, claims tasks, and returns zero failures."""
    execution_id, task_id = await create_test_evidence_task(session_factory)

    sqs_event = {
        "Records": [
            {
                "messageId": "msg-12345",
                "body": json.dumps(
                    {
                        "event_id": str(uuid.uuid4()),
                        "execution_id": str(execution_id),
                        "task_id": str(task_id),
                        "kind": "TASK_DISPATCH",
                        "attempts": 1,  # outbox delivery attempt
                        "payload": {
                            "schema_version": "1.0",
                            "execution_id": str(execution_id),
                            "task_id": str(task_id),
                            "worker": "EVIDENCE",
                            "action": "collect",
                            "attempt": 0,  # task attempt
                            "context_version": 1,
                            "payload": {
                                "queries": ["Dakshineswar temple"],
                            },
                        },
                    }
                ),
            }
        ]
    }

    failures = await process_batch(sqs_event, context=None, session_factory=session_factory)
    assert failures == []

    # Verify task completed
    async with session_factory() as session:
        task = (await session.execute(select(TaskModel).where(TaskModel.id == task_id))).scalar_one()
        assert task.status == "COMPLETED"
        assert task.result_id is not None


# -----------------------------------------------------------------------------
# 10. Regression: normalize_url must not strip params starting with 'source' or 'ref'
# -----------------------------------------------------------------------------
def test_normalize_url_preserves_non_tracking_params() -> None:
    """Regression for the old prefix-match bug that stripped source_id, reference, etc.

    Two pages whose only distinction is a 'source_id' or 'ref_id' query param must
    produce different canonical URLs so they don't silently collide under the
    snapshot uniqueness constraint.
    """
    # These must survive normalization (not tracking params)
    url_with_source_id = "https://example.gov.in/notice?source_id=42&category=health"
    url_with_ref_id = "https://example.gov.in/doc?ref_id=99&lang=en"
    url_with_reference = "https://example.com/page?reference=ABC-123"

    norm_source_id = normalize_url(url_with_source_id)
    norm_ref_id = normalize_url(url_with_ref_id)
    norm_reference = normalize_url(url_with_reference)

    assert "source_id=42" in norm_source_id, "source_id must not be stripped"
    assert "category=health" in norm_source_id
    assert "ref_id=99" in norm_ref_id, "ref_id must not be stripped"
    assert "reference=ABC-123" in norm_reference, "reference must not be stripped"

    # Known tracking params must still be stripped
    dirty = "https://example.com/page?source_id=42&utm_source=twitter&fbclid=abc"
    norm_dirty = normalize_url(dirty)
    assert "source_id=42" in norm_dirty
    assert "utm_source" not in norm_dirty
    assert "fbclid" not in norm_dirty


# -----------------------------------------------------------------------------
# 11. Regression: ExecutionEngineError must produce a batchItemFailure
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_consumer_execution_engine_error_produces_batch_failure(session_factory) -> None:
    """Regression: ExecutionEngineError must be returned as a batchItemFailure, not silently acked.

    Previously the consumer caught ExecutionEngineError and returned, which acknowledged
    the SQS message and prevented it from reaching the DLQ.
    """
    # Dispatch to a task_id that does not exist in the database
    nonexistent_execution_id = uuid.uuid4()
    nonexistent_task_id = uuid.uuid4()

    sqs_event = {
        "Records": [
            {
                "messageId": "msg-engine-error",
                "body": json.dumps(
                    {
                        "event_id": str(uuid.uuid4()),
                        "execution_id": str(nonexistent_execution_id),
                        "task_id": str(nonexistent_task_id),
                        "kind": "TASK_DISPATCH",
                        "attempts": 1,
                        "payload": {
                            "schema_version": "1.0",
                            "execution_id": str(nonexistent_execution_id),
                            "task_id": str(nonexistent_task_id),
                            "worker": "EVIDENCE",
                            "action": "collect",
                            "attempt": 0,
                            "context_version": 1,
                            "payload": {"queries": ["temple timings"]},
                        },
                    }
                ),
            }
        ]
    }

    failures = await process_batch(sqs_event, context=None, session_factory=session_factory)

    # The message must appear in batchItemFailures so SQS can redrive it to the DLQ
    assert len(failures) == 1
    assert failures[0]["itemIdentifier"] == "msg-engine-error"


# -----------------------------------------------------------------------------
# 12. Regression: savepoint failure inside complete_task still rolls back observations
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_observation_rollback_on_savepoint_failure_inside_complete_task(
    session_factory,
) -> None:
    """Regression: a failure raised inside complete_task's begin_nested() savepoint must
    still roll back staged observations via the except-Exception block in
    stage_observations_and_complete.

    Previously the docstring implied only pre-savepoint lease checks triggered the rollback.
    This test locks in the broader guarantee.
    """
    execution_id, task_id = await create_test_evidence_task(session_factory)

    test_obs = [
        RawObservation.create(
            source_url="https://example.com/savepoint-failure-test",
            publisher="example.com",
            excerpt_text="This observation must roll back when complete_task savepoint fails.",
        )
    ]

    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        claim_msg = TaskQueueMessage(task_id=task_id, execution_id=execution_id, attempt=0)
        claim_result = await service.fencing_service.claim_task(
            message=claim_msg,
            worker="EVIDENCE",
            action="collect",
            expected_context_version=1,
            lease_seconds=300,
        )

        # Patch record_job_event to raise inside complete_task's savepoint.
        # complete_task calls record_job_event inside begin_nested(); raising there
        # rolls back the savepoint and propagates the exception to stage_observations_and_complete,
        # which must then roll back the staged observations.
        target = "functions.orchestrator.execution.fencing.record_job_event"
        with unittest.mock.patch(target, side_effect=RuntimeError("simulated savepoint failure")):
            with pytest.raises(RuntimeError, match="simulated savepoint failure"):
                await service.stage_observations_and_complete(
                    claim_result=claim_result,
                    observations=test_obs,
                )

    # Verify: no observation row survived the savepoint failure
    async with session_factory() as session:
        obs_res = await session.execute(
            select(SourceObservationModel).where(
                SourceObservationModel.execution_id == execution_id,
                SourceObservationModel.source_url == "https://example.com/savepoint-failure-test",
            )
        )
        assert obs_res.scalar_one_or_none() is None, (
            "Staged observation must be rolled back when complete_task's savepoint fails"
        )


# -----------------------------------------------------------------------------
# 13. Regression: budget test verifies that the one permitted observation was persisted
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_local_budget_iteration_limit_persists_observations(session_factory) -> None:
    """Strengthens test_local_budget_iteration_limit: verifies that the one call that
    ran within budget actually produced persisted observations, not just a usage report.
    """
    execution_id, task_id = await create_test_evidence_task(
        session_factory,
        payload={
            "queries": ["query 1", "query 2", "query 3"],
            "budget": {
                "remaining_tool_calls": 1,
                "remaining_cost_minor": 100,
                "currency": "INR",
            },
        },
    )

    envelope = TaskDispatchEnvelope(
        event_id=uuid.uuid4(),
        execution_id=execution_id,
        task_id=task_id,
        kind="TASK_DISPATCH",
        outbox_delivery_attempts=0,
        task_attempt=0,
        context_version=1,
        worker="EVIDENCE",
        action="collect",
        inner_payload={},
    )

    adapter = FixtureSearchAdapter()
    async with session_factory() as session:
        service = EvidenceCollectorService(session)
        result_id, obs_ids = await service.execute_collection_task(envelope, adapter=adapter)

    # The adapter ran exactly once
    assert adapter.call_count == 1

    # Observations from that one permitted call must be persisted
    assert len(obs_ids) >= 1, "At least one observation must be persisted within budget"

    async with session_factory() as session:
        obs_rows = (
            await session.execute(
                select(SourceObservationModel).where(
                    SourceObservationModel.execution_id == execution_id
                )
            )
        ).scalars().all()
        assert len(obs_rows) == len(obs_ids), (
            "Persisted row count must match the IDs returned by execute_collection_task"
        )

        wr = (
            await session.execute(
                select(WorkerResultModel).where(WorkerResultModel.id == result_id)
            )
        ).scalar_one()
        assert wr.output_ref["budget_exhausted"] is True
        assert wr.usage["tool_calls_made"] == 1
