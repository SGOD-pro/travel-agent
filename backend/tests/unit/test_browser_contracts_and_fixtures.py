"""Unit tests for browser domain contracts and offline FixtureBrowserAdapter.

Conforms to 0003 Slice 1:
- SUCCESS requires substantive content and a valid SHA256 content_hash.
- UNAVAILABLE, TIMED_OUT, and BLOCKED require an unavailable_reason and permit
  absent content; never fabricate excerpts, hashes, or locators.
- Enforces documented text limits, UUID fields, and allowed wait_until values.
- Distinguishes adapter results (BrowserRenderResult) from persisted worker output
  (BrowserWorkerOutput).
- Tests serialization and negative validation across all outcomes.
- Tests fixture execution with zero network and zero browser binaries.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from contracts.browser import (
    BrowserExtractionPort,
    BrowserRenderResult,
    BrowserRenderStatus,
    BrowserResourceUsage,
    BrowserTaskPayload,
    BrowserWaitUntil,
    BrowserWorkerOutput,
)
from contracts.evidence import HotelObservation
from contracts.money import Currency
from contracts.trips import TransportMode
from functions.browser.fixtures import (
    FixtureBrowserAdapter,
    compute_content_hash,
)

VALID_HASH = "a1b2c3d4e5f67890123456789abcdef0123456789abcdef0123456789abcdef0"
VALID_UUID = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")


# ============================================================================
# 1. BrowserTaskPayload Tests
# ============================================================================


def test_browser_task_payload_valid_defaults() -> None:
    """Verifies BrowserTaskPayload serialization and default values."""
    payload = BrowserTaskPayload(
        url="https://example.com/hotels/view?id=123",
        canonical_url="https://example.com/hotels/view?id=123",
        category="lodging",
        reason="NOSCRIPT_JAVASCRIPT_REQUIRED",
    )

    assert payload.wait_until == BrowserWaitUntil.NETWORKIDLE
    assert payload.timeout_seconds == 20
    assert payload.selector_target is None
    assert payload.placeholder_observation_id is None

    # Serialization roundtrip
    dumped = payload.model_dump(mode="json")
    assert dumped["wait_until"] == "networkidle"
    loaded = BrowserTaskPayload.model_validate(dumped)
    assert loaded == payload


@pytest.mark.parametrize("wait_until", list(BrowserWaitUntil))
def test_browser_task_payload_allowed_wait_until_values(wait_until: BrowserWaitUntil) -> None:
    """Verifies all allowed wait_until values are accepted."""
    payload = BrowserTaskPayload(
        url="https://example.com",
        canonical_url="https://example.com",
        category="transit",
        reason="DYNAMIC_METADATA_FETCH",
        wait_until=wait_until,
    )
    assert payload.wait_until == wait_until


def test_browser_task_payload_with_uuid_and_selector() -> None:
    """Verifies UUID parsing and optional selector limit."""
    payload = BrowserTaskPayload(
        url="https://example.com",
        canonical_url="https://example.com",
        category="lodging",
        reason="SPA_CONTAINER",
        selector_target="div.hotel-price",
        placeholder_observation_id=VALID_UUID,
    )
    assert payload.placeholder_observation_id == VALID_UUID
    assert payload.selector_target == "div.hotel-price"


def test_browser_task_payload_rejects_invalid_uuid() -> None:
    """Verifies that non-UUID string raises ValidationError."""
    with pytest.raises(ValidationError, match="placeholder_observation_id"):
        BrowserTaskPayload(
            url="https://example.com",
            canonical_url="https://example.com",
            category="lodging",
            reason="SPA_CONTAINER",
            placeholder_observation_id="not-a-valid-uuid",
        )


def test_browser_task_payload_rejects_invalid_wait_until() -> None:
    """Verifies that unapproved wait_until strategy raises ValidationError."""
    with pytest.raises(ValidationError, match="wait_until"):
        BrowserTaskPayload(
            url="https://example.com",
            canonical_url="https://example.com",
            category="lodging",
            reason="SPA_CONTAINER",
            wait_until="until_forever",
        )


@pytest.mark.parametrize("timeout", [0, -5, 61, 100])
def test_browser_task_payload_rejects_out_of_bounds_timeout(timeout: int) -> None:
    """Verifies timeout_seconds boundary enforcement [1, 60]."""
    with pytest.raises(ValidationError, match="timeout_seconds"):
        BrowserTaskPayload(
            url="https://example.com",
            canonical_url="https://example.com",
            category="lodging",
            reason="SPA_CONTAINER",
            timeout_seconds=timeout,
        )


def test_browser_task_payload_rejects_empty_strings() -> None:
    """Verifies min_length=1 enforcement on required string fields."""
    with pytest.raises(ValidationError):
        BrowserTaskPayload(
            url="",
            canonical_url="https://example.com",
            category="lodging",
            reason="SPA_CONTAINER",
        )
    with pytest.raises(ValidationError):
        BrowserTaskPayload(
            url="https://example.com",
            canonical_url="",
            category="lodging",
            reason="SPA_CONTAINER",
        )
    with pytest.raises(ValidationError):
        BrowserTaskPayload(
            url="https://example.com",
            canonical_url="https://example.com",
            category="",
            reason="SPA_CONTAINER",
        )
    with pytest.raises(ValidationError):
        BrowserTaskPayload(
            url="https://example.com",
            canonical_url="https://example.com",
            category="lodging",
            reason="",
        )


# ============================================================================
# 2. BrowserResourceUsage Tests
# ============================================================================


def test_browser_resource_usage_serialization() -> None:
    """Verifies BrowserResourceUsage serialization and validation."""
    usage = BrowserResourceUsage(duration_ms=1200, memory_mb=512)
    dumped = usage.model_dump(mode="json")
    assert dumped == {"duration_ms": 1200, "memory_mb": 512}

    usage_no_mem = BrowserResourceUsage(duration_ms=1200)
    assert usage_no_mem.memory_mb is None

    with pytest.raises(ValidationError):
        BrowserResourceUsage(duration_ms=-1)


# ============================================================================
# 3. BrowserRenderResult Serialization & Invariant Tests
# ============================================================================


def test_browser_render_result_success_serialization() -> None:
    """Verifies SUCCESS BrowserRenderResult serialization with observations and hash."""
    hotel = HotelObservation(
        property_name="Grand Palace",
        location_name="Kolkata",
        price_per_night=Decimal("4500.00"),
        currency=Currency.INR,
        provider="booking_scrapling",
        source_url="https://example.com/hotels/1",
        deep_link="https://example.com/hotels/1?ref=test",
    )
    result = BrowserRenderResult(
        status=BrowserRenderStatus.SUCCESS,
        canonical_url="https://example.com/hotels/1",
        effective_url="https://example.com/hotels/1",
        content_hash=VALID_HASH,
        excerpt_text="Grand Palace hotel description and amenities",
        excerpt_locator="section.overview",
        rendered_html_snippet="<html><body>Grand Palace</body></html>",
        render_duration_ms=1420,
        usage=BrowserResourceUsage(duration_ms=1420, memory_mb=512),
        parsed_lodging_observations=[hotel],
    )

    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "SUCCESS"
    assert dumped["content_hash"] == VALID_HASH
    assert len(dumped["parsed_lodging_observations"]) == 1

    loaded = BrowserRenderResult.model_validate(dumped)
    assert loaded.status == BrowserRenderStatus.SUCCESS
    assert loaded.content_hash == VALID_HASH
    assert loaded.parsed_lodging_observations[0].property_name == "Grand Palace"


@pytest.mark.parametrize(
    "status,reason",
    [
        (BrowserRenderStatus.UNAVAILABLE, "Target server returned HTTP 503 Service Unavailable"),
        (BrowserRenderStatus.TIMED_OUT, "Navigation timed out after 20 seconds waiting for networkidle"),
        (BrowserRenderStatus.BLOCKED, "Cloudflare Turnstile challenge intercepted; automation denied"),
    ],
)
def test_browser_render_result_error_outcomes_serialization(
    status: BrowserRenderStatus, reason: str
) -> None:
    """Verifies UNAVAILABLE, TIMED_OUT, and BLOCKED serialization with required reason and absent content."""
    result = BrowserRenderResult(
        status=status,
        canonical_url="https://example.com/page",
        effective_url="https://example.com/page",
        content_hash=None,
        excerpt_text=None,
        excerpt_locator=None,
        rendered_html_snippet=None,
        render_duration_ms=5000,
        unavailable_reason=reason,
        usage=BrowserResourceUsage(duration_ms=5000, memory_mb=256),
    )

    dumped = result.model_dump(mode="json")
    assert dumped["status"] == status.value
    assert dumped["unavailable_reason"] == reason
    assert dumped["content_hash"] is None
    assert dumped["excerpt_text"] is None
    assert dumped["excerpt_locator"] is None

    loaded = BrowserRenderResult.model_validate(dumped)
    assert loaded.status == status
    assert loaded.unavailable_reason == reason
    assert loaded.content_hash is None


# ============================================================================
# 4. BrowserRenderResult Negative Validation Tests
# ============================================================================


def test_success_requires_content_hash() -> None:
    """Verifies SUCCESS status fails without content_hash."""
    with pytest.raises(ValidationError, match="SUCCESS status requires a valid SHA256 content_hash"):
        BrowserRenderResult(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            content_hash=None,
            excerpt_text="Substantive text content",
            excerpt_locator="main",
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


@pytest.mark.parametrize(
    "bad_hash",
    [
        "too_short",
        "a" * 63,
        "a" * 65,
        "z" * 64,  # 'z' is not a hex character
        "1234567890abcdef" * 3 + "g",  # invalid hex character
    ],
)
def test_success_rejects_invalid_content_hash(bad_hash: str) -> None:
    """Verifies content_hash must be a valid 64-character hex digest."""
    with pytest.raises(ValidationError, match="content_hash"):
        BrowserRenderResult(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            content_hash=bad_hash,
            excerpt_text="Substantive text content",
            excerpt_locator="main",
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


@pytest.mark.parametrize("empty_excerpt", [None, "", "   ", "\n\t  "])
def test_success_requires_substantive_excerpt_text(empty_excerpt: str | None) -> None:
    """Verifies SUCCESS status rejects empty or whitespace-only excerpt_text."""
    with pytest.raises(ValidationError, match="SUCCESS status requires substantive non-empty excerpt_text"):
        BrowserRenderResult(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            content_hash=VALID_HASH,
            excerpt_text=empty_excerpt,
            excerpt_locator="main",
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


def test_success_rejects_excerpt_exceeding_1000_chars() -> None:
    """Verifies max_length=1000 enforcement on excerpt_text."""
    oversized_text = "a" * 1001
    with pytest.raises(ValidationError, match="excerpt_text"):
        BrowserRenderResult(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            content_hash=VALID_HASH,
            excerpt_text=oversized_text,
            excerpt_locator="main",
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


@pytest.mark.parametrize("empty_locator", [None, "", "   "])
def test_success_requires_non_empty_excerpt_locator(empty_locator: str | None) -> None:
    """Verifies SUCCESS status rejects missing or empty excerpt_locator."""
    with pytest.raises(ValidationError, match="excerpt_locator"):
        BrowserRenderResult(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            content_hash=VALID_HASH,
            excerpt_text="Valid excerpt text",
            excerpt_locator=empty_locator,
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


def test_success_rejects_unavailable_reason() -> None:
    """Verifies SUCCESS status cannot have an unavailable_reason set."""
    with pytest.raises(ValidationError, match="SUCCESS status must not have an unavailable_reason"):
        BrowserRenderResult(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            content_hash=VALID_HASH,
            excerpt_text="Valid excerpt text",
            excerpt_locator="main",
            unavailable_reason="Something failed",
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


@pytest.mark.parametrize(
    "status",
    [BrowserRenderStatus.UNAVAILABLE, BrowserRenderStatus.TIMED_OUT, BrowserRenderStatus.BLOCKED],
)
@pytest.mark.parametrize("empty_reason", [None, "", "   "])
def test_error_outcomes_require_descriptive_reason(
    status: BrowserRenderStatus, empty_reason: str | None
) -> None:
    """Verifies error outcomes fail without a non-empty unavailable_reason."""
    with pytest.raises(ValidationError, match="requires a descriptive non-empty unavailable_reason"):
        BrowserRenderResult(
            status=status,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            unavailable_reason=empty_reason,
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


@pytest.mark.parametrize(
    "status",
    [BrowserRenderStatus.UNAVAILABLE, BrowserRenderStatus.TIMED_OUT, BrowserRenderStatus.BLOCKED],
)
def test_error_outcomes_reject_fabricated_content_hash(status: BrowserRenderStatus) -> None:
    """Verifies error outcomes forbid fabricated content_hash."""
    with pytest.raises(
        ValidationError,
        match="permits absent content and must never fabricate excerpts, hashes, or locators",
    ):
        BrowserRenderResult(
            status=status,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            content_hash=VALID_HASH,  # Fabricated hash must be rejected
            unavailable_reason="Page failed",
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


@pytest.mark.parametrize(
    "status",
    [BrowserRenderStatus.UNAVAILABLE, BrowserRenderStatus.TIMED_OUT, BrowserRenderStatus.BLOCKED],
)
def test_error_outcomes_reject_fabricated_excerpt_text(status: BrowserRenderStatus) -> None:
    """Verifies error outcomes forbid fabricated excerpt_text."""
    with pytest.raises(
        ValidationError,
        match="permits absent content and must never fabricate excerpts, hashes, or locators",
    ):
        BrowserRenderResult(
            status=status,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            excerpt_text="Fabricated excerpt for failed page",
            unavailable_reason="Page failed",
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


@pytest.mark.parametrize(
    "status",
    [BrowserRenderStatus.UNAVAILABLE, BrowserRenderStatus.TIMED_OUT, BrowserRenderStatus.BLOCKED],
)
def test_error_outcomes_reject_fabricated_excerpt_locator(status: BrowserRenderStatus) -> None:
    """Verifies error outcomes forbid fabricated excerpt_locator."""
    with pytest.raises(
        ValidationError,
        match="permits absent content and must never fabricate excerpts, hashes, or locators",
    ):
        BrowserRenderResult(
            status=status,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            excerpt_locator="main#fake-locator",
            unavailable_reason="Page failed",
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


# ============================================================================
# 5. BrowserWorkerOutput Tests
# ============================================================================


def test_browser_worker_output_success_valid() -> None:
    """Verifies BrowserWorkerOutput SUCCESS validates observation_id UUID and content_hash."""
    output = BrowserWorkerOutput(
        status=BrowserRenderStatus.SUCCESS,
        canonical_url="https://example.com/resort",
        effective_url="https://example.com/resort",
        observation_id=VALID_UUID,
        content_hash=VALID_HASH,
        rendered_html_bytes=48200,
        render_duration_ms=1420,
        parsed_observations=[{"property_name": "Grand Palace", "price": "4500.00"}],
        usage=BrowserResourceUsage(duration_ms=1420, memory_mb=512),
    )
    dumped = output.model_dump(mode="json")
    assert dumped["observation_id"] == str(VALID_UUID)
    assert dumped["content_hash"] == VALID_HASH

    loaded = BrowserWorkerOutput.model_validate(dumped)
    assert loaded.observation_id == VALID_UUID


def test_browser_worker_output_success_requires_observation_id() -> None:
    """Verifies SUCCESS output requires observation_id UUID."""
    with pytest.raises(ValidationError, match="requires a persisted observation_id UUID"):
        BrowserWorkerOutput(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            observation_id=None,
            content_hash=VALID_HASH,
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


def test_browser_worker_output_success_requires_content_hash() -> None:
    """Verifies SUCCESS output requires content_hash."""
    with pytest.raises(ValidationError, match="requires a valid SHA256 content_hash"):
        BrowserWorkerOutput(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            observation_id=VALID_UUID,
            content_hash=None,
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


@pytest.mark.parametrize(
    "status",
    [BrowserRenderStatus.UNAVAILABLE, BrowserRenderStatus.TIMED_OUT, BrowserRenderStatus.BLOCKED],
)
def test_browser_worker_output_error_requires_unavailable_sources(status: BrowserRenderStatus) -> None:
    """Verifies error worker output requires unavailable_sources records."""
    with pytest.raises(ValidationError, match="requires non-empty unavailable_sources records"):
        BrowserWorkerOutput(
            status=status,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            observation_id=None,
            content_hash=None,
            unavailable_sources=[],  # Must not be empty
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


@pytest.mark.parametrize(
    "status",
    [BrowserRenderStatus.UNAVAILABLE, BrowserRenderStatus.TIMED_OUT, BrowserRenderStatus.BLOCKED],
)
def test_browser_worker_output_error_rejects_fabricated_identifiers(
    status: BrowserRenderStatus,
) -> None:
    """Verifies error worker output rejects observation_id or content_hash."""
    with pytest.raises(ValidationError, match="must not fabricate observation_id or content_hash"):
        BrowserWorkerOutput(
            status=status,
            canonical_url="https://example.com",
            effective_url="https://example.com",
            observation_id=VALID_UUID,  # Fabricated ID
            content_hash=None,
            unavailable_sources=[{"url": "https://example.com", "reason": "Timed out"}],
            render_duration_ms=100,
            usage=BrowserResourceUsage(duration_ms=100),
        )


# ============================================================================
# 6. FixtureBrowserAdapter Execution Tests (Zero Network / Zero Binary)
# ============================================================================


@pytest.mark.asyncio
async def test_fixture_browser_adapter_implements_port() -> None:
    """Verifies FixtureBrowserAdapter is an instance of BrowserExtractionPort."""
    adapter = FixtureBrowserAdapter()
    assert isinstance(adapter, BrowserExtractionPort)
    assert adapter.is_test_fixture is True


@pytest.mark.asyncio
async def test_fixture_browser_adapter_lodging_default_execution() -> None:
    """Verifies lodging default execution produces valid HotelObservation with valid SHA256."""
    adapter = FixtureBrowserAdapter()
    payload = BrowserTaskPayload(
        url="https://dynamic.hotelportal.com/resorts/kolkata",
        canonical_url="https://dynamic.hotelportal.com/resorts/kolkata",
        category="lodging",
        reason="NOSCRIPT_JAVASCRIPT_REQUIRED",
    )

    result = await adapter.render_page(payload)

    assert result.status == BrowserRenderStatus.SUCCESS
    assert result.canonical_url == payload.canonical_url
    assert result.effective_url == payload.url
    assert len(result.content_hash or "") == 64
    assert result.content_hash == compute_content_hash(result.excerpt_text or "")
    assert result.excerpt_text is not None and len(result.excerpt_text) > 0
    assert result.excerpt_locator == "main#fixture-content"
    assert len(result.parsed_lodging_observations) == 1
    assert result.parsed_lodging_observations[0].property_name == "Fixture Heritage Palace"
    assert result.parsed_lodging_observations[0].currency == Currency.INR
    assert adapter.call_count == 1
    assert adapter.recorded_payloads[0] == payload


@pytest.mark.asyncio
async def test_fixture_browser_adapter_transit_default_execution() -> None:
    """Verifies transit default execution produces valid FareObservation with valid SHA256."""
    adapter = FixtureBrowserAdapter()
    payload = BrowserTaskPayload(
        url="https://dynamic.transitportal.com/routes/metro",
        canonical_url="https://dynamic.transitportal.com/routes/metro",
        category="transit",
        reason="SPA_CONTAINER",
    )

    result = await adapter.render_page(payload)

    assert result.status == BrowserRenderStatus.SUCCESS
    assert len(result.parsed_transit_observations) == 1
    transit = result.parsed_transit_observations[0]
    assert transit.operator_name == "Kolkata Metro Rail"
    assert transit.mode == TransportMode.TRAIN
    assert transit.price == Decimal("20.00")
    assert result.content_hash == compute_content_hash(result.excerpt_text or "")


@pytest.mark.asyncio
async def test_fixture_browser_adapter_canned_lookup_by_url_and_normalization() -> None:
    """Verifies canned fixture matching across canonical URL, raw URL, and stripped tracking params."""
    target_canonical = "https://dynamic.portal.com/offer"
    canned_result = FixtureBrowserAdapter.create_success_result(
        canonical_url=target_canonical,
        excerpt_text="Special festival promotion details",
        excerpt_locator="div.promo",
    )
    adapter = FixtureBrowserAdapter(fixtures_by_url={target_canonical: canned_result})

    # Call with tracking parameter in URL; should match canonical or normalized URL
    payload = BrowserTaskPayload(
        url="https://dynamic.portal.com/offer?utm_source=adwords&utm_campaign=winter",
        canonical_url=target_canonical,
        category="lodging",
        reason="DYNAMIC_JS",
    )

    result = await adapter.render_page(payload)
    assert result == canned_result
    assert result.excerpt_text == "Special festival promotion details"


@pytest.mark.asyncio
async def test_fixture_browser_adapter_canned_timed_out_outcome() -> None:
    """Verifies TIMED_OUT outcome execution retains reason and avoids fabricated content."""
    canonical = "https://hanging-portal.com/slow"
    timed_out_result = FixtureBrowserAdapter.create_unavailable_result(
        canonical_url=canonical,
        status=BrowserRenderStatus.TIMED_OUT,
        reason="Navigation timeout of 20 seconds exceeded while awaiting networkidle",
        render_duration_ms=20000,
    )
    adapter = FixtureBrowserAdapter(fixtures_by_url={canonical: timed_out_result})

    payload = BrowserTaskPayload(
        url=canonical,
        canonical_url=canonical,
        category="lodging",
        reason="DYNAMIC_JS",
    )

    result = await adapter.render_page(payload)
    assert result.status == BrowserRenderStatus.TIMED_OUT
    assert result.unavailable_reason == "Navigation timeout of 20 seconds exceeded while awaiting networkidle"
    assert result.content_hash is None
    assert result.excerpt_text is None
    assert result.excerpt_locator is None


@pytest.mark.asyncio
async def test_fixture_browser_adapter_canned_blocked_outcome() -> None:
    """Verifies BLOCKED outcome execution retains reason and avoids fabricated content."""
    canonical = "https://protected-site.com/challenge"
    blocked_result = FixtureBrowserAdapter.create_unavailable_result(
        canonical_url=canonical,
        status=BrowserRenderStatus.BLOCKED,
        reason="Cloudflare Turnstile verification challenge blocked automated browser render",
        render_duration_ms=850,
    )
    adapter = FixtureBrowserAdapter(fixtures_by_url={canonical: blocked_result})

    payload = BrowserTaskPayload(
        url=canonical,
        canonical_url=canonical,
        category="lodging",
        reason="DYNAMIC_JS",
    )

    result = await adapter.render_page(payload)
    assert result.status == BrowserRenderStatus.BLOCKED
    assert "Cloudflare Turnstile" in (result.unavailable_reason or "")
    assert result.content_hash is None
    assert result.excerpt_text is None


@pytest.mark.asyncio
async def test_fixture_execution_makes_zero_network_or_binary_calls() -> None:
    """Guarantees offline execution without socket creation or subprocess calls."""
    adapter = FixtureBrowserAdapter()
    payload = BrowserTaskPayload(
        url="https://offline.example.com",
        canonical_url="https://offline.example.com",
        category="lodging",
        reason="DYNAMIC_JS",
    )

    # Patch socket and subprocess to guarantee zero live calls
    with patch("socket.socket", side_effect=RuntimeError("Network access forbidden in offline tests")):
        with patch("subprocess.Popen", side_effect=RuntimeError("Subprocess forbidden")):
            result = await adapter.render_page(payload)

    assert result.status == BrowserRenderStatus.SUCCESS
    assert result.canonical_url == payload.canonical_url
    assert result.content_hash == compute_content_hash(result.excerpt_text or "")
