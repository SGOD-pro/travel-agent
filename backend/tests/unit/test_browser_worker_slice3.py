"""Unit tests for spec 0003 Slice 3: Fenced Browser Worker Service and Observation Persistence.

Covers:
- AC-3: Browser worker claims and executes tasks under WorkerFencingService lease fencing,
  validating parent execution status, context version, and attempt counters.
- AC-6: Rendered content parsed via Scrapling into clean entity observations and persisted
  as immutable SourceObservationModel in source_observations with extraction_method="BROWSER_HTML",
  referencing placeholder observation UUID in artifact_ref without in-place mutations.
- AC-7: When navigation times out, crashes, or encounters bot challenges, worker records explicit
  unavailable source entry in worker_results and completes task under lease fencing as success.
- Modular isolation: zero imports from functions.evidence_collector.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from contracts.browser import (
    BrowserExtractionPort,
    BrowserRenderResult,
    BrowserRenderStatus,
    BrowserResourceUsage,
)
from functions.browser.fixtures import compute_content_hash
from functions.browser.parser import (
    parse_inr_price,
    parse_rendered_entities,
)
from functions.browser.service import (
    BrowserWorkerService,
    parse_browser_dispatch_message,
)
from functions.orchestrator.execution.fencing import (
    AttemptsExhaustedError,
    ParentCancelledError,
    ParentTerminalError,
    StaleContextError,
    TaskClaimResult,
    WorkerFencingService,
)

# ============================================================================
# 1. Scrapling Entity Parsing Unit Tests (AC-6)
# ============================================================================


def test_parse_inr_price_numeric_and_symbols() -> None:
    """Verifies strict decimal parsing for Indian Rupee formats and non-coercion."""
    assert parse_inr_price("₹4,500.00") == Decimal("4500.00")
    assert parse_inr_price("Rs. 1,250") == Decimal("1250")
    assert parse_inr_price("INR 8000") == Decimal("8000")
    assert parse_inr_price("350.50") == Decimal("350.50")

    # Strict zero coercion: negative, missing, or zero strings return None
    assert parse_inr_price(None) is None
    assert parse_inr_price("") is None
    assert parse_inr_price("   ") is None
    assert parse_inr_price("Price on request") is None
    assert parse_inr_price("-₹500") is None
    assert parse_inr_price("₹0") is None


def test_parse_rendered_entities_lodging_cards() -> None:
    """Verifies extraction of hotel entities from rendered HTML using Scrapling."""
    html = """
    <html>
      <body>
        <div class="hotel-card">
          <h2 class="property-name">Seaside Luxury Resort</h2>
          <div class="price">₹6,800 / night</div>
          <span class="rating">4.7 / 5</span>
        </div>
        <div class="hotel-card">
          <span class="title">Boutique Heritage Villa</span>
          <span class="price-val">Rs. 3,200</span>
          <span class="score">4.2</span>
        </div>
        <div class="hotel-card">
          <span class="title">Unquoted Homestay</span>
          <span class="price">Call for rates</span>
        </div>
      </body>
    </html>
    """
    entities = parse_rendered_entities(html, category="lodging", canonical_url="https://example.com/hotels")
    assert len(entities) == 2
    assert entities[0]["property_name"] == "Seaside Luxury Resort"
    assert entities[0]["price_per_night"] == "6800"
    assert entities[0]["currency"] == "INR"
    assert entities[0]["rating"] == 4.7

    assert entities[1]["property_name"] == "Boutique Heritage Villa"
    assert entities[1]["price_per_night"] == "3200"
    assert entities[1]["currency"] == "INR"
    assert entities[1]["rating"] == 4.2


def test_parse_rendered_entities_transit_fares() -> None:
    """Verifies extraction of transit schedule and fare entities from rendered HTML."""
    html = """
    <html>
      <body>
        <div class="fare-row">
          <span class="operator">Bengal Express</span>
          <span class="departure-time">06:00</span>
          <span class="arrival-time">10:30</span>
          <span class="fare">₹450</span>
        </div>
        <div class="fare-row">
          <span class="carrier">Kolkata Metro</span>
          <span class="fare">Rs. 20</span>
        </div>
      </body>
    </html>
    """
    entities = parse_rendered_entities(html, category="transit", canonical_url="https://example.com/transit")
    assert len(entities) == 2
    assert entities[0]["operator_name"] == "Bengal Express"
    assert entities[0]["price"] == "450"
    assert entities[0]["departure_time"] == "06:00"
    assert entities[0]["arrival_time"] == "10:30"

    assert entities[1]["operator_name"] == "Kolkata Metro"
    assert entities[1]["price"] == "20"


def test_parse_rendered_entities_empty_or_malformed() -> None:
    """Verifies that empty or invalid HTML returns empty list safely."""
    assert parse_rendered_entities("", category="lodging", canonical_url="https://example.com") == []
    assert parse_rendered_entities("   ", category="transit", canonical_url="https://example.com") == []
    assert parse_rendered_entities("<div>Just plain text</div>", category="lodging", canonical_url="https://example.com") == []


# ============================================================================
# 2. Message Dispatch Parsing Unit Tests
# ============================================================================


def test_parse_browser_dispatch_message_direct_dict() -> None:
    """Verifies parsing of direct task dispatch dictionary."""
    task_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    msg_dict = {
        "task_id": str(task_id),
        "execution_id": str(exec_id),
        "worker": "BROWSER",
        "action": "render",
        "attempt": 1,
        "context_version": 2,
        "payload": {
            "url": "https://example.com/page",
            "canonical_url": "https://example.com/page",
            "category": "lodging",
            "reason": "DYNAMIC_JAVASCRIPT",
        },
    }
    envelope = parse_browser_dispatch_message(msg_dict)
    assert envelope.task_id == task_id
    assert envelope.execution_id == exec_id
    assert envelope.worker == "BROWSER"
    assert envelope.action == "render"
    assert envelope.attempt == 1
    assert envelope.context_version == 2
    assert envelope.payload["canonical_url"] == "https://example.com/page"

    queue_msg = envelope.to_task_queue_message()
    assert queue_msg.task_id == task_id
    assert queue_msg.execution_id == exec_id
    assert queue_msg.attempt == 1


def test_parse_browser_dispatch_message_sqs_wrapped_body() -> None:
    """Verifies parsing of SQS event structure with serialized JSON body."""
    task_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    sqs_record = {
        "body": f'{{"task_id": "{task_id}", "execution_id": "{exec_id}", "worker": "BROWSER", "action": "render", "payload": {{"url": "https://example.com"}}}}'
    }
    envelope = parse_browser_dispatch_message(sqs_record)
    assert envelope.task_id == task_id
    assert envelope.execution_id == exec_id
    assert envelope.worker == "BROWSER"


def test_parse_browser_dispatch_message_missing_ids_raises() -> None:
    """Verifies that missing task_id or execution_id raises ValueError."""
    with pytest.raises(ValueError, match="Missing required task_id or execution_id"):
        parse_browser_dispatch_message({"worker": "BROWSER"})


# ============================================================================
# 3. Fenced Worker Claim Validation Tests (AC-3)
# ============================================================================


@pytest.mark.asyncio
async def test_worker_fencing_rejects_cancelled_parent_before_browser_automation() -> None:
    """AC-3: When parent execution is cancelled, claim_task raises ParentCancelledError and browser is not called."""
    mock_session = AsyncMock()
    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.side_effect = ParentCancelledError("Execution was cancelled")

    mock_port = AsyncMock(spec=BrowserExtractionPort)
    service = BrowserWorkerService(mock_session, extraction_port=mock_port, fencing_service=mock_fencing)

    msg = {
        "task_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "worker": "BROWSER",
        "action": "render",
        "payload": {"url": "https://example.com", "canonical_url": "https://example.com", "category": "lodging", "reason": "TEST"},
    }

    with pytest.raises(ParentCancelledError):
        await service.execute_render_task(msg)

    # Browser automation port MUST NEVER be called
    mock_port.render_page.assert_not_called()


@pytest.mark.asyncio
async def test_worker_fencing_rejects_terminal_parent_before_browser_automation() -> None:
    """AC-3: When parent execution is in terminal state, claim_task raises ParentTerminalError and browser is not called."""
    mock_session = AsyncMock()
    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.side_effect = ParentTerminalError("Execution already succeeded")

    mock_port = AsyncMock(spec=BrowserExtractionPort)
    service = BrowserWorkerService(mock_session, extraction_port=mock_port, fencing_service=mock_fencing)

    msg = {
        "task_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "payload": {"url": "https://example.com", "canonical_url": "https://example.com", "category": "lodging", "reason": "TEST"},
    }

    with pytest.raises(ParentTerminalError):
        await service.execute_render_task(msg)

    mock_port.render_page.assert_not_called()


@pytest.mark.asyncio
async def test_worker_fencing_rejects_stale_context_version() -> None:
    """AC-3: Context version mismatch raises StaleContextError before browser execution."""
    mock_session = AsyncMock()
    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.side_effect = StaleContextError("Context version mismatch")

    mock_port = AsyncMock(spec=BrowserExtractionPort)
    service = BrowserWorkerService(mock_session, extraction_port=mock_port, fencing_service=mock_fencing)

    msg = {
        "task_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "context_version": 1,
        "payload": {"url": "https://example.com", "canonical_url": "https://example.com", "category": "lodging", "reason": "TEST"},
    }

    with pytest.raises(StaleContextError):
        await service.execute_render_task(msg)

    mock_port.render_page.assert_not_called()


@pytest.mark.asyncio
async def test_worker_fencing_rejects_attempts_exhausted() -> None:
    """AC-3: When max attempts exhausted, raises AttemptsExhaustedError before browser execution."""
    mock_session = AsyncMock()
    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.side_effect = AttemptsExhaustedError("Attempts exhausted (3/3)")

    mock_port = AsyncMock(spec=BrowserExtractionPort)
    service = BrowserWorkerService(mock_session, extraction_port=mock_port, fencing_service=mock_fencing)

    msg = {
        "task_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "attempt": 3,
        "payload": {"url": "https://example.com", "canonical_url": "https://example.com", "category": "lodging", "reason": "TEST"},
    }

    with pytest.raises(AttemptsExhaustedError):
        await service.execute_render_task(msg)

    mock_port.render_page.assert_not_called()


# ============================================================================
# 4. Successful Observation Persistence Unit Tests (AC-6)
# ============================================================================


@pytest.mark.asyncio
async def test_execute_render_task_success_persists_immutable_observation_and_completes() -> None:
    """AC-6: Successful render inserts SourceObservationModel, links placeholder UUID in artifact_ref, and completes task."""
    task_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    placeholder_obs_id = uuid.uuid4()
    persisted_obs_id = uuid.uuid4()
    result_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_exec_res = MagicMock()
    mock_exec_res.scalar_one.return_value = persisted_obs_id
    mock_exec_res.scalar_one_or_none.return_value = persisted_obs_id
    mock_session.execute.return_value = mock_exec_res

    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=exec_id,
        worker="BROWSER",
        action="render",
        attempt=1,
        context_version=1,
        lease_token=lease_token,
        lease_expiry=datetime.now(UTC),
        payload_ref={
            "url": "https://dynamic.example.com/resorts/palace?ref=web",
            "canonical_url": "https://dynamic.example.com/resorts/palace",
            "category": "lodging",
            "reason": "NOSCRIPT_JAVASCRIPT_REQUIRED",
            "wait_until": "networkidle",
            "timeout_seconds": 20,
            "placeholder_observation_id": str(placeholder_obs_id),
        },
    )
    mock_fencing.complete_task.return_value = result_id

    # Create canned success outcome from FixtureBrowserAdapter
    excerpt_text = "[FIXTURE_RENDERED] Grand Palace Resort content"
    canned_result = BrowserRenderResult(
        status=BrowserRenderStatus.SUCCESS,
        canonical_url="https://dynamic.example.com/resorts/palace",
        effective_url="https://dynamic.example.com/resorts/palace?ref=web",
        content_hash=compute_content_hash(excerpt_text),
        excerpt_text=excerpt_text,
        excerpt_locator="main#content",
        rendered_html_snippet="<html><body><div class='hotel-card'><h2>Grand Palace Resort</h2><div class='price'>₹5,200</div></div></body></html>",
        render_duration_ms=450,
        unavailable_reason=None,
        usage=BrowserResourceUsage(duration_ms=450, memory_mb=256),
        parsed_lodging_observations=[],
        parsed_transit_observations=[],
    )

    mock_port = AsyncMock(spec=BrowserExtractionPort)
    mock_port.render_page.return_value = canned_result

    service = BrowserWorkerService(mock_session, extraction_port=mock_port, fencing_service=mock_fencing)

    res_id, output = await service.execute_render_task({
        "task_id": str(task_id),
        "execution_id": str(exec_id),
    })

    assert res_id == result_id
    assert output.status == BrowserRenderStatus.SUCCESS
    assert output.observation_id == persisted_obs_id
    assert output.content_hash == canned_result.content_hash
    assert output.canonical_url == "https://dynamic.example.com/resorts/palace"
    assert output.unavailable_sources == []
    assert len(output.parsed_observations) == 1
    assert output.parsed_observations[0]["property_name"] == "Grand Palace Resort"
    assert output.parsed_observations[0]["price_per_night"] == "5200"

    # Verify session execute called to insert observation
    assert mock_session.execute.call_count >= 2

    # Verify complete_task called with valid BrowserWorkerOutput payload
    mock_fencing.complete_task.assert_called_once()
    complete_kwargs = mock_fencing.complete_task.call_args[1]
    assert complete_kwargs["execution_id"] == exec_id
    assert complete_kwargs["task_id"] == task_id
    assert complete_kwargs["lease_token"] == lease_token
    assert complete_kwargs["output"]["status"] == "SUCCESS"
    assert complete_kwargs["output"]["observation_id"] == str(persisted_obs_id)


@pytest.mark.asyncio
async def test_execute_render_task_success_fallback_to_obs_id_when_lookup_none() -> None:
    """Verifies that if scalar_one_or_none returns None, worker defensively falls back to obs_id."""
    task_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    result_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_exec_res = MagicMock()
    mock_exec_res.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_exec_res

    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=exec_id,
        worker="BROWSER",
        action="render",
        attempt=1,
        context_version=1,
        lease_token=lease_token,
        lease_expiry=datetime.now(UTC),
        payload_ref={
            "url": "https://example.com/fallback",
            "canonical_url": "https://example.com/fallback",
            "category": "lodging",
            "reason": "TEST",
        },
    )
    mock_fencing.complete_task.return_value = result_id

    canned_result = BrowserRenderResult(
        status=BrowserRenderStatus.SUCCESS,
        canonical_url="https://example.com/fallback",
        effective_url="https://example.com/fallback",
        content_hash=compute_content_hash("Fallback content"),
        excerpt_text="Fallback content",
        excerpt_locator="body",
        rendered_html_snippet=None,
        render_duration_ms=100,
        unavailable_reason=None,
        usage=BrowserResourceUsage(duration_ms=100, memory_mb=256),
        parsed_lodging_observations=[],
        parsed_transit_observations=[],
    )

    mock_port = AsyncMock(spec=BrowserExtractionPort)
    mock_port.render_page.return_value = canned_result

    service = BrowserWorkerService(mock_session, extraction_port=mock_port, fencing_service=mock_fencing)

    res_id, output = await service.execute_render_task({
        "task_id": str(task_id),
        "execution_id": str(exec_id),
    })

    assert res_id == result_id
    assert output.status == BrowserRenderStatus.SUCCESS
    assert output.observation_id is not None
    assert isinstance(output.observation_id, uuid.UUID)
    mock_fencing.complete_task.assert_called_once()
    complete_kwargs = mock_fencing.complete_task.call_args[1]
    assert complete_kwargs["output"]["observation_id"] == str(output.observation_id)


# ============================================================================
# 5. Graceful Error Degradation Unit Tests (AC-7)
# ============================================================================


@pytest.mark.asyncio
async def test_execute_render_task_timed_out_degrades_gracefully() -> None:
    """AC-7: Navigation timeout records unavailable source record and completes task as success without fabricating content."""
    task_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    result_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=exec_id,
        worker="BROWSER",
        action="render",
        attempt=1,
        context_version=1,
        lease_token=lease_token,
        lease_expiry=datetime.now(UTC),
        payload_ref={
            "url": "https://slow.example.com",
            "canonical_url": "https://slow.example.com",
            "category": "lodging",
            "reason": "TEST",
            "timeout_seconds": 20,
        },
    )
    mock_fencing.complete_task.return_value = result_id

    timed_out_result = BrowserRenderResult(
        status=BrowserRenderStatus.TIMED_OUT,
        canonical_url="https://slow.example.com",
        effective_url="https://slow.example.com",
        content_hash=None,
        excerpt_text=None,
        excerpt_locator=None,
        rendered_html_snippet=None,
        render_duration_ms=20000,
        unavailable_reason="Navigation timeout of 20 seconds exceeded",
        usage=BrowserResourceUsage(duration_ms=20000, memory_mb=512),
        parsed_lodging_observations=[],
        parsed_transit_observations=[],
    )

    mock_port = AsyncMock(spec=BrowserExtractionPort)
    mock_port.render_page.return_value = timed_out_result

    service = BrowserWorkerService(mock_session, extraction_port=mock_port, fencing_service=mock_fencing)

    res_id, output = await service.execute_render_task({
        "task_id": str(task_id),
        "execution_id": str(exec_id),
    })

    assert res_id == result_id
    assert output.status == BrowserRenderStatus.TIMED_OUT
    assert output.observation_id is None
    assert output.content_hash is None
    assert output.rendered_html_bytes == 0
    assert len(output.unavailable_sources) == 1
    assert output.unavailable_sources[0]["url"] == "https://slow.example.com"
    assert "timeout" in output.unavailable_sources[0]["reason"].lower()

    # Zero source observations should be inserted on timeout
    mock_session.execute.assert_not_called()

    # complete_task must be called so parent execution continues smoothly
    mock_fencing.complete_task.assert_called_once()
    complete_kwargs = mock_fencing.complete_task.call_args[1]
    assert complete_kwargs["output"]["status"] == "TIMED_OUT"
    assert complete_kwargs["output"]["observation_id"] is None
    assert complete_kwargs["output"]["content_hash"] is None


@pytest.mark.asyncio
async def test_execute_render_task_blocked_by_bot_challenge_degrades_gracefully() -> None:
    """AC-7: Bot challenge / cloudflare block completes task gracefully with BLOCKED status."""
    task_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    result_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=exec_id,
        worker="BROWSER",
        action="render",
        attempt=1,
        context_version=1,
        lease_token=lease_token,
        lease_expiry=datetime.now(UTC),
        payload_ref={
            "url": "https://protected.example.com",
            "canonical_url": "https://protected.example.com",
            "category": "lodging",
            "reason": "TEST",
        },
    )
    mock_fencing.complete_task.return_value = result_id

    blocked_result = BrowserRenderResult(
        status=BrowserRenderStatus.BLOCKED,
        canonical_url="https://protected.example.com",
        effective_url="https://protected.example.com",
        content_hash=None,
        excerpt_text=None,
        excerpt_locator=None,
        rendered_html_snippet=None,
        render_duration_ms=1800,
        unavailable_reason="Cloudflare browser verification challenge encountered",
        usage=BrowserResourceUsage(duration_ms=1800, memory_mb=350),
        parsed_lodging_observations=[],
        parsed_transit_observations=[],
    )

    mock_port = AsyncMock(spec=BrowserExtractionPort)
    mock_port.render_page.return_value = blocked_result

    service = BrowserWorkerService(mock_session, extraction_port=mock_port, fencing_service=mock_fencing)

    res_id, output = await service.execute_render_task({
        "task_id": str(task_id),
        "execution_id": str(exec_id),
    })

    assert output.status == BrowserRenderStatus.BLOCKED
    assert output.observation_id is None
    assert output.content_hash is None
    assert "Cloudflare" in output.unavailable_sources[0]["reason"]
    mock_fencing.complete_task.assert_called_once()


@pytest.mark.asyncio
async def test_execute_render_task_driver_exception_converts_to_degraded_outcome() -> None:
    """AC-7: Driver exception (e.g. browser binary crash or timeout exception) completes task gracefully."""
    task_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    result_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_fencing = AsyncMock(spec=WorkerFencingService)
    mock_fencing.claim_task.return_value = TaskClaimResult(
        task_id=task_id,
        execution_id=exec_id,
        worker="BROWSER",
        action="render",
        attempt=1,
        context_version=1,
        lease_token=lease_token,
        lease_expiry=datetime.now(UTC),
        payload_ref={
            "url": "https://crash.example.com",
            "canonical_url": "https://crash.example.com",
            "category": "lodging",
            "reason": "TEST",
            "timeout_seconds": 15,
        },
    )
    mock_fencing.complete_task.return_value = result_id

    mock_port = AsyncMock(spec=BrowserExtractionPort)
    mock_port.render_page.side_effect = TimeoutError("Page navigation timeout exceeded")

    service = BrowserWorkerService(mock_session, extraction_port=mock_port, fencing_service=mock_fencing)

    res_id, output = await service.execute_render_task({
        "task_id": str(task_id),
        "execution_id": str(exec_id),
    })

    assert output.status == BrowserRenderStatus.TIMED_OUT
    assert output.observation_id is None
    assert "timeout" in output.unavailable_sources[0]["reason"].lower()
    mock_fencing.complete_task.assert_called_once()


# ============================================================================
# 6. Modular Isolation Invariant Tests
# ============================================================================


def test_browser_package_has_zero_imports_from_evidence_collector() -> None:
    """Verifies strict modular boundary: functions.browser must never import functions.evidence_collector."""
    import functions.browser.models as models_mod
    import functions.browser.parser as parser_mod
    import functions.browser.service as service_mod

    for mod in (models_mod, parser_mod, service_mod):
        for name, val in vars(mod).items():
            if hasattr(val, "__module__") and val.__module__:
                assert not val.__module__.startswith("functions.evidence_collector"), (
                    f"Forbidden cross-worker import in {mod.__name__}: {name} from {val.__module__}"
                )
