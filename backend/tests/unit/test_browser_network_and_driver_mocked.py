"""Mocked unit tests for browser network controls and Playwright driver adapter.

Conforms to spec 0003 Slice 4:
1. is_ip_allowed rejects loopback, RFC 1918, link local, metadata, and carrier grade NAT.
2. resolve_and_validate_host verifies IP safety across single and multiple DNS records.
3. validate_navigation_url blocks dangerous schemes and restricted destinations.
4. RouteSecurityInterceptor aborts WebSockets, forbidden protocols, and simulates DNS rebinding defense.
5. PlaywrightBrowserAdapter configures service_workers="block", WebSocket disabling,
   pre navigation validation, and graceful degradation for timeouts, blocks, and crashes.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from contracts.browser import (
    BrowserRenderStatus,
    BrowserTaskPayload,
    BrowserWaitUntil,
)
from functions.browser.driver import PlaywrightBrowserAdapter
from functions.browser.network import (
    BrowserSecurityError,
    RouteSecurityInterceptor,
    SecureEgressProxy,
    is_ip_allowed,
    resolve_and_validate_host,
    validate_navigation_url,
)


def _mock_dns_resolver_for_ips(*ips: str) -> Callable[[str, int], list[Any]]:
    """Returns a mock DNS resolver callable that returns the specified IP addresses."""

    def _resolver(host: str, port: int) -> list[Any]:
        results: list[Any] = []
        for ip_str in ips:
            family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
            results.append((family, socket.SOCK_STREAM, 6, "", (ip_str, port)))
        return results

    return _resolver


class TestBrowserNetworkSecurity:
    """Tests for IP address filtering and DNS resolution validation."""

    def test_is_ip_allowed_public_ips(self) -> None:
        """Verifies public globally routable IP addresses pass checks."""
        assert is_ip_allowed(ipaddress.ip_address("8.8.8.8")) is True
        assert is_ip_allowed(ipaddress.ip_address("1.1.1.1")) is True
        assert is_ip_allowed(ipaddress.ip_address("93.184.216.34")) is True
        assert is_ip_allowed(ipaddress.ip_address("2607:f8b0:4005:805::200e")) is True

    @pytest.mark.parametrize(
        "blocked_ip",
        [
            "127.0.0.1",
            "127.0.0.2",
            "127.255.255.255",
            "::1",
            "169.254.169.254",  # AWS IMDS IPv4
            "fd00:ec2::254",  # AWS IMDS IPv6
            "169.254.1.1",  # Link local IPv4
            "fe80::1",  # Link local IPv6
            "10.0.0.1",  # RFC 1918 private
            "10.254.0.1",
            "172.16.0.1",  # RFC 1918 private
            "172.31.255.255",
            "192.168.0.1",  # RFC 1918 private
            "192.168.1.100",
            "fc00::1",  # IPv6 unique local
            "100.64.0.1",  # Carrier grade NAT (RFC 6598)
            "100.127.255.255",
            "0.0.0.0",  # Unspecified
            "::",
            "224.0.0.1",  # Multicast
            "ff02::1",
            "240.0.0.1",  # Reserved
            "::ffff:127.0.0.1",  # IPv4 mapped IPv6 loopback
            "::ffff:169.254.169.254",  # IPv4 mapped IPv6 metadata
        ],
    )
    def test_is_ip_allowed_rejects_restricted_ips(self, blocked_ip: str) -> None:
        """Verifies that all private, link local, metadata, and reserved IPs are rejected."""
        ip_obj = ipaddress.ip_address(blocked_ip)
        assert is_ip_allowed(ip_obj) is False

    def test_resolve_and_validate_host_direct_ip_literals(self) -> None:
        """Verifies direct IP literals are checked without network DNS lookup."""
        # Public IP literal passes
        res = resolve_and_validate_host("8.8.8.8")
        assert res == ["8.8.8.8"]

        # Loopback and metadata literals fail
        with pytest.raises(BrowserSecurityError, match="restricted or private range"):
            resolve_and_validate_host("127.0.0.1")

        with pytest.raises(BrowserSecurityError, match="restricted or private range"):
            resolve_and_validate_host("169.254.169.254")

    def test_resolve_and_validate_host_dns_resolution(self) -> None:
        """Verifies domain resolution against public and private targets."""
        resolver_safe = _mock_dns_resolver_for_ips("93.184.216.34")
        ips = resolve_and_validate_host("example.com", dns_resolver=resolver_safe)
        assert ips == ["93.184.216.34"]

        resolver_meta = _mock_dns_resolver_for_ips("169.254.169.254")
        with pytest.raises(BrowserSecurityError, match="resolved to restricted IP"):
            resolve_and_validate_host("metadata.internal", dns_resolver=resolver_meta)

    def test_resolve_and_validate_host_multi_ip_mixed_records(self) -> None:
        """Verifies that if any DNS record in a set resolves to a restricted IP, all are rejected."""
        # Domain with one public IP and one internal loopback IP
        resolver_mixed = _mock_dns_resolver_for_ips("93.184.216.34", "127.0.0.1")
        with pytest.raises(BrowserSecurityError, match="resolved to restricted IP"):
            resolve_and_validate_host("mixed.example.com", dns_resolver=resolver_mixed)

    def test_validate_navigation_url_schemes(self) -> None:
        """Verifies allowed and disallowed URL schemes."""
        resolver = _mock_dns_resolver_for_ips("93.184.216.34")

        # Allowed HTTP and HTTPS
        url_https = validate_navigation_url("https://example.com/hotels", dns_resolver=resolver)
        assert url_https == "https://example.com/hotels"

        url_http = validate_navigation_url("http://example.com/hotels", dns_resolver=resolver)
        assert url_http == "http://example.com/hotels"

        # Disallowed schemes
        for invalid in [
            "file:///etc/passwd",
            "data:text/html,<h1>Hello</h1>",
            "ws://example.com/socket",
            "wss://example.com/socket",
            "ftp://example.com/files",
            "gopher://example.com/1",
        ]:
            with pytest.raises(BrowserSecurityError, match="not permitted"):
                validate_navigation_url(invalid, dns_resolver=resolver)


class TestRouteSecurityInterceptor:
    """Tests for in flight Playwright route interception and DNS rebinding protection."""

    @pytest.mark.asyncio
    async def test_route_interceptor_aborts_websockets(self) -> None:
        """Verifies WebSocket requests are aborted by client immediately."""
        interceptor = RouteSecurityInterceptor()

        # Route with websocket resource type
        route = MagicMock()
        route.request = MagicMock(
            url="wss://example.com/feed",
            resource_type="websocket",
            headers={},
        )
        route.abort = AsyncMock()
        route.continue_ = AsyncMock()

        await interceptor(route)

        route.abort.assert_awaited_once_with("blockedbyclient")
        route.continue_.assert_not_awaited()
        assert len(interceptor.blocked_requests) == 1
        assert "WebSocket" in interceptor.blocked_requests[0]["reason"]

    @pytest.mark.asyncio
    async def test_route_interceptor_aborts_websocket_upgrade_header(self) -> None:
        """Verifies HTTP upgrade header for websocket is detected and aborted."""
        interceptor = RouteSecurityInterceptor()

        route = MagicMock()
        route.request = MagicMock(
            url="http://example.com/chat",
            resource_type="xhr",
            headers={"upgrade": "websocket"},
        )
        route.abort = AsyncMock()
        route.continue_ = AsyncMock()

        await interceptor(route)

        route.abort.assert_awaited_once_with("blockedbyclient")
        assert len(interceptor.blocked_requests) == 1

    @pytest.mark.asyncio
    async def test_route_interceptor_aborts_disallowed_schemes(self) -> None:
        """Verifies non HTTP schemes like file: or ftp: are aborted in flight."""
        interceptor = RouteSecurityInterceptor()

        route = MagicMock()
        route.request = MagicMock(
            url="file:///etc/hosts",
            resource_type="document",
            headers={},
        )
        route.abort = AsyncMock()
        route.continue_ = AsyncMock()

        await interceptor(route)

        route.abort.assert_awaited_once_with("blockedbyclient")
        assert len(interceptor.blocked_requests) == 1
        assert "Disallowed scheme" in interceptor.blocked_requests[0]["reason"]

    @pytest.mark.asyncio
    async def test_route_interceptor_aborts_data_navigation(self) -> None:
        """Verifies data: scheme for top level navigation or frame is aborted."""
        interceptor = RouteSecurityInterceptor()

        route = MagicMock()
        route.request = MagicMock(
            url="data:text/html,<h1>XSS</h1>",
            resource_type="document",
            headers={},
            is_navigation_request=MagicMock(return_value=True),
        )
        route.abort = AsyncMock()
        route.continue_ = AsyncMock()

        await interceptor(route)

        route.abort.assert_awaited_once_with("blockedbyclient")
        assert len(interceptor.blocked_requests) == 1

    @pytest.mark.asyncio
    async def test_route_interceptor_allows_data_subresources(self) -> None:
        """Verifies data: images or inline fonts are allowed as harmless subresources."""
        interceptor = RouteSecurityInterceptor()

        route = MagicMock()
        route.request = MagicMock(
            url="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            resource_type="image",
            headers={},
            is_navigation_request=MagicMock(return_value=False),
        )
        route.abort = AsyncMock()
        route.continue_ = AsyncMock()

        await interceptor(route)

        route.continue_.assert_awaited_once()
        route.abort.assert_not_awaited()
        assert len(interceptor.blocked_requests) == 0

    @pytest.mark.asyncio
    async def test_route_interceptor_dns_rebinding_simulation(self) -> None:
        """Explicitly tests DNS rebinding protection.

        Simulates attacker domain attacker.com:
        - Hop 1 resolves to public IP 93.184.216.34: route is allowed.
        - Hop 2 rebinds to metadata IP 169.254.169.254: route is aborted.
        Proves hostname check alone is insufficient, live IP resolution per request is essential.
        """
        dns_state = {"current_ip": "93.184.216.34"}

        def dynamic_resolver(host: str, port: int) -> list[Any]:
            ip_str = dns_state["current_ip"]
            family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
            return [(family, socket.SOCK_STREAM, 6, "", (ip_str, port))]

        interceptor = RouteSecurityInterceptor(dns_resolver=dynamic_resolver)

        # First request: resolves to public IP
        route1 = MagicMock()
        route1.request = MagicMock(
            url="https://attacker.com/initial",
            resource_type="document",
            headers={},
        )
        route1.abort = AsyncMock()
        route1.continue_ = AsyncMock()

        await interceptor(route1)
        route1.continue_.assert_awaited_once()
        route1.abort.assert_not_awaited()
        assert len(interceptor.blocked_requests) == 0

        # DNS rebinding occurs: attacker.com now resolves to AWS metadata IP
        dns_state["current_ip"] = "169.254.169.254"

        route2 = MagicMock()
        route2.request = MagicMock(
            url="https://attacker.com/steal-creds",
            resource_type="fetch",
            headers={},
        )
        route2.abort = AsyncMock()
        route2.continue_ = AsyncMock()

        await interceptor(route2)
        route2.abort.assert_awaited_once_with("blockedbyclient")
        route2.continue_.assert_not_awaited()
        assert len(interceptor.blocked_requests) == 1
        assert "restricted IP 169.254.169.254" in interceptor.blocked_requests[0]["reason"]

    @pytest.mark.asyncio
    async def test_route_interceptor_offloads_dns_resolution_via_asyncio_to_thread(self) -> None:
        """Verifies RouteSecurityInterceptor uses asyncio.to_thread for non blocking DNS calls."""
        interceptor = RouteSecurityInterceptor(dns_resolver=_mock_dns_resolver_for_ips("93.184.216.34"))

        route = MagicMock()
        route.request = MagicMock(
            url="https://example.com/api/data",
            resource_type="fetch",
            headers={},
        )
        route.abort = AsyncMock()
        route.continue_ = AsyncMock()

        with patch("functions.browser.network.asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            await interceptor(route)
            mock_to_thread.assert_awaited_once()

        route.continue_.assert_awaited_once()
        route.abort.assert_not_awaited()


class TestPlaywrightBrowserAdapterMocked:
    """Mocked tests for PlaywrightBrowserAdapter lifecycle and error degradation."""

    @pytest.fixture
    def sample_payload(self) -> BrowserTaskPayload:
        return BrowserTaskPayload(
            url="https://example.com/hotels/resort",
            canonical_url="https://example.com/hotels/resort",
            category="lodging",
            reason="NOSCRIPT_JAVASCRIPT_REQUIRED",
            wait_until=BrowserWaitUntil.NETWORKIDLE,
            timeout_seconds=20,
            selector_target=".hotel-card",
        )

    @pytest.mark.asyncio
    async def test_pre_navigation_security_rejection(self) -> None:
        """Verifies adapter rejects private destination without launching browser."""
        resolver_private = _mock_dns_resolver_for_ips("127.0.0.1")
        adapter = PlaywrightBrowserAdapter(dns_resolver=resolver_private)

        payload = BrowserTaskPayload(
            url="http://internal-service.local/admin",
            canonical_url="http://internal-service.local/admin",
            category="lodging",
            reason="NOSCRIPT_JAVASCRIPT_REQUIRED",
        )

        result = await adapter.render_page(payload)

        assert result.status == BrowserRenderStatus.BLOCKED
        assert result.content_hash is None
        assert result.excerpt_text is None
        assert result.unavailable_reason is not None
        assert "Pre navigation security policy blocked URL" in result.unavailable_reason

    @pytest.mark.asyncio
    async def test_context_configuration_and_websocket_disabling(
        self, sample_payload: BrowserTaskPayload
    ) -> None:
        """Verifies service_workers='block', WebSocket disabling script, and route handler."""
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()

        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.route = AsyncMock()
        mock_context.add_init_script = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()
        mock_page.close = AsyncMock()

        mock_page.url = sample_payload.url
        mock_page.goto = AsyncMock(return_value=MagicMock(status=200))
        mock_page.title = AsyncMock(return_value="Luxury Resort & Spa")
        mock_page.content = AsyncMock(
            return_value="<html><body><div class='hotel-card'>Grand Palace Resort INR 4500</div></body></html>"
        )

        mock_target = MagicMock()
        mock_target.inner_text = AsyncMock(return_value="Grand Palace Resort INR 4500")
        mock_page.wait_for_selector = AsyncMock()
        mock_page.query_selector = AsyncMock(return_value=mock_target)

        resolver_public = _mock_dns_resolver_for_ips("93.184.216.34")
        adapter = PlaywrightBrowserAdapter(
            browser=mock_browser,
            dns_resolver=resolver_public,
        )

        result = await adapter.render_page(sample_payload)

        # Verify context options: service_workers="block"
        mock_browser.new_context.assert_awaited_once()
        _, kwargs = mock_browser.new_context.call_args
        assert kwargs.get("service_workers") == "block"

        # Verify WebSocket disabled via add_init_script
        mock_context.add_init_script.assert_awaited_once()
        script_arg = mock_context.add_init_script.call_args[0][0]
        assert "WebSocket" in script_arg

        # Verify route interception registered on context
        mock_context.route.assert_awaited_once()
        assert mock_context.route.call_args[0][0] == "**/*"

        # Verify render outcome
        assert result.status == BrowserRenderStatus.SUCCESS
        assert result.content_hash is not None
        assert result.excerpt_text == "Grand Palace Resort INR 4500"
        assert result.excerpt_locator == ".hotel-card"
        assert result.unavailable_reason is None

    @pytest.mark.asyncio
    async def test_navigation_timeout_degrades_gracefully(
        self, sample_payload: BrowserTaskPayload
    ) -> None:
        """Verifies Playwright TimeoutError degrades to TIMED_OUT status."""
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()

        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.route = AsyncMock()
        mock_context.add_init_script = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()
        mock_page.close = AsyncMock()

        mock_page.goto = AsyncMock(
            side_effect=PlaywrightTimeoutError("Navigation timeout of 20000ms exceeded")
        )

        resolver_public = _mock_dns_resolver_for_ips("93.184.216.34")
        adapter = PlaywrightBrowserAdapter(
            browser=mock_browser,
            dns_resolver=resolver_public,
        )

        result = await adapter.render_page(sample_payload)

        assert result.status == BrowserRenderStatus.TIMED_OUT
        assert result.content_hash is None
        assert result.excerpt_text is None
        assert result.unavailable_reason is not None
        assert "Navigation timeout of 20 seconds exceeded" in result.unavailable_reason

    @pytest.mark.asyncio
    async def test_bot_challenge_detection_degrades_to_blocked(
        self, sample_payload: BrowserTaskPayload
    ) -> None:
        """Verifies Cloudflare or bot challenge title triggers BLOCKED status."""
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()

        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.route = AsyncMock()
        mock_context.add_init_script = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()
        mock_page.close = AsyncMock()

        mock_page.url = sample_payload.url
        mock_page.goto = AsyncMock(return_value=MagicMock(status=200))
        mock_page.title = AsyncMock(return_value="Just a moment... Cloudflare")
        mock_page.content = AsyncMock(
            return_value="<html><body>Please wait, verifying your browser</body></html>"
        )

        resolver_public = _mock_dns_resolver_for_ips("93.184.216.34")
        adapter = PlaywrightBrowserAdapter(
            browser=mock_browser,
            dns_resolver=resolver_public,
        )

        result = await adapter.render_page(sample_payload)

        assert result.status == BrowserRenderStatus.BLOCKED
        assert result.content_hash is None
        assert result.excerpt_text is None
        assert result.unavailable_reason is not None
        assert "Bot verification or anti bot challenge detected" in result.unavailable_reason

    @pytest.mark.asyncio
    async def test_http_403_forbidden_degrades_to_blocked(
        self, sample_payload: BrowserTaskPayload
    ) -> None:
        """Verifies HTTP 403 response triggers BLOCKED status."""
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()

        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.route = AsyncMock()
        mock_context.add_init_script = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()
        mock_page.close = AsyncMock()

        mock_page.url = sample_payload.url
        mock_page.goto = AsyncMock(return_value=MagicMock(status=403))

        resolver_public = _mock_dns_resolver_for_ips("93.184.216.34")
        adapter = PlaywrightBrowserAdapter(
            browser=mock_browser,
            dns_resolver=resolver_public,
        )

        result = await adapter.render_page(sample_payload)

        assert result.status == BrowserRenderStatus.BLOCKED
        assert result.content_hash is None
        assert result.unavailable_reason is not None
        assert "HTTP 403" in result.unavailable_reason

    @pytest.mark.asyncio
    async def test_browser_crash_degrades_to_unavailable(
        self, sample_payload: BrowserTaskPayload
    ) -> None:
        """Verifies unexpected browser crash or error degrades to UNAVAILABLE."""
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()

        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.route = AsyncMock()
        mock_context.add_init_script = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()
        mock_page.close = AsyncMock()

        mock_page.goto = AsyncMock(
            side_effect=PlaywrightError("Target page, context or browser has been closed")
        )

        resolver_public = _mock_dns_resolver_for_ips("93.184.216.34")
        adapter = PlaywrightBrowserAdapter(
            browser=mock_browser,
            dns_resolver=resolver_public,
        )

        result = await adapter.render_page(sample_payload)

        assert result.status == BrowserRenderStatus.UNAVAILABLE
        assert result.content_hash is None
        assert result.unavailable_reason is not None
        assert "Target page, context or browser has been closed" in result.unavailable_reason

    @pytest.mark.asyncio
    async def test_redirect_response_tracking_retains_and_cleans_up_background_tasks(
        self, sample_payload: BrowserTaskPayload
    ) -> None:
        """Verifies that redirect on_response listener tracks background tasks in a strong reference set."""
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()

        registered_callbacks: dict[str, Any] = {}

        def mock_on(event: str, callback: Any) -> None:
            registered_callbacks[event] = callback

        mock_page.on = MagicMock(side_effect=mock_on)
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.route = AsyncMock()
        mock_context.add_init_script = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()
        mock_page.close = AsyncMock()

        mock_page.url = sample_payload.url
        mock_page.title = AsyncMock(return_value="Resort")
        mock_page.content = AsyncMock(
            return_value="<html><body><div>Grand Resort Excerpt INR 5000</div></body></html>"
        )
        mock_target = MagicMock()
        mock_target.inner_text = AsyncMock(return_value="Grand Resort Excerpt INR 5000")
        mock_page.wait_for_selector = AsyncMock()
        mock_page.query_selector = AsyncMock(return_value=mock_target)

        # Trigger response listener during navigation
        async def fake_goto(*args: Any, **kwargs: Any) -> Any:
            on_res = registered_callbacks.get("response")
            if on_res:
                mock_res = MagicMock()
                mock_res.status = 302
                mock_res.headers = {"location": "https://example.com/redirected"}
                mock_res.url = "https://example.com/start"
                on_res(mock_res)
            return MagicMock(status=200)

        mock_page.goto = AsyncMock(side_effect=fake_goto)

        resolver_public = _mock_dns_resolver_for_ips("93.184.216.34")
        adapter = PlaywrightBrowserAdapter(
            browser=mock_browser,
            dns_resolver=resolver_public,
        )

        result = await adapter.render_page(sample_payload)

        assert result.status == BrowserRenderStatus.SUCCESS
        assert "response" in registered_callbacks


class TestSecureEgressProxy:
    """C3: Verifies that SecureEgressProxy enforces destination safety at the socket boundary."""

    @pytest.mark.asyncio
    async def test_egress_proxy_blocks_direct_metadata_and_private_ips(self) -> None:
        """Verifies proxy immediately returns HTTP 403 Forbidden for metadata or private IP requests."""
        proxy = SecureEgressProxy()
        port = await proxy.start()

        try:
            # 1. Request to metadata endpoint
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: 169.254.169.254\r\n\r\n")
            await writer.drain()

            resp = await reader.read(4096)
            writer.close()
            await writer.wait_closed()

            assert b"403 Forbidden" in resp
            assert b"Egress blocked by security policy" in resp
            assert len(proxy.blocked_requests) == 1
            assert "169.254.169.254" in proxy.blocked_requests[0]["url"]

            # 2. Request to private IP
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
            writer2.write(b"GET http://10.0.0.1/admin HTTP/1.1\r\nHost: 10.0.0.1\r\n\r\n")
            await writer2.drain()

            resp2 = await reader2.read(4096)
            writer2.close()
            await writer2.wait_closed()

            assert b"403 Forbidden" in resp2
            assert len(proxy.blocked_requests) == 2
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_egress_proxy_blocks_dns_rebinding_attempts(self) -> None:
        """Verifies proxy resolves hostnames before connecting and blocks rebinding to metadata."""
        # Resolver returns metadata IP for a legitimate-looking domain
        resolver_rebind = _mock_dns_resolver_for_ips("169.254.169.254")

        proxy = SecureEgressProxy(dns_resolver=resolver_rebind)
        port = await proxy.start()

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET http://rebind.attacker.com/data HTTP/1.1\r\nHost: rebind.attacker.com\r\n\r\n")
            await writer.drain()

            resp = await reader.read(4096)
            writer.close()
            await writer.wait_closed()

            assert b"403 Forbidden" in resp
            assert len(proxy.blocked_requests) == 1
            assert "rebind.attacker.com" in proxy.blocked_requests[0]["url"]
            assert "restricted IP" in proxy.blocked_requests[0]["reason"]
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_egress_proxy_blocks_websocket_upgrade(self) -> None:
        """Verifies proxy blocks WebSocket upgrade handshakes with HTTP 403 Forbidden."""
        proxy = SecureEgressProxy()
        port = await proxy.start()

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            req = (
                b"GET http://example.com/ws HTTP/1.1\r\n"
                b"Host: example.com\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n\r\n"
            )
            writer.write(req)
            await writer.drain()

            resp = await reader.read(4096)
            writer.close()
            await writer.wait_closed()

            assert b"403 Forbidden" in resp
            assert b"WebSockets are disabled" in resp
            assert len(proxy.blocked_requests) == 1
            assert "WebSocket upgrade" in proxy.blocked_requests[0]["reason"]
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_egress_proxy_binds_connection_to_validated_ip(self) -> None:
        """Verifies proxy binds outbound connection to the validated IP literal and forwards request."""
        # Spin up a small local target server
        received_requests: list[str] = []

        async def handle_target(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            line = await r.readline()
            received_requests.append(line.decode("latin1"))
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK")
            await w.drain()
            w.close()
            await w.wait_closed()

        target_server = await asyncio.start_server(handle_target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]

        # Validator allows 127.0.0.1 for this test
        proxy = SecureEgressProxy(ip_validator=lambda ip: True)
        proxy_port = await proxy.start()

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            req = f"GET http://127.0.0.1:{target_port}/test-path HTTP/1.1\r\nHost: 127.0.0.1:{target_port}\r\n\r\n".encode("latin1")
            writer.write(req)
            await writer.drain()

            resp = await reader.read(4096)
            writer.close()
            await writer.wait_closed()

            assert b"200 OK" in resp
            assert b"OK" in resp
            assert len(received_requests) == 1
            assert "GET /test-path HTTP/1.1" in received_requests[0]
            assert len(proxy.blocked_requests) == 0
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    @pytest.mark.asyncio
    async def test_egress_proxy_multi_homed_fallback_on_first_ip_failure(self) -> None:
        """Verifies proxy falls back to second candidate IP if first candidate IP connection fails."""
        received_requests: list[str] = []

        async def handle_target(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            line = await r.readline()
            received_requests.append(line.decode("latin1"))
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 9\r\n\r\nSECONDARY")
            await w.drain()
            w.close()
            await w.wait_closed()

        target_server = await asyncio.start_server(handle_target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]

        # DNS resolver returns two IPs: first IP 127.0.0.99 (nothing listening), second IP 127.0.0.1
        fake_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.99", target_port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", target_port)),
        ]
        proxy = SecureEgressProxy(
            dns_resolver=lambda host, port: fake_dns,
            ip_validator=lambda ip: True,
        )
        proxy_port = await proxy.start()

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            req = f"GET http://multihomed.example.com:{target_port}/resilient HTTP/1.1\r\nHost: multihomed.example.com:{target_port}\r\n\r\n".encode("latin1")
            writer.write(req)
            await writer.drain()

            resp = await reader.read(4096)
            writer.close()
            await writer.wait_closed()

            assert b"200 OK" in resp
            assert b"SECONDARY" in resp
            assert len(received_requests) == 1
            assert "GET /resilient HTTP/1.1" in received_requests[0]
            assert len(proxy.blocked_requests) == 0
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

