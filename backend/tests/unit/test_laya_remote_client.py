"""Unit tests for RemoteLayaDecisionAdapter and payload sanitization.

Conforms to spec 0004 Slice 2:
1. Upstream wire communication over HTTP via httpx with POST /v1/systemone.
2. Dual authentication: separate Bearer and Cloudflare Access headers.
3. Cumulative total deadline (5.0s default) and individual socket timeouts (2.5s default).
4. Transient 5xx retry loop with exponential backoff and fast failure on 4xx.
5. Immediate fast failure on unconfigured LAYA_ENDPOINT_URL.
6. Deterministic singleton bypass without network overhead.
7. Strict mode missing field defaults and unknown truncation status.
8. Payload sanitization stripping Bearer tokens, Cloudflare credentials, and traveler PII.
9. Health probe GET /health execution.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from config.config import Settings
from contracts.decision import (
    DecisionAction,
    DecisionPhase,
    DecisionStatus,
    DecisionType,
    LayaDecisionRequest,
)
from functions.decision_router.client import RemoteLayaDecisionAdapter
from functions.decision_router.fixtures import FixtureLayaDecisionAdapter
from functions.decision_router.sanitization import (
    sanitize_context,
    sanitize_headers,
    sanitize_text,
)

SAMPLE_EXECUTION_ID = UUID("3fa85f64-5717-4562-b3fc-2c963f66afa6")
SAMPLE_ENDPOINT = "https://laya.example.cloudflarestream.com"


def _make_request(
    eligible_choices: list[DecisionAction] | None = None,
    phase: DecisionPhase = DecisionPhase.BROWSER_ELIGIBILITY,
    context: dict[str, object] | None = None,
) -> LayaDecisionRequest:
    choices = eligible_choices or [
        DecisionAction.DISPATCH_BROWSER_ESCALATION,
        DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
        DecisionAction.DISPATCH_EVIDENCE_SEARCH,
    ]
    ctx = context or {
        "execution_id": str(SAMPLE_EXECUTION_ID),
        "phase": phase.value,
        "category": "lodging",
        "destination": "Kolkata",
        "static_search_completed": True,
        "unhandled_browser_requests": 1,
        "browser_escalation_slots_remaining": 3,
        "traveler_name": "Sensitive John Doe",
        "email": "john.doe@example.com",
    }
    return LayaDecisionRequest(
        decision_id=uuid4(),
        decision_type=DecisionType.EVIDENCE_ROUTING,
        phase=phase,
        execution_id=SAMPLE_EXECUTION_ID,
        trip_id=None,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="evidence_routing:v1",
        policy_derived_eligible_choices=choices,
        budget_remaining=3,
        context=ctx,
        idempotency_key=f"{SAMPLE_EXECUTION_ID}:laya_decision:{phase.value}:1234",
    )


# ============================================================================
# 1. Sanitization Utilities Tests
# ============================================================================


def test_sanitize_headers_redacts_credentials() -> None:
    """Verifies that Authorization and Cloudflare Access headers are redacted."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer secret_laya_key_123",
        "CF-Access-Client-Id": "cf_client_id_456",
        "CF-Access-Client-Secret": "cf_client_secret_789",
        "X-Custom-Header": "PublicValue",
    }
    sanitized = sanitize_headers(headers)

    assert sanitized["Content-Type"] == "application/json"
    assert sanitized["X-Custom-Header"] == "PublicValue"
    assert sanitized["Authorization"] == "[REDACTED]"
    assert sanitized["CF-Access-Client-Id"] == "[REDACTED]"
    assert sanitized["CF-Access-Client-Secret"] == "[REDACTED]"


def test_sanitize_context_strips_pii_and_disallowed_keys() -> None:
    """Verifies that traveler PII and disallowed keys are removed from context."""
    raw_ctx = {
        "execution_id": str(SAMPLE_EXECUTION_ID),
        "phase": "BROWSER_ELIGIBILITY",
        "category": "lodging",
        "destination": "Kolkata",
        "traveler_name": "John Doe",
        "email": "johndoe@example.com",
        "phone": "+1-555-0199",
        "user_id": "usr_999",
        "token": "tok_xyz",
        "sanitized_summary": "Short summary",
        "static_search_completed": True,
    }
    sanitized = sanitize_context(raw_ctx)

    assert "traveler_name" not in sanitized
    assert "email" not in sanitized
    assert "phone" not in sanitized
    assert "user_id" not in sanitized
    assert "token" not in sanitized

    assert sanitized["execution_id"] == str(SAMPLE_EXECUTION_ID)
    assert sanitized["destination"] == "Kolkata"
    assert sanitized["sanitized_summary"] == "Short summary"


