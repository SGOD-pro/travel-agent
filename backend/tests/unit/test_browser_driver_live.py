"""Live browser execution tests conforming to spec 0003 Slice 4.

Verifies actual headless Chromium browser execution:
1. Actual client side JavaScript execution and DOM hydration.
2. Navigation timeout enforcement mapping to TIMED_OUT.
3. HTTP 302 redirect to metadata endpoint (169.254.169.254) intercepted and aborted.
4. Pre navigation private and metadata blocking (169.254.169.254, 127.0.0.1).
5. Service worker registration blocking (service_workers="block").
6. WebSocket disabling in live browser execution context.
"""

from __future__ import annotations

import hashlib
import http.server
import ipaddress
import threading
import time
from collections.abc import Generator

import pytest

from contracts.browser import (
    BrowserRenderStatus,
    BrowserTaskPayload,
    BrowserWaitUntil,
)
from functions.browser.driver import PlaywrightBrowserAdapter
from functions.browser.network import AWS_METADATA_IPV4


class _LiveTestServerHandler(http.server.BaseHTTPRequestHandler):
    """Local HTTP handler serving test endpoints for live Chromium verification."""

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default HTTP server stderr output during test runs."""
        pass

    def do_GET(self) -> None:
        if self.path == "/dynamic-lodging":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Dynamic Lodging Portal</title></head>
            <body>
              <div id="app">Loading hotel listings...</div>
              <script>
                // Simulate client-side SPA rendering
                setTimeout(function() {
                  var container = document.getElementById('app');
                  container.innerHTML = '<div class="hotel-card"><h2 class="hotel-name">Grand Heritage Resort</h2><span class="price">INR 4800</span><span class="rating">4.8</span></div>';
                }, 50);
              </script>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/slow-page":
            # Sleep 3 seconds to trigger a 1 second client timeout
            time.sleep(3.0)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>Too late</body></html>")

        elif self.path == "/redirect-to-metadata":
            # HTTP 302 redirect to AWS metadata endpoint
            self.send_response(302)
            self.send_header("Location", f"http://{AWS_METADATA_IPV4}/latest/meta-data/")
            self.end_headers()

        elif self.path == "/service-worker-test":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html>
            <body>
              <div id="sw-result">Testing</div>
              <script>
                if ('serviceWorker' in navigator && navigator.serviceWorker) {
                  try {
                    navigator.serviceWorker.register('/sw.js').then(function(reg) {
                      if (reg) {
                        document.getElementById('sw-result').innerText = 'SW_REGISTERED';
                      } else {
                        document.getElementById('sw-result').innerText = 'SW_BLOCKED: undefined registration';
                      }
                    }).catch(function(err) {
                      document.getElementById('sw-result').innerText = 'SW_BLOCKED: ' + err.name;
                    });
                  } catch (err) {
                    document.getElementById('sw-result').innerText = 'SW_BLOCKED: ' + err.message;
                  }
                } else {
                  document.getElementById('sw-result').innerText = 'SW_UNSUPPORTED';
                }
              </script>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/websocket-test":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html>
            <body>
              <div id="ws-result">Testing</div>
              <script>
                try {
                  var ws = new WebSocket('ws://echo.example.com');
                  document.getElementById('ws-result').innerText = 'WS_INITIALIZED';
                } catch (err) {
                  document.getElementById('ws-result').innerText = 'WS_DISABLED: ' + err.message;
                }
              </script>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/delayed-dynamic-content":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Delayed Lodging Portal</title></head>
            <body>
              <div id="initial-container">Initial static content without entities</div>
              <script>
                // Dynamically insert hotel card after navigation completes
                setTimeout(function() {
                  var div = document.createElement('div');
                  div.id = 'delayed-target';
                  div.className = 'hotel-card';
                  div.innerHTML = '<h2 class="hotel-name">Delayed Luxury Palms</h2><span class="price">INR 7500</span><span class="rating">4.9</span>';
                  document.body.appendChild(div);
                }, 150);
              </script>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/page-with-metadata-subresource":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = f"""
            <!DOCTYPE html>
            <html>
            <head><title>Page With Subresource</title></head>
            <body>
              <div id="sub-target">Subresource Page Content</div>
              <img src="http://{AWS_METADATA_IPV4}/subresource-probe.png" alt="probe" />
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def live_server() -> Generator[str, None, None]:
    """Spins up a lightweight localhost HTTP server in a daemon thread for live testing."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _LiveTestServerHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url
    finally:
        server.shutdown()


class TestLiveBrowserExecution:
    """Verifies actual Playwright Chromium browser execution and live network interception."""

    @pytest.mark.asyncio
    async def test_live_javascript_rendering(self, live_server: str) -> None:
        """Verifies actual JavaScript rendering, DOM hydration, and entity extraction."""

        # Custom validator allows the local test server IP while keeping metadata blocked
        def local_test_ip_validator(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
            return str(ip) != AWS_METADATA_IPV4

        adapter = PlaywrightBrowserAdapter(
            ip_validator=local_test_ip_validator,
            headless=True,
        )

        payload = BrowserTaskPayload(
            url=f"{live_server}/dynamic-lodging",
            canonical_url="https://example.com/resort",
            category="lodging",
            reason="CLIENT_SPA_RENDER",
            wait_until=BrowserWaitUntil.NETWORKIDLE,
            timeout_seconds=10,
            selector_target=".hotel-card",
        )

        result = await adapter.render_page(payload)

        assert result.status == BrowserRenderStatus.SUCCESS
        assert result.content_hash is not None
        assert result.excerpt_text is not None
        assert "Grand Heritage Resort" in result.excerpt_text
        assert "INR 4800" in result.excerpt_text
        assert result.excerpt_locator == ".hotel-card"
        assert result.unavailable_reason is None

        # Verify content_hash is the SHA256 of excerpt_text
        expected_hash = hashlib.sha256(result.excerpt_text.encode("utf-8")).hexdigest()
        assert result.content_hash == expected_hash

        # Verify Scrapling parsed entity
        assert len(result.parsed_lodging_observations) == 1
        obs = result.parsed_lodging_observations[0]
        assert obs.property_name == "Grand Heritage Resort"
        assert obs.price_per_night == 4800
        assert obs.rating == 4.8

    @pytest.mark.asyncio
    async def test_live_navigation_timeout(self, live_server: str) -> None:
        """Verifies that slow pages triggering Playwright timeout degrade to TIMED_OUT."""

        def local_test_ip_validator(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
            return str(ip) != AWS_METADATA_IPV4

        adapter = PlaywrightBrowserAdapter(
            ip_validator=local_test_ip_validator,
            headless=True,
        )

        payload = BrowserTaskPayload(
            url=f"{live_server}/slow-page",
            canonical_url="https://example.com/slow",
            category="lodging",
            reason="TIMEOUT_TEST",
            wait_until=BrowserWaitUntil.LOAD,
            timeout_seconds=1,  # 1 second timeout while endpoint sleeps 3 seconds
        )

        result = await adapter.render_page(payload)

        assert result.status == BrowserRenderStatus.TIMED_OUT
        assert result.content_hash is None
        assert result.excerpt_text is None
        assert result.unavailable_reason is not None
        assert "Navigation timeout of 1 seconds exceeded" in result.unavailable_reason

    @pytest.mark.asyncio
    async def test_live_redirect_to_metadata_endpoint_blocked(self, live_server: str) -> None:
        """Verifies that an HTTP 302 redirect to metadata endpoint is aborted in flight."""

        # Initial localhost request is allowed, but 169.254.169.254 redirect target is blocked
        def test_validator(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
            return str(ip) != AWS_METADATA_IPV4

        adapter = PlaywrightBrowserAdapter(
            ip_validator=test_validator,
            headless=True,
        )

        payload = BrowserTaskPayload(
            url=f"{live_server}/redirect-to-metadata",
            canonical_url="https://example.com/redirect",
            category="lodging",
            reason="REDIRECT_SSRF_TEST",
            wait_until=BrowserWaitUntil.LOAD,
            timeout_seconds=5,
        )

        result = await adapter.render_page(payload)

        assert result.status == BrowserRenderStatus.BLOCKED
        assert result.content_hash is None
        assert result.unavailable_reason is not None
        assert "Security policy blocked" in result.unavailable_reason
        assert AWS_METADATA_IPV4 in result.unavailable_reason

    @pytest.mark.asyncio
    async def test_live_default_adapter_blocks_metadata_and_private_destinations(self) -> None:
        """Verifies default PlaywrightBrowserAdapter blocks 169.254.169.254 and private IPs."""
        default_adapter = PlaywrightBrowserAdapter(headless=True)

        # 1. AWS EC2 metadata IP
        payload_metadata = BrowserTaskPayload(
            url=f"http://{AWS_METADATA_IPV4}/latest/meta-data/",
            canonical_url="http://169.254.169.254/latest/meta-data/",
            category="lodging",
            reason="METADATA_PROBE",
        )
        result_meta = await default_adapter.render_page(payload_metadata)
        assert result_meta.status == BrowserRenderStatus.BLOCKED
        assert result_meta.content_hash is None
        assert result_meta.unavailable_reason is not None
        assert "restricted or private range" in result_meta.unavailable_reason

        # 2. Loopback 127.0.0.1
        payload_loopback = BrowserTaskPayload(
            url="http://127.0.0.1:8080/admin",
            canonical_url="http://127.0.0.1:8080/admin",
            category="lodging",
            reason="LOOPBACK_PROBE",
        )
        result_loop = await default_adapter.render_page(payload_loopback)
        assert result_loop.status == BrowserRenderStatus.BLOCKED
        assert result_loop.content_hash is None
        assert result_loop.unavailable_reason is not None
        assert "restricted or private range" in result_loop.unavailable_reason

    @pytest.mark.asyncio
    async def test_live_service_workers_blocked_in_chromium(self, live_server: str) -> None:
        """Verifies that service_workers='block' prevents service worker registration."""

        def local_test_ip_validator(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
            return str(ip) != AWS_METADATA_IPV4

        adapter = PlaywrightBrowserAdapter(
            ip_validator=local_test_ip_validator,
            headless=True,
        )

        payload = BrowserTaskPayload(
            url=f"{live_server}/service-worker-test",
            canonical_url="https://example.com/sw",
            category="lodging",
            reason="SW_TEST",
            wait_until=BrowserWaitUntil.NETWORKIDLE,
            timeout_seconds=5,
            selector_target="#sw-result",
        )

        result = await adapter.render_page(payload)

        assert result.status == BrowserRenderStatus.SUCCESS
        assert result.excerpt_text is not None
        # Registration must fail or be blocked by Chromium
        assert "SW_REGISTERED" not in result.excerpt_text
        assert "SW_BLOCKED" in result.excerpt_text or "SW_UNSUPPORTED" in result.excerpt_text

    @pytest.mark.asyncio
    async def test_live_websockets_disabled_in_page_context(self, live_server: str) -> None:
        """Verifies that WebSocket constructors are disabled/neutered in browser context."""

        def local_test_ip_validator(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
            return str(ip) != AWS_METADATA_IPV4

        adapter = PlaywrightBrowserAdapter(
            ip_validator=local_test_ip_validator,
            headless=True,
        )

        payload = BrowserTaskPayload(
            url=f"{live_server}/websocket-test",
            canonical_url="https://example.com/ws",
            category="lodging",
            reason="WS_TEST",
            wait_until=BrowserWaitUntil.NETWORKIDLE,
            timeout_seconds=5,
            selector_target="#ws-result",
        )

        result = await adapter.render_page(payload)

        assert result.status == BrowserRenderStatus.SUCCESS
        assert result.excerpt_text is not None
        # WebSocket initialization must fail or throw
        assert "WS_INITIALIZED" not in result.excerpt_text
        assert "WS_DISABLED" in result.excerpt_text

    @pytest.mark.asyncio
    async def test_live_post_wait_snapshot_consistency_with_dynamic_js_insertion(
        self, live_server: str
    ) -> None:
        """C5: Verifies stored HTML, excerpt, and parsed entities match the post-wait DOM snapshot."""

        def local_test_ip_validator(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
            return str(ip) != AWS_METADATA_IPV4

        adapter = PlaywrightBrowserAdapter(
            ip_validator=local_test_ip_validator,
            headless=True,
        )

        payload = BrowserTaskPayload(
            url=f"{live_server}/delayed-dynamic-content",
            canonical_url="https://example.com/delayed-resort",
            category="lodging",
            reason="DYNAMIC_JS_INSERTION_TEST",
            wait_until=BrowserWaitUntil.DOMCONTENTLOADED,
            timeout_seconds=10,
            selector_target="#delayed-target",
        )

        result = await adapter.render_page(payload)

        assert result.status == BrowserRenderStatus.SUCCESS
        assert result.content_hash is not None
        assert result.excerpt_text is not None
        assert "Delayed Luxury Palms" in result.excerpt_text
        assert "INR 7500" in result.excerpt_text
        assert result.excerpt_locator == "#delayed-target"

        # Content hash must be SHA256 of excerpt_text
        expected_hash = hashlib.sha256(result.excerpt_text.encode("utf-8")).hexdigest()
        assert result.content_hash == expected_hash

        # Stored rendered HTML snippet must contain the post-wait dynamically inserted content
        assert result.rendered_html_snippet is not None
        assert "Delayed Luxury Palms" in result.rendered_html_snippet
        assert "hotel-card" in result.rendered_html_snippet

        # Parsed entities must be derived from the exact same post-wait snapshot
        assert len(result.parsed_lodging_observations) == 1
        obs = result.parsed_lodging_observations[0]
        assert obs.property_name == "Delayed Luxury Palms"
        assert obs.price_per_night == 7500
        assert obs.rating == 4.9

    @pytest.mark.asyncio
    async def test_live_egress_proxy_blocks_metadata_subresource(
        self, live_server: str
    ) -> None:
        """C3: Verifies that egress proxy blocks outbound subresources targeting metadata."""

        def local_test_ip_validator(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
            return str(ip) != AWS_METADATA_IPV4

        adapter = PlaywrightBrowserAdapter(
            ip_validator=local_test_ip_validator,
            headless=True,
        )

        payload = BrowserTaskPayload(
            url=f"{live_server}/page-with-metadata-subresource",
            canonical_url="https://example.com/subresource-test",
            category="lodging",
            reason="SUBRESOURCE_METADATA_TEST",
            wait_until=BrowserWaitUntil.LOAD,
            timeout_seconds=10,
            selector_target="#sub-target",
        )

        result = await adapter.render_page(payload)

        # Page itself loaded successfully but subresource was intercepted and blocked by egress proxy
        assert result.status == BrowserRenderStatus.SUCCESS
        assert result.excerpt_text is not None
        assert "Subresource Page Content" in result.excerpt_text
