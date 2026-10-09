"""Idempotency repository and atomic job acceptance for API layer.

Conforms strictly to spec 0001 Transaction 1: Job acceptance and idempotency reservation.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from functions.orchestrator.execution.job_events import record_job_event
from functions.orchestrator.execution.models import ExecutionModel, OutboxEventModel


class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused with a conflicting payload (HTTP 409)."""

    pass


def compute_request_hash(payload: Any) -> str:
    """Computes deterministic SHA256 hex digest of canonical JSON payload."""
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class IdempotencyRepository:
    """Manages API idempotency records and atomic job acceptance."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def accept_job(
        self,
        *,
        owner_id: uuid.UUID,
        operation: str,
        idempotency_key: str,
        payload: dict[str, Any],
        trip_id: uuid.UUID | None = None,
        workflow: str = "TRAVEL",
        deadline_seconds: int = 3600,
        expiry_seconds: int = 86400,
        test_fail_after_idempotency_insert: bool = False,
        test_fail_after_execution_insert: bool = False,
        test_fail_after_outbox_insert: bool = False,
    ) -> tuple[dict[str, Any], int, bool]:
        """Atomically accepts a job with owner-scoped idempotency reservation.

        Pre-generates execution ID and stored response envelope.
        Commits idempotency record, execution and EXECUTION_START outbox event
        in ONE transaction under PostgreSQL READ COMMITTED.

        Returns:
            tuple of (response_json, status_code, is_cached)
            where status_code is 202 (or cached code).

        Raises:
            IdempotencyConflictError: If key exists with different payload hash (HTTP 409).
        """
        now = datetime.now(UTC)
        request_hash = compute_request_hash(payload)
        expires_at = now + timedelta(seconds=expiry_seconds)
        deadline_at = now + timedelta(seconds=deadline_seconds)

        # 1. Pre-generate the execution ID and stored response
        execution_id = uuid.uuid4()
        response_json = {
            "job_id": str(execution_id),
            "status": "QUEUED",
            "status_url": f"/api/v1/jobs/{execution_id}",
        }
        status_code = 202

        # 2. Conflict-safe insert on api_idempotency with ON CONFLICT DO NOTHING RETURNING owner_id
        insert_sql = text(
            """
            INSERT INTO api_idempotency (
                owner_id, operation, key, request_hash, response_json, status_code, expires_at
            ) VALUES (
                :owner_id, :operation, :key, :request_hash, :response_json, :status_code, :expires_at
            )
            ON CONFLICT (owner_id, operation, key) DO NOTHING
            RETURNING owner_id;
            """
        )

        res = await self._session.execute(
            insert_sql,
            {
                "owner_id": owner_id,
                "operation": operation,
                "key": idempotency_key,
                "request_hash": request_hash,
                "response_json": json.dumps(response_json),
                "status_code": status_code,
                "expires_at": expires_at,
            },
        )
        inserted_row = res.scalar_one_or_none()

        if inserted_row is not None:
            try:
                # Fresh request: row was reserved.
                if test_fail_after_idempotency_insert:
                    raise RuntimeError("Simulated failure after api_idempotency insert")

                # Insert execution record
                execution = ExecutionModel(
                    id=execution_id,
                    owner_id=owner_id,
                    trip_id=trip_id,
                    workflow=workflow,
                    status="CREATED",
                    context_version=1,
                    deadline_at=deadline_at,
                    created_at=now,
                    updated_at=now,
                )
                self._session.add(execution)
                await self._session.flush()

                if test_fail_after_execution_insert:
                    raise RuntimeError("Simulated failure after executions insert")

                # Insert EXECUTION_START outbox event
                outbox_event = OutboxEventModel(
                    id=uuid.uuid4(),
                    execution_id=execution_id,
                    aggregate_id=execution_id,
                    task_id=None,
                    kind="EXECUTION_START",
                    event_type="EXECUTION_START",
                    payload_ref=response_json,
                    payload=response_json,
                    due_at=now,
                    attempts=0,
                    created_at=now,
                )
                self._session.add(outbox_event)
                await self._session.flush()

                # Insert initial JOB_ACCEPTED timeline event in job_events
                await record_job_event(
                    self._session,
                    execution_id=execution_id,
                    event_type="JOB_ACCEPTED",
                    safe_payload={
                        "status": "CREATED",
                        "workflow": workflow,
                        "trip_id": str(trip_id) if trip_id else None,
                    },
                )

                if test_fail_after_outbox_insert:
                    raise RuntimeError("Simulated failure after outbox_events insert")

                # Commit the idempotency record, execution, outbox event and job event in ONE transaction
                await self._session.commit()
                return response_json, status_code, False
            except Exception:
                await self._session.rollback()
                raise

        # 3. Duplicate or concurrent request: conflict occurred.
        # Query existing record
        select_sql = text(
            """
            SELECT request_hash, response_json, status_code
            FROM api_idempotency
            WHERE owner_id = :owner_id AND operation = :operation AND key = :key;
            """
        )
        existing_result = await self._session.execute(
            select_sql,
            {
                "owner_id": owner_id,
                "operation": operation,
                "key": idempotency_key,
            },
        )
        existing = existing_result.mappings().first()

        if existing is None:
            await self._session.rollback()
            raise RuntimeError(
                f"Idempotency conflict detected for key '{idempotency_key}' but record is uncommitted"
            )

        stored_hash = existing["request_hash"]
        stored_response = existing["response_json"]
        if isinstance(stored_response, str):
            stored_response = json.loads(stored_response)
        stored_status_code = existing["status_code"]

        if stored_hash == request_hash:
            # Identical payload: commit transaction and return cached response
            await self._session.commit()
            return stored_response, stored_status_code, True

        # Conflicting payload: rollback transaction and raise HTTP 409 conflict error
        await self._session.rollback()
        raise IdempotencyConflictError(
            f"IDEMPOTENCY_CONFLICT: Idempotency key '{idempotency_key}' was previously used with a different request payload."
        )