def test_sanitize_context_bounds_long_summary() -> None:
    """Verifies that oversized summary strings are bounded client side."""
    huge_summary = "A" * 6000
    sanitized = sanitize_context({"sanitized_summary": huge_summary}, max_summary_length=100)
    assert len(sanitized["sanitized_summary"]) <= 120
    assert sanitized["sanitized_summary"].endswith("... [truncated]")


def test_sanitize_text_redacts_known_secrets() -> None:
    """Verifies string secret replacement in logs."""
    text = "Failed to connect with Authorization: Bearer abc123def456ghi and secret_token_xyz"
    cleaned = sanitize_text(text, secrets=["secret_token_xyz"])
    assert "secret_token_xyz" not in cleaned
    assert "[REDACTED]" in cleaned


# ============================================================================
# 2. Dual Authentication and Request Dispatch Tests
# ============================================================================


@pytest.mark.asyncio
async def test_dual_authentication_headers_attached() -> None:
    """Verifies that separate Bearer and Cloudflare Access headers are attached."""
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        ext_wire = FixtureLayaDecisionAdapter.make_sample_extended_wire_response()
        return httpx.Response(200, json=ext_wire.model_dump(mode="json"))

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT,
        LAYA_API_KEY="my-bearer-api-key",
        CF_ACCESS_CLIENT_ID="my-cf-client-id",
        CF_ACCESS_CLIENT_SECRET="my-cf-client-secret",
    )

    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        request = _make_request()
        envelope = await adapter.query_decision(request)

        assert envelope.status == DecisionStatus.VALIDATED
        assert captured_headers.get("authorization") == "Bearer my-bearer-api-key"
        assert captured_headers.get("cf-access-client-id") == "my-cf-client-id"
        assert captured_headers.get("cf-access-client-secret") == "my-cf-client-secret"


@pytest.mark.asyncio
async def test_request_payload_is_sanitized_before_dispatch() -> None:
    """Verifies that traveler PII is omitted from outbound wire request."""
    captured_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        captured_payload.update(data)
        ext_wire = FixtureLayaDecisionAdapter.make_sample_extended_wire_response()
        return httpx.Response(200, json=ext_wire.model_dump(mode="json"))

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT)

    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        request = _make_request(
            context={
                "execution_id": str(SAMPLE_EXECUTION_ID),
                "destination": "Kolkata",
                "traveler_name": "John Doe",
                "email": "john@example.com",
            }
        )
        await adapter.query_decision(request)

        state = captured_payload.get("state", {})
        assert "traveler_name" not in state
        assert "email" not in state
        assert state.get("destination") == "Kolkata"


# ============================================================================
# 3. Deterministic Singleton and Unconfigured Bypass Tests
# ============================================================================


@pytest.mark.asyncio
async def test_deterministic_singleton_bypasses_remote_inference() -> None:
    """Verifies that single eligible choice executes singleton bypass with zero network calls."""
    mock_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mock_called
        mock_called = True
        return httpx.Response(200, json={})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT)

    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        single_choice_req = _make_request(
            eligible_choices=[DecisionAction.DISPATCH_EVIDENCE_SEARCH],
            phase=DecisionPhase.SEARCH_COMPLETION,
        )
        envelope = await adapter.query_decision(single_choice_req)

        assert mock_called is False
        assert envelope.status == DecisionStatus.DETERMINISTIC_SINGLETON
        assert envelope.allowed_choice == DecisionAction.DISPATCH_EVIDENCE_SEARCH
        assert envelope.confidence is None
        assert envelope.answer_confidence is None
        assert envelope.latency_ms == 0
        assert envelope.model_version == "deterministic_singleton"


@pytest.mark.asyncio
async def test_unconfigured_endpoint_fast_failure() -> None:
    """Verifies that empty LAYA_ENDPOINT_URL fails immediately with FALLBACK_UNCONFIGURED."""
    settings = Settings(LAYA_ENDPOINT_URL=None)
    adapter = RemoteLayaDecisionAdapter(settings=settings)

    req = _make_request()
    envelope = await adapter.query_decision(req)

    assert envelope.status == DecisionStatus.FALLBACK_UNCONFIGURED
    assert envelope.confidence is None
    assert envelope.answer_confidence is None
    assert envelope.latency_ms == 0
    assert "empty or unconfigured" in (envelope.rationale or "")


# ============================================================================
# 4. Strict Mode and Truncation Tests
# ============================================================================


