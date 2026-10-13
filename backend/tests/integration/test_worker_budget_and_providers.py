"""Integration tests for durable budget reservations, syndication detection, and live provider support.

Conforms to spec 0002 Slice 3:
- AC 4: Identical content hash across distinct URLs flagged as non independent corroboration.
- AC 13: Browser escalation requests emitted and recorded in worker output.
- AC 14: Durable pre call budget reservations across concurrent tasks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from functions.api.models import TripModel
from functions.evidence_collector.fixtures import (
    FixtureSearchAdapter,
    RawObservation,
    compute_content_hash,
)
from functions.evidence_collector.models import SourceObservationModel
from functions.evidence_collector.service import (
    EvidenceCollectorService,
    TaskDispatchEnvelope,
)
from functions.orchestrator.execution.fencing import (
    ExecutionModel,
    ParentCancelledError,
    TaskModel,
    WorkerResultModel,
)


async def _create_test_execution_and_task(
    session_factory: Any,
    *,
    execution_id: uuid.UUID | None = None,
    budget: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    cancelled_at: datetime | None = None,
    deadline_minutes: int = 60,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper creating execution and task records for integration testing."""
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    task_id = uuid.uuid4()
    now = datetime.now(UTC)

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
                status="CANCELLED" if cancelled_at else "CREATED",
                context_version=1,
                deadline_at=now + timedelta(minutes=deadline_minutes),
                budget=budget,
                cancelled_at=cancelled_at,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)

        task = TaskModel(
            id=task_id,
            execution_id=execution_id,
            parent_task_id=None,
            worker="EVIDENCE",
            action="collect",
            payload_ref=payload or {},
            input_hash="hash_input",
            idempotency_key=f"idem-{task_id}",
            attempt=0,
            max_attempts=3,
            context_version=1,
            status="PENDING",
            deadline_at=now + timedelta(minutes=deadline_minutes),
            result_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.commit()

    return execution_id, task_id


class TestWorkerBudgetAndProviders:
    """Integration test suite for budget reservations and syndication."""

    @pytest.mark.asyncio
    async def test_durable_budget_reservations_enforce_limit(self, session_factory: Any) -> None:
        """Verifies AC 14: tasks enforce durable pre call budget limits on executions.budget."""
        initial_budget = {
            "remaining_tool_calls": 2,
            "remaining_cost_minor": 100,
            "currency": "INR",
            "cost_per_call_minor": 50,
        }
        task_payload = {
            "queries": ["query 1", "query 2", "query 3"],
            "budget": initial_budget,
        }

        execution_id, task_id = await _create_test_execution_and_task(
            session_factory,
            budget=initial_budget,
            payload=task_payload,
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
            inner_payload=task_payload,
        )

        search_adapter = FixtureSearchAdapter()

        async with session_factory() as session:
            service = EvidenceCollectorService(session)
            result_id, obs_ids = await service.execute_collection_task(
                envelope,
                adapter=search_adapter,
            )

        # 1. Adapter must be called exactly 2 times (limit reached before 3rd query)
        assert search_adapter.call_count == 2

        # 2. Worker result verifies budget_exhausted flag
        async with session_factory() as session:
            stmt = select(WorkerResultModel).where(WorkerResultModel.id == result_id)
            res = await session.execute(stmt)
            worker_result = res.scalar_one()

            assert worker_result.output_ref is not None
            assert worker_result.output_ref["budget_exhausted"] is True
            assert worker_result.output_ref["usage"]["tool_calls_made"] == 2
            assert worker_result.output_ref["usage"]["cost_minor"] == 100

            # 3. Check durable executions.budget
            exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id)
            exec_res = await session.execute(exec_stmt)
            execution = exec_res.scalar_one()
            assert execution.budget is not None
            assert execution.budget["spent_tool_calls"] == 2
            assert execution.budget["spent_cost_minor"] == 100
            assert execution.budget["reserved_tool_calls"] == 0

    @pytest.mark.asyncio
    async def test_syndication_and_non_independent_corroboration_flagged(self, session_factory: Any) -> None:
        """Verifies AC 4: duplicated content across distinct URLs is flagged as non independent corroboration."""
        now = datetime.now(UTC)
        unique_tag = uuid.uuid4().hex[:8]
        shared_excerpt = f"Kolkata Police Advisory {unique_tag}: Central Avenue closed for heavy commercial vehicles."
        shared_hash = compute_content_hash(shared_excerpt)

        execution_id_1, task_id_1 = await _create_test_execution_and_task(session_factory)

        # Seed original observation under first URL
        async with session_factory() as session:
            orig_obs = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=execution_id_1,
                source_url="https://kolkatapolice.gov.in/advisory",
                canonical_url="https://kolkatapolice.gov.in/advisory",
                publisher="kolkatapolice.gov.in",
                content_hash=shared_hash,
                retrieved_at=now,
                excerpt_text=shared_excerpt,
                excerpt_locator="notice:1",
                extraction_method="MANUAL_FIXTURE",
            )
            session.add(orig_obs)
            await session.commit()
            orig_obs_id = orig_obs.id

        payload = {"queries": ["kolkata police advisory"]}
        execution_id_2, task_id_2 = await _create_test_execution_and_task(
            session_factory,
            payload=payload,
        )

        syndicated_obs = RawObservation.create(
            source_url="https://timesofindia.indiatimes.com/kolkata-police-advisory-copy",
            publisher="timesofindia.indiatimes.com",
            excerpt_text=shared_excerpt,
            extraction_method="MANUAL_FIXTURE",
        )

        envelope = TaskDispatchEnvelope(
            event_id=uuid.uuid4(),
            execution_id=execution_id_2,
            task_id=task_id_2,
            kind="TASK_DISPATCH",
            outbox_delivery_attempts=0,
            task_attempt=0,
            context_version=1,
            worker="EVIDENCE",
            action="collect",
            inner_payload=payload,
        )

        custom_adapter = FixtureSearchAdapter(fixtures=[syndicated_obs])

        async with session_factory() as session:
            service = EvidenceCollectorService(session)
            result_id, obs_ids = await service.execute_collection_task(
                envelope,
                adapter=custom_adapter,
            )

        # Verify output records non independent corroboration
        async with session_factory() as session:
            stmt = select(WorkerResultModel).where(WorkerResultModel.id == result_id)
            res = await session.execute(stmt)
            worker_result = res.scalar_one()

            observations = worker_result.output_ref["observations"]
            assert len(observations) == 1
            second_obs = observations[0]

            assert second_obs["canonical_url"] == "https://timesofindia.indiatimes.com/kolkata-police-advisory-copy"
            assert second_obs["content_hash"] == shared_hash
            assert second_obs["non_independent_corroboration"] is True
            assert second_obs["corroborates_observation_id"] == str(orig_obs_id)
            assert second_obs["syndicated_with_url"] == "https://kolkatapolice.gov.in/advisory"

            # Check database row artifact_ref
            obs_stmt = select(SourceObservationModel).where(SourceObservationModel.id == obs_ids[0])
            obs_res = await session.execute(obs_stmt)
            db_obs = obs_res.scalar_one()
            assert db_obs.artifact_ref is not None
            assert db_obs.artifact_ref["non_independent_corroboration"] is True
            assert db_obs.artifact_ref["corroborates_observation_id"] == str(orig_obs_id)

    @pytest.mark.asyncio
    async def test_parent_cancelled_halts_collection_gracefully(self, session_factory: Any) -> None:
        """Verifies AC 14: if parent execution is cancelled during collection, calls halt."""
        now = datetime.now(UTC)

        execution_id, task_id = await _create_test_execution_and_task(
            session_factory,
            cancelled_at=now,
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
            inner_payload={"queries": ["query 1", "query 2"]},
        )

        search_adapter = FixtureSearchAdapter()

        async with session_factory() as session:
            service = EvidenceCollectorService(session)
            with pytest.raises(ParentCancelledError):
                await service.execute_collection_task(
                    envelope,
                    adapter=search_adapter,
                )

        assert search_adapter.call_count == 0

    @pytest.mark.asyncio
    async def test_reconcile_budget_does_not_suppress_original_adapter_exception(
        self, session_factory: Any
    ) -> None:
        """Verifies Major 2: adapter exception propagates cleanly without being suppressed by reconcile_budget."""
        execution_id, task_id = await _create_test_execution_and_task(
            session_factory,
            budget={"remaining_tool_calls": 5, "remaining_cost_minor": 500},
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
            inner_payload={"queries": ["faulty query"]},
        )

        class FaultyAdapter:
            is_live_provider = False

            async def search(self, query: str, limit: int = 5):
                raise RuntimeError("Original search adapter failure")

        async with session_factory() as session:
            service = EvidenceCollectorService(session)
            with pytest.raises(RuntimeError) as exc_info:
                await service.execute_collection_task(
                    envelope,
                    adapter=FaultyAdapter(),
                )
            assert "Original search adapter failure" in str(exc_info.value)
