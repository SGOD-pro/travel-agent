"""Transport adapter contracts and implementations for outbox relay conforming to spec 0001."""

from __future__ import annotations

import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboxMessage:
    """Immutable envelope for an outbox message dispatched to transport."""

    event_id: uuid.UUID
    execution_id: uuid.UUID
    task_id: uuid.UUID | None
    kind: str
    payload: dict[str, Any]
    attempts: int
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Serializes the message preserving event_id for transport deduplication."""
        return {
            "event_id": str(self.event_id),
            "execution_id": str(self.execution_id),
            "task_id": str(self.task_id) if self.task_id else None,
            "kind": self.kind,
            "payload": self.payload,
            "attempts": self.attempts,
            "created_at": self.created_at.isoformat(),
        }

    def to_json(self) -> str:
        """JSON string representation preserving event_id for transport deduplication."""
        return json.dumps(self.to_dict())


class TransportAdapter(ABC):
    """Abstract interface for publishing outbox events to a message queue."""

    @abstractmethod
    async def publish(self, message: OutboxMessage) -> None:
        """Publishes a single outbox event to the queue transport.

        Must preserve event_id for receiver deduplication.
        Raises an exception on transport or network failures.
        """
        pass



class NullTransportAdapter(TransportAdapter):
    """No-op transport for local development and tests without a real SQS queue.

    Logs a warning on every publish call so callers know messages are being
    discarded. Use this as the local fallback instead of constructing
    SqsTransportAdapter with sqs_client=None.
    """

    async def publish(self, message: OutboxMessage) -> None:
        logger.warning(
            "NullTransportAdapter: discarding outbox message %s (kind=%s) — "
            "no SQS client configured",
            message.event_id,
            message.kind,
        )


class ControlledTransportAdapter(TransportAdapter):
    """Test transport adapter providing controlled failure injection and inspection."""

    def __init__(self) -> None:
        self.published: list[OutboxMessage] = []
        self.fail_all: bool = False
        self.fail_event_ids: set[uuid.UUID] = set()
        self.delay_seconds: float = 0.0
        self.error_to_raise: Exception = RuntimeError("Controlled transport network failure")
        self.on_publish_hook: Callable[[OutboxMessage], Coroutine[Any, Any, None]] | None = None

    async def publish(self, message: OutboxMessage) -> None:
        if self.fail_all or message.event_id in self.fail_event_ids:
            raise self.error_to_raise

        if self.delay_seconds > 0:
            import asyncio

            await asyncio.sleep(self.delay_seconds)

        self.published.append(message)

        if self.on_publish_hook is not None:
            await self.on_publish_hook(message)


class SqsTransportAdapter(TransportAdapter):
    """Amazon SQS transport adapter publishing events preserving event_id."""

    def __init__(
        self,
        queue_url: str,
        sqs_client: Any,
        is_fifo: bool = False,
    ) -> None:
        if sqs_client is None:
            raise ValueError(
                "SqsTransportAdapter requires a real SQS client. "
                "For local development without SQS, use NullTransportAdapter instead."
            )
        self.queue_url = queue_url
        self.sqs_client = sqs_client
        self.is_fifo = is_fifo

    async def publish(self, message: OutboxMessage) -> None:

        params: dict[str, Any] = {
            "QueueUrl": self.queue_url,
            "MessageBody": message.to_json(),
            "MessageAttributes": {
                "EventId": {
                    "DataType": "String",
                    "StringValue": str(message.event_id),
                },
                "Kind": {
                    "DataType": "String",
                    "StringValue": message.kind,
                },
            },
        }

        if self.is_fifo:
            params["MessageGroupId"] = str(message.execution_id)
            params["MessageDeduplicationId"] = str(message.event_id)

        # Supports both async clients (aioboto3) and standard boto3 via asyncio loop
        if hasattr(self.sqs_client, "send_message"):
            send_fn = self.sqs_client.send_message
            import inspect

            if inspect.iscoroutinefunction(send_fn):
                await send_fn(**params)
            else:
                import asyncio

                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: send_fn(**params))


def build_sqs_transport() -> TransportAdapter:
    """Builds an SQS transport from environment configuration, or a NullTransportAdapter.

    Reads OUTBOX_QUEUE_URL from the environment.  When boto3 is not installed
    or the env var is absent this returns a NullTransportAdapter (logs warnings,
    discards messages) so local processes do not crash-loop on every relay tick.

    Use SqsTransportAdapter directly only when you already hold a configured client.
    """
    queue_url = os.environ.get("OUTBOX_QUEUE_URL", "")
    if not queue_url:
        logger.warning("OUTBOX_QUEUE_URL not set; using NullTransportAdapter (messages discarded)")
        return NullTransportAdapter()

    try:
        import boto3

        sqs_client = boto3.client(
            "sqs", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        return SqsTransportAdapter(
            queue_url=queue_url,
            sqs_client=sqs_client,
            is_fifo=queue_url.endswith(".fifo"),
        )
    except ImportError:
        logger.warning(
            "boto3 not installed; using NullTransportAdapter (messages discarded). "
            "Install boto3 to enable SQS publishing."
        )
        return NullTransportAdapter()