@pytest.mark.asyncio
async def test_strict_mode_response_missing_fields_defaults() -> None:
    """Verifies handling of strict Jev mode response where optional fields are absent."""

    def handler(request: httpx.Request) -> httpx.Response:
        strict_wire = FixtureLayaDecisionAdapter.make_sample_strict_wire_response(
            choice="DISPATCH_BROWSER_ESCALATION",
            model="typed-decisions",
        )
        return httpx.Response(200, json=strict_wire.model_dump(mode="json"))

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT)

    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        req = _make_request()
        envelope = await adapter.query_decision(req)

        assert envelope.status == DecisionStatus.VALIDATED
        assert envelope.allowed_choice == DecisionAction.DISPATCH_BROWSER_ESCALATION
        assert envelope.answer_confidence == 0.820
        assert envelope.confidence is None
        assert envelope.model_version == "typed-decisions"


@pytest.mark.asyncio
async def test_truncated_tokenizer_prompt_triggers_fallback() -> None:
    """Verifies that usage.truncated=True triggers FALLBACK_TRUNCATED."""

    def handler(request: httpx.Request) -> httpx.Response:
        wire = FixtureLayaDecisionAdapter.make_sample_extended_wire_response(truncated=True)
        return httpx.Response(200, json=wire.model_dump(mode="json"))

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT)

    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        req = _make_request()
        envelope = await adapter.query_decision(req)

        assert envelope.status == DecisionStatus.FALLBACK_TRUNCATED
        assert envelope.confidence is None
        assert envelope.answer_confidence is None


# ============================================================================
# 5. Deadlines, Retries, and Error Handling
# ============================================================================


@pytest.mark.asyncio
async def test_http_4xx_client_error_zero_retries() -> None:
    """Verifies that 4xx client errors fail immediately with zero retries."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(403, json={"error": "Forbidden Cloudflare token"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT, LAYA_MAX_RETRIES=2)

    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        req = _make_request()
        envelope = await adapter.query_decision(req)

        assert request_count == 1
        assert adapter.retry_count == 0
        assert envelope.status == DecisionStatus.FALLBACK_ERROR
        assert "Remote HTTP 403" in (envelope.rationale or "")


@pytest.mark.asyncio
async def test_transient_5xx_retries_and_recovers() -> None:
    """Verifies that transient 5xx errors retry with backoff and succeed when healed."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(502, text="Bad Gateway")
        wire = FixtureLayaDecisionAdapter.make_sample_extended_wire_response()
        return httpx.Response(200, json=wire.model_dump(mode="json"))

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT, LAYA_MAX_RETRIES=2)

    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        req = _make_request()
        envelope = await adapter.query_decision(req)

        assert request_count == 2
        assert adapter.retry_count == 1
        assert envelope.status == DecisionStatus.VALIDATED


@pytest.mark.asyncio
async def test_transient_5xx_exhausts_retries() -> None:
    """Verifies that persistent 5xx server errors exhaust max retries and fail."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(503, text="Service Unavailable")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT, LAYA_MAX_RETRIES=2)

    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        req = _make_request()
        envelope = await adapter.query_decision(req)

        assert request_count == 3
        assert adapter.retry_count == 2
        assert envelope.status == DecisionStatus.FALLBACK_ERROR


@pytest.mark.asyncio
async def test_socket_timeout_triggers_fallback_timeout() -> None:
    """Verifies that hanging requests exceeding timeouts map to FALLBACK_TIMEOUT."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Socket read timed out")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT,
        LAYA_REQUEST_TIMEOUT_SECONDS=0.5,
        LAYA_TOTAL_DEADLINE_SECONDS=1.0,
        LAYA_MAX_RETRIES=1,
    )

    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        req = _make_request()
        envelope = await adapter.query_decision(req)

        assert envelope.status == DecisionStatus.FALLBACK_TIMEOUT


# ============================================================================
# 6. Health Probe Tests
# ============================================================================


@pytest.mark.asyncio
async def test_check_health_returns_true_on_200() -> None:
    """Verifies GET /health returns True when endpoint is healthy."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT)

    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        healthy = await adapter.check_health()
        assert healthy is True


@pytest.mark.asyncio
async def test_check_health_returns_false_on_error_or_unconfigured() -> None:
    """Verifies GET /health returns False on errors or unconfigured URL."""
    # 1. Unconfigured URL
    unconfigured_adapter = RemoteLayaDecisionAdapter(settings=Settings(LAYA_ENDPOINT_URL=None))
    assert await unconfigured_adapter.check_health() is False

    # 2. Server 500 error
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Error")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(LAYA_ENDPOINT_URL=SAMPLE_ENDPOINT)
    async with RemoteLayaDecisionAdapter(settings=settings, http_client=mock_client) as adapter:
        assert await adapter.check_health() is False
