"""Verify durable outbox messages retain the workflow and API payload."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from functions.orchestrator.execution import consumer


class RecordingRunner:
    def __init__(self) -> None:
        self.checkpointer = object()
        self.start_event: Any = None
        self.graph: Any = None

    async def handle_execution_start(self, *, event: Any, graph: Any = None) -> None:
        self.start_event = event
        self.graph = graph


@pytest.mark.asyncio
async def test_consumer_routes_composition_and_preserves_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = uuid.uuid4()
    event_id = uuid.uuid4()
    request_payload = {"products": ["media"], "media_url": "https://example.com/media"}
    graph = object()
    runner = RecordingRunner()
    monkeypatch.setattr(consumer, "build_api_composition_workflow", lambda checkpointer: graph)

    await consumer.process_record(
        {
            "body": json.dumps(
                {
                    "kind": "EXECUTION_START",
                    "execution_id": str(execution_id),
                    "event_id": str(event_id),
                    "payload": {
                        "workflow": "API_COMPOSITION",
                        "context_version": 1,
                        "input": {"request": request_payload},
                    },
                }
            )
        },
        runner,  # type: ignore[arg-type]
    )

    assert runner.start_event.execution_id == execution_id
    assert runner.start_event.event_id == event_id
    assert runner.start_event.workflow == "API_COMPOSITION"
    assert runner.start_event.payload == {"request": request_payload}
    assert runner.graph is graph
