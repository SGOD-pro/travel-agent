"""Unit tests for SerpAPI adapter conforming to spec 0002 AC 1.

Verifies organic and place result normalization, latency measurement in milliseconds,
minor unit spend tracking, timeout handling, rate limiting (HTTP 429), and offline test isolation.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from functions.evidence_collector.serpapi_adapter import (
    DEFAULT_SERPAPI_COST_MINOR,
    SerpApiAdapter,
    SerpApiAdapterError,
    SerpApiRateLimitError,
    SerpApiTimeoutError,
)

SAMPLE_SERPAPI_RESPONSE = {
    "search_metadata": {
        "id": "search_12345",
        "status": "Success",
        "total_time_taken": 0.42,
    },
    "organic_results": [
        {
            "position": 1,
            "title": "Kolkata Traffic Advisory 2026",
            "link": "https://kolkatatrafficpolice.gov.in/advisory2026?utm_source=twitter",
            "snippet": "Central Avenue closed for heavy vehicles from 16:00 to 04:00 during Puja.",
            "displayed_link": "https://kolkatatrafficpolice.gov.in",
        },
        {
            "position": 2,
            "title": "Durga Puja 2026 Route Map",
            "link": "https://timesofindia.indiatimes.com/city/kolkata/puja-route-map",
            "snippet": "Detailed police traffic plan and road restrictions across South and North Kolkata.",
            "displayed_link": "https://timesofindia.indiatimes.com",
        },
    ],
    "local_results": {
        "places": [
            {
                "position": 1,
                "title": "Dakshineswar Kali Temple",
                "place_id": "ChIJ_3_fake_id_1",
                "address": "Dakshineswar, Kolkata, West Bengal 700076",
                "rating": 4.7,
                "reviews": 125000,
                "gps_coordinates": {"latitude": 22.6534, "longitude": 88.3574},
                "link": "https://dakshineswartemple.org",
            }
        ]
    },
}


class TestSerpApiAdapter:
    """Unit test suite for SerpApiAdapter."""

    @pytest.mark.asyncio
    async def test_search_organic_and_places_normalized(self) -> None:
        """Verifies that SerpAPI returns normalized RawObservation items for organic and places (AC 1)."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/search"
            assert request.url.params["engine"] == "google"
            assert request.url.params["q"] == "Kolkata Durga Puja traffic"
            assert request.url.params["api_key"] == "test_key_secret"
            return httpx.Response(200, json=SAMPLE_SERPAPI_RESPONSE)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        adapter = SerpApiAdapter(
            api_key="test_key_secret",
            base_url="https://mock.serpapi.test",
            client=client,
        )

        results = await adapter.search("Kolkata Durga Puja traffic", limit=5)

        # Expected: 2 organic results + 1 place result = 3 observations
        assert len(results) == 3

        # Check first organic observation
        obs1 = next(r for r in results if r.excerpt_locator == "serpapi:organic[0].snippet")
        assert obs1.publisher == "kolkatatrafficpolice.gov.in"
        assert (
            obs1.canonical_url == "https://kolkatatrafficpolice.gov.in/advisory2026"
        )  # stripped utm_source
        assert "Central Avenue closed" in obs1.excerpt_text
        assert obs1.extraction_method == "SERPAPI_SEARCH"
        assert obs1.excerpt_locator == "serpapi:organic[0].snippet"
        assert obs1.artifact_ref is not None
        assert obs1.artifact_ref["position"] == 1

        # Check place observation
        place_obs = next(r for r in results if r.excerpt_locator == "serpapi:places[0]")
        assert "Dakshineswar Kali Temple" in place_obs.excerpt_text
        assert "Rating: 4.7" in place_obs.excerpt_text
        assert place_obs.extraction_method == "SERPAPI_SEARCH"
        assert place_obs.excerpt_locator == "serpapi:places[0]"
        assert place_obs.artifact_ref is not None
        assert place_obs.artifact_ref["rating"] == 4.7
        assert place_obs.artifact_ref["gps"]["latitude"] == 22.6534

        # Check call stats and minor unit spend
        assert adapter.call_count == 1
        assert adapter.total_cost_minor == DEFAULT_SERPAPI_COST_MINOR
        assert adapter.last_latency_ms > 0.0

        await client.aclose()

    @pytest.mark.asyncio
    async def test_latency_tracked_in_milliseconds(self) -> None:
        """Verifies that response latency is recorded in milliseconds (AC 1)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"organic_results": []})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        adapter = SerpApiAdapter(api_key="key", client=client)
        await adapter.search("query")

        assert adapter.last_latency_ms >= 0.0
        assert isinstance(adapter.last_latency_ms, float)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_minor_unit_cost_accumulates_per_query(self) -> None:
        """Verifies that minor unit cost increments by configured amount per query (AC 1)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"organic_results": []})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        cost_per_query = 75
        adapter = SerpApiAdapter(
            api_key="key",
            cost_per_query_minor=cost_per_query,
            client=client,
        )

        await adapter.search("query 1")
        await adapter.search("query 2")

        assert adapter.call_count == 2
        assert adapter.total_cost_minor == 150
        await client.aclose()

    @pytest.mark.asyncio
    async def test_timeout_raises_serpapi_timeout_error(self) -> None:
        """Verifies that provider timeout raises SerpApiTimeoutError cleanly (AC 1)."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("Connection timed out after 20s")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        adapter = SerpApiAdapter(api_key="key", timeout_seconds=5.0, client=client)

        with pytest.raises(SerpApiTimeoutError) as exc_info:
            await adapter.search("timeout query")

        assert "timed out" in str(exc_info.value).lower()
        await client.aclose()

    @pytest.mark.asyncio
    async def test_rate_limit_429_raises_serpapi_rate_limit_error(self) -> None:
        """Verifies that HTTP 429 raises SerpApiRateLimitError (AC 1)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="Too Many Requests")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        adapter = SerpApiAdapter(api_key="key", client=client)

        with pytest.raises(SerpApiRateLimitError) as exc_info:
            await adapter.search("rate limited query")

        assert "rate limit exceeded" in str(exc_info.value).lower()
        await client.aclose()

    @pytest.mark.asyncio
    async def test_missing_api_key_raises_error_without_network_call(self) -> None:
        """Verifies that attempting search without an API key fails before network dispatch."""
        adapter = SerpApiAdapter(api_key=None)
        # Clear settings fallback if present
        adapter._api_key = None

        with pytest.raises(SerpApiAdapterError) as exc_info:
            await adapter.search("test")

        assert "SERPAPI_API_KEY is not configured" in str(exc_info.value)

    def test_safe_url_for_logging_redacts_api_key(self) -> None:
        """Verifies that _safe_url_for_logging redacts api_key from query parameters."""
        from functions.evidence_collector.serpapi_adapter import _safe_url_for_logging

        raw_url = "https://serpapi.com/search?engine=google&q=kolkata&api_key=secret_12345&num=5"
        masked = _safe_url_for_logging(raw_url)
        assert "secret_12345" not in masked
        assert "api_key=%5BREDACTED%5D" in masked or "api_key=[REDACTED]" in masked

    @pytest.mark.asyncio
    async def test_http_error_does_not_leak_api_key_in_exception(self) -> None:
        """Verifies that error responses containing the api_key redact it in exception messages."""
        secret_key = "super_secret_serp_key"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=f"Internal Server Error for key {secret_key}")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        adapter = SerpApiAdapter(api_key=secret_key, client=client)

        with pytest.raises(SerpApiAdapterError) as exc_info:
            await adapter.search("test query")

        err_msg = str(exc_info.value)
        assert secret_key not in err_msg
        assert "[REDACTED]" in err_msg
        await client.aclose()

    @pytest.mark.asyncio
    async def test_extra_params_cannot_override_api_key_or_core_params(self) -> None:
        """Verifies that caller extra_params cannot override api_key or core query params."""
        captured_params: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_params.update(request.url.params)
            return httpx.Response(200, json={"organic_results": []})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        adapter = SerpApiAdapter(api_key="real_configured_key", client=client)

        await adapter.search(
            "test query",
            limit=3,
            extra_params={
                "api_key": "injected_override_key",
                "q": "injected_query",
                "num": 99,
                "hl": "en",
                "gl": "in",
            },
        )

        assert captured_params["api_key"] == "real_configured_key"
        assert captured_params["q"] == "test query"
        assert captured_params["num"] == "3"
        assert captured_params["hl"] == "en"
        assert captured_params["gl"] == "in"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_transport_error_redacts_credentials_in_logs_and_tracebacks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verifies transport errors do not leak request credentials in formatted logs or tracebacks."""
        fake_secret = "FAKE_SECRET_TRANSPORT_KEY_12345"

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                f"Failed to connect to host on {request.url}", request=request
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        adapter = SerpApiAdapter(api_key=fake_secret, client=client)

        with caplog.at_level("DEBUG"):
            with pytest.raises(SerpApiAdapterError) as exc_info:
                await adapter.search("test connectivity")

        # 1. Secret must not appear in raised exception message
        err_str = str(exc_info.value)
        assert fake_secret not in err_str
        assert "ConnectError" in err_str or "Failed to connect" in err_str

        # 2. Secret must not appear in caplog formatted records
        assert fake_secret not in caplog.text
        assert "[REDACTED]" in caplog.text

        # 3. Subsequent caller traceback formatting must not leak secret
        import traceback

        formatted_tb = "".join(traceback.format_exception(exc_info.value))
        assert fake_secret not in formatted_tb
        assert "[REDACTED]" in formatted_tb
        await client.aclose()

    @pytest.mark.asyncio
    async def test_timeout_error_redacts_credentials_in_logs_and_tracebacks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verifies timeout errors do not leak request credentials in logs or chained exceptions."""
        fake_secret = "FAKE_SECRET_TIMEOUT_KEY_67890"

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(
                f"Timed out waiting for response from {request.url}", request=request
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        adapter = SerpApiAdapter(api_key=fake_secret, timeout_seconds=2.0, client=client)

        with caplog.at_level("DEBUG"):
            with pytest.raises(SerpApiTimeoutError) as exc_info:
                await adapter.search("timeout query")

        assert fake_secret not in str(exc_info.value)
        assert fake_secret not in caplog.text
        assert "timed out after" in str(exc_info.value).lower()

        import traceback

        formatted_tb = "".join(traceback.format_exception(exc_info.value))
        assert fake_secret not in formatted_tb
        await client.aclose()

    @pytest.mark.asyncio
    async def test_unexpected_error_redacts_credentials_in_logs_and_tracebacks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verifies unexpected errors sanitize credentials before logger.exception outputs traceback."""
        fake_secret = "FAKE_SECRET_UNEXPECTED_KEY_99999"

        def handler(request: httpx.Request) -> httpx.Response:
            raise RuntimeError(f"Unexpected socket crash with key {fake_secret}")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        adapter = SerpApiAdapter(api_key=fake_secret, client=client)

        with caplog.at_level("DEBUG"):
            with pytest.raises(SerpApiAdapterError) as exc_info:
                await adapter.search("crash query")

        assert fake_secret not in str(exc_info.value)
        assert "[REDACTED]" in str(exc_info.value)
        assert fake_secret not in caplog.text
        assert "[REDACTED]" in caplog.text

        import traceback

        formatted_tb = "".join(traceback.format_exception(exc_info.value))
        assert fake_secret not in formatted_tb
        assert "[REDACTED]" in formatted_tb
        await client.aclose()

    @pytest.mark.asyncio
    async def test_adapter_engine_selection_enforces_purpose_over_conflicting_engine(self) -> None:
        """Verifies explicit purpose dictates permitted engine at adapter boundary."""
        captured_requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(dict(request.url.params))
            return httpx.Response(200, json={"organic_results": []})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        adapter = SerpApiAdapter(api_key="test_key", client=client)

        # 1. Advisory with conflicting engine="google_maps" -> normalized to "google"
        await adapter.search(
            "Bengaluru to Mysuru advisory",
            extra_params={"purpose": "advisory", "engine": "google_maps"},
        )
        assert captured_requests[-1]["engine"] == "google"

        # 2. Venue discovery with conflicting engine="google" -> normalized to "google_maps"
        await adapter.search(
            "Mysuru Palace",
            extra_params={"purpose": "attractions", "engine": "google"},
        )
        assert captured_requests[-1]["engine"] == "google_maps"

        # 3. Dining with unsupported engine="bing" -> normalized to "google_maps"
        await adapter.search(
            "Mysuru Dosa",
            extra_params={"purpose": "dining", "engine": "bing"},
        )
        assert captured_requests[-1]["engine"] == "google_maps"
        await client.aclose()

    def test_sanitize_exception_handles_cyclic_exception_chains(self) -> None:
        """Verifies _sanitize_exception breaks cyclic __cause__ and __context__ references."""
        from functions.evidence_collector.serpapi_adapter import _sanitize_exception

        exc1 = RuntimeError("error 1 with api_key=secret_val_123")
        exc2 = ValueError("error 2 with api_key=secret_val_456")

        # Create cycle
        exc1.__cause__ = exc2
        exc2.__cause__ = exc1

        # Must not raise RecursionError
        _sanitize_exception(exc1)
        assert "secret_val_123" not in str(exc1.args)
        assert "secret_val_456" not in str(exc2.args)
        assert "[REDACTED]" in str(exc1.args)
        assert "[REDACTED]" in str(exc2.args)


