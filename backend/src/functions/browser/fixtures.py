"""Test-only fixture adapter for browser rendering conforming to spec 0003 Slice 1.

This adapter provides canned browser rendering and dynamic entity extraction outcomes
for offline tests. It executes entirely in memory with zero external network access
and zero headless browser binary dependencies.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from contracts.browser import (
    BrowserExtractionPort,
    BrowserRenderResult,
    BrowserRenderStatus,
    BrowserResourceUsage,
    BrowserTaskPayload,
)
from contracts.evidence import EvidenceClass, FareObservation, HotelObservation
from contracts.money import Currency
from contracts.trips import TransportMode

_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "utm_id",
        "fbclid",
        "gclid",
        "msclkid",
        "ttclid",
        "twclid",
    }
)


def compute_content_hash(text: str) -> str:
    """Computes a SHA256 lowercase hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


def normalize_url(url: str) -> str:
    """Normalizes a URL by lowercasing scheme/host and stripping tracking query parameters."""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    query_items = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(query_items)

    path = parsed.path
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")

    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


class FixtureBrowserAdapter(BrowserExtractionPort):
    """Test-only fixture browser adapter returning canned render results.

    Strictly test-only: must never be injected as a production browser driver.
    """

    def __init__(
        self,
        fixtures_by_url: dict[str, BrowserRenderResult] | None = None,
        default_result: BrowserRenderResult | None = None,
    ) -> None:
        self.is_test_fixture = True
        self._fixtures = dict(fixtures_by_url) if fixtures_by_url is not None else {}
        self._default_result = default_result
        self.call_count = 0
        self.recorded_payloads: list[BrowserTaskPayload] = []

    @classmethod
    def create_success_result(
        cls,
        *,
        canonical_url: str,
        effective_url: str | None = None,
        excerpt_text: str | None = None,
        excerpt_locator: str = "main#rendered-content",
        rendered_html_snippet: str | None = "<html><body><main id='rendered-content'>Dynamic Content</main></body></html>",
        render_duration_ms: int = 420,
        usage: BrowserResourceUsage | None = None,
        parsed_lodging_observations: list[HotelObservation] | None = None,
        parsed_transit_observations: list[FareObservation] | None = None,
    ) -> BrowserRenderResult:
        """Helper to create a valid SUCCESS BrowserRenderResult with a true SHA256 hash."""
        text = excerpt_text or f"[FIXTURE_RENDERED] Dynamic content rendered for {canonical_url}"
        computed_hash = compute_content_hash(text)
        res_usage = usage or BrowserResourceUsage(duration_ms=render_duration_ms, memory_mb=256)
        return BrowserRenderResult(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url=canonical_url,
            effective_url=effective_url or canonical_url,
            content_hash=computed_hash,
            excerpt_text=text,
            excerpt_locator=excerpt_locator,
            rendered_html_snippet=rendered_html_snippet,
            render_duration_ms=render_duration_ms,
            unavailable_reason=None,
            usage=res_usage,
            parsed_lodging_observations=parsed_lodging_observations or [],
            parsed_transit_observations=parsed_transit_observations or [],
        )

    @classmethod
    def create_unavailable_result(
        cls,
        *,
        canonical_url: str,
        reason: str,
        status: BrowserRenderStatus = BrowserRenderStatus.UNAVAILABLE,
        effective_url: str | None = None,
        render_duration_ms: int = 150,
        rendered_html_snippet: str | None = None,
        usage: BrowserResourceUsage | None = None,
    ) -> BrowserRenderResult:
        """Helper to create an error/degraded outcome with reason and zero fabricated content."""
        if status == BrowserRenderStatus.SUCCESS:
            raise ValueError("status must be UNAVAILABLE, TIMED_OUT, or BLOCKED")
        res_usage = usage or BrowserResourceUsage(duration_ms=render_duration_ms, memory_mb=256)
        return BrowserRenderResult(
            status=status,
            canonical_url=canonical_url,
            effective_url=effective_url or canonical_url,
            content_hash=None,
            excerpt_text=None,
            excerpt_locator=None,
            rendered_html_snippet=rendered_html_snippet,
            render_duration_ms=render_duration_ms,
            unavailable_reason=reason,
            usage=res_usage,
            parsed_lodging_observations=[],
            parsed_transit_observations=[],
        )

    def _generate_default_success_result(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
        """Generates a category-aware default SUCCESS outcome."""
        now = datetime.now(UTC)
        lodging_obs: list[HotelObservation] = []
        transit_obs: list[FareObservation] = []

        if payload.category == "lodging":
            lodging_obs.append(
                HotelObservation(
                    property_name="Fixture Heritage Palace",
                    location_name="Kolkata",
                    price_per_night=Decimal("5200.00"),
                    currency=Currency.INR,
                    rating=4.5,
                    review_count=120,
                    evidence_class=EvidenceClass.INDICATIVE_SEARCH,
                    provider="fixture_browser_candidate",
                    source_url=payload.url,
                    deep_link=payload.url,
                    observed_at=now,
                )
            )
        elif payload.category == "transit":
            transit_obs.append(
                FareObservation(
                    origin="Howrah Junction",
                    destination="Esplanade",
                    operator_name="Kolkata Metro Rail",
                    departure_time="09:00",
                    arrival_time="09:15",
                    price=Decimal("20.00"),
                    currency=Currency.INR,
                    mode=TransportMode.TRAIN,
                    evidence_class=EvidenceClass.INDICATIVE_SEARCH,
                    provider="fixture_browser_candidate",
                    source_url=payload.url,
                    deep_link=payload.url,
                    observed_at=now,
                )
            )

        text = f"[FIXTURE_RENDERED] Dynamic DOM rendered for {payload.canonical_url} ({payload.category})"
        computed_hash = compute_content_hash(text)
        return BrowserRenderResult(
            status=BrowserRenderStatus.SUCCESS,
            canonical_url=payload.canonical_url,
            effective_url=payload.url,
            content_hash=computed_hash,
            excerpt_text=text,
            excerpt_locator="main#fixture-content",
            rendered_html_snippet="<html><body><main id='fixture-content'>Dynamic Content</main></body></html>",
            render_duration_ms=350,
            unavailable_reason=None,
            usage=BrowserResourceUsage(duration_ms=350, memory_mb=256),
            parsed_lodging_observations=lodging_obs,
            parsed_transit_observations=transit_obs,
        )

    async def render_page(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
        """Renders dynamic page content from canned fixtures without network or browser binary."""
        self.call_count += 1
        self.recorded_payloads.append(payload)

        # Look up by canonical URL or raw URL or normalized URL
        norm_url = normalize_url(payload.url)
        norm_canonical = normalize_url(payload.canonical_url)

        for candidate in (payload.canonical_url, payload.url, norm_canonical, norm_url):
            if candidate in self._fixtures:
                return self._fixtures[candidate]

        if self._default_result is not None:
            return self._default_result

        return self._generate_default_success_result(payload)
