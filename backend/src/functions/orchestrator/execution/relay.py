"""Transactional outbox relay service conforming to spec 0001.

Implements Transaction 3 (durable outbox relay batch claim and dispatch):
1. Claims due outbox events using durable relay tokens and lease expiries via SKIP LOCKED.
2. Commits claims before invoking transport.
3. Publishes events through a transport adapter preserving event IDs for deduplication.
4. Fences success and failure mutations against relay ownership.
5. Applies capped exponential retry backoff and an explicit exhausted delivery policy.
6. Bounds batch size, provider timeout, and invocation duration.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from functions.orchestrator.execution.transport import OutboxMessage, TransportAdapter

logger = logging.getLogger(__name__)


@dataclass
class OutboxBatchResult:
    """Summary of processing a single claimed outbox batch."""

    claimed_count: int = 0
    delivered_count: int = 0
    retry_scheduled_count: int = 0
    exhausted_count: int = 0
    fenced_out_count: int = 0


@dataclass
class OutboxRelayRunSummary:
    """Summary of an outbox relay invocation loop."""

    batches_processed: int = 0
    total_claimed: int = 0
    total_delivered: int = 0
    total_retry_scheduled: int = 0
    total_exhausted: int = 0
    total_fenced_out: int = 0
    elapsed_seconds: float = 0.0
    terminated_by_timeout: bool = False


class OutboxRelayService:
    """Bounded transactional outbox relay service."""

    DEFAULT_MAX_BATCH_SIZE: int = 100
    DEFAULT_BATCH_SIZE: int = 50
    DEFAULT_LEASE_SECONDS: int = 30
    DEFAULT_PROVIDER_TIMEOUT: float = 5.0
    DEFAULT_MAX_RUNTIME_SECONDS: float = 25.0
    DEFAULT_MAX_ATTEMPTS: int = 5
    DEFAULT_BASE_BACKOFF_SECONDS: int = 2
    DEFAULT_MAX_BACKOFF_SECONDS: int = 60

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        transport: TransportAdapter,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT,
        max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_backoff_seconds: int = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._transport = transport
        self._max_batch_size = max_batch_size
        self._batch_size = max(1, min(batch_size, max_batch_size))
        self._lease_seconds = max(5, lease_seconds)
        self._provider_timeout_seconds = max(0.1, provider_timeout_seconds)
        self._max_runtime_seconds = max(1.0, max_runtime_seconds)
        self._max_attempts = max(1, max_attempts)
        self._base_backoff_seconds = max(1, base_backoff_seconds)
        self._max_backoff_seconds = max(base_backoff_seconds, max_backoff_seconds)

    async def claim_due_batch(
        self,
        *,
        relay_token: uuid.UUID,
        batch_size: int | None = None,
        execution_id: uuid.UUID | None = None,
    ) -> list[OutboxMessage]:
        """Atomically claims a batch of due outbox events using SKIP LOCKED.

        Commits the claim before returning so that database locks are released
        prior to making network transport calls.
        """
        effective_batch_size = max(
            1, min(batch_size or self._batch_size, self._max_batch_size)
        )
        now = datetime.now(UTC)
        lease_expiry = now + timedelta(seconds=self._lease_seconds)

        where_clauses = [
            "delivered_at IS NULL",
            "failed_at IS NULL",
            "due_at <= :now",
            "(lease_expiry IS NULL OR lease_expiry <= :now)",
        ]
        params: dict[str, Any] = {
            "relay_token": relay_token,
            "lease_expiry": lease_expiry,
            "now": now,
            "batch_size": effective_batch_size,
        }
        if execution_id is not None:
            where_clauses.append("execution_id = :execution_id")
            params["execution_id"] = execution_id

        where_sql = "\n                  AND ".join(where_clauses)
        claim_sql = text(
            f"""
            UPDATE outbox_events
            SET lease_token = :relay_token,
                lease_expiry = :lease_expiry
            WHERE id IN (
                SELECT id
                FROM outbox_events
                WHERE {where_sql}
                ORDER BY due_at ASC
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, execution_id, task_id, kind, payload_ref, payload, attempts, created_at;
            """
        )

        async with self._session_factory() as session:
            res = await session.execute(claim_sql, params)
            rows = res.mappings().all()
            await session.commit()

        messages: list[OutboxMessage] = []
        for r in rows:
            # Normalize payload from payload_ref or legacy payload
            payload_data = r["payload_ref"] if r["payload_ref"] is not None else (r["payload"] or {})
            messages.append(
                OutboxMessage(
                    event_id=r["id"],
                    execution_id=r["execution_id"],
                    task_id=r["task_id"],
                    kind=r["kind"] or "EVENT",
                    payload=payload_data,
                    attempts=r["attempts"],
                    created_at=r["created_at"],
                )
            )

        return messages

    async def confirm_delivery(
        self,
        *,
        event_id: uuid.UUID,
        relay_token: uuid.UUID,
    ) -> bool:
        """Atomically records successful event delivery fenced by relay_token.

        Returns True on successful confirmation, False if fenced out.
        """
        now = datetime.now(UTC)
        confirm_sql = text(
            """
            UPDATE outbox_events
            SET delivered_at = :now,
                attempts = attempts + 1,
                lease_token = NULL,
                lease_expiry = NULL
            WHERE id = :event_id
              AND lease_token = :relay_token
            RETURNING id;
            """
        )

        async with self._session_factory() as session:
            res = await session.execute(
                confirm_sql,
                {
                    "now": now,
                    "event_id": event_id,
                    "relay_token": relay_token,
                },
            )
            row = res.mappings().first()
            await session.commit()

        return row is not None

    async def handle_delivery_failure(
        self,
        *,
        event_id: uuid.UUID,
        relay_token: uuid.UUID,
        current_attempts: int,
        error_message: str,
    ) -> tuple[bool, bool]:
        """Atomically records transport failure fenced by relay_token.

        Applies capped exponential backoff if retry eligible, or transitions
        to failed_at if attempts are exhausted.

        Returns (was_accepted, is_exhausted):
        - was_accepted: True if update succeeded, False if fenced out.
        - is_exhausted: True if max_attempts reached, False if retry scheduled.
        """
        now = datetime.now(UTC)
        next_attempts = current_attempts + 1
        is_exhausted = next_attempts >= self._max_attempts

        if is_exhausted:
            fail_sql = text(
                """
                UPDATE outbox_events
                SET failed_at = :now,
                    last_error = :error_message,
                    attempts = attempts + 1,
                    lease_token = NULL,
                    lease_expiry = NULL
                WHERE id = :event_id
                  AND lease_token = :relay_token
                RETURNING id;
                """
            )
            params = {
                "now": now,
                "error_message": error_message[:1000],
                "event_id": event_id,
                "relay_token": relay_token,
            }
        else:
            backoff_sec = min(
                self._max_backoff_seconds,
                self._base_backoff_seconds * (2 ** current_attempts),
            )
            new_due_at = now + timedelta(seconds=backoff_sec)

            fail_sql = text(
                """
                UPDATE outbox_events
                SET due_at = :new_due_at,
                    last_error = :error_message,
                    attempts = attempts + 1,
                    lease_token = NULL,
                    lease_expiry = NULL
                WHERE id = :event_id
                  AND lease_token = :relay_token
                RETURNING id;
                """
            )
            params = {
                "new_due_at": new_due_at,
                "error_message": error_message[:1000],
                "event_id": event_id,
                "relay_token": relay_token,
            }

        async with self._session_factory() as session:
            res = await session.execute(fail_sql, params)
            row = res.mappings().first()
            await session.commit()

        was_accepted = row is not None
        return was_accepted, is_exhausted

    async def process_batch(
        self,
        batch: list[OutboxMessage],
        *,
        relay_token: uuid.UUID,
    ) -> OutboxBatchResult:
        """Processes a claimed batch of outbox messages against transport."""
        result = OutboxBatchResult(claimed_count=len(batch))

        for msg in batch:
            try:
                # Bound transport call by provider timeout
                await asyncio.wait_for(
                    self._transport.publish(msg),
                    timeout=self._provider_timeout_seconds,
                )
                confirmed = await self.confirm_delivery(
                    event_id=msg.event_id,
                    relay_token=relay_token,
                )
                if confirmed:
                    result.delivered_count += 1
                else:
                    logger.warning(
                        "Outbox delivery confirmation fenced out for event %s",
                        msg.event_id,
                    )
                    result.fenced_out_count += 1
            except Exception as exc:
                err_str = str(exc) or type(exc).__name__
                logger.warning(
                    "Outbox transport failed for event %s: %s",
                    msg.event_id,
                    err_str,
                )
                accepted, exhausted = await self.handle_delivery_failure(
                    event_id=msg.event_id,
                    relay_token=relay_token,
                    current_attempts=msg.attempts,
                    error_message=err_str,
                )
                if not accepted:
                    result.fenced_out_count += 1
                elif exhausted:
                    result.exhausted_count += 1
                else:
                    result.retry_scheduled_count += 1

        return result

    async def run_relay_loop(
        self,
        *,
        max_runtime_seconds: float | None = None,
        execution_id: uuid.UUID | None = None,
    ) -> OutboxRelayRunSummary:
        """Runs the bounded outbox relay loop until work is complete or time budget expires."""
        runtime_limit = max_runtime_seconds or self._max_runtime_seconds
        start_time = time.monotonic()
        summary = OutboxRelayRunSummary()

        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= runtime_limit:
                summary.terminated_by_timeout = True
                break

            relay_token = uuid.uuid4()
            batch = await self.claim_due_batch(
                relay_token=relay_token,
                execution_id=execution_id,
            )
            if not batch:
                # No more due events
                break

            batch_res = await self.process_batch(batch, relay_token=relay_token)

            summary.batches_processed += 1
            summary.total_claimed += batch_res.claimed_count
            summary.total_delivered += batch_res.delivered_count
            summary.total_retry_scheduled += batch_res.retry_scheduled_count
            summary.total_exhausted += batch_res.exhausted_count
            summary.total_fenced_out += batch_res.fenced_out_count

        summary.elapsed_seconds = time.monotonic() - start_time
        return summary
