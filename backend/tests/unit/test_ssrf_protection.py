"""Unit tests for SSRF protection and defensive URL validation conforming to spec 0002 AC 18.

Verifies rejection of loopback, link local, carrier grade NAT, RFC 1918 private IP ranges,
disallowed schemes, redirect hops pointing to private endpoints, and excessive payload sizes.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from functions.evidence_collector.ssrf import (
    SSRFPayloadSizeExceededError,
    SSRFSecurityError,
    is_ip_allowed,
    safe_fetch_page,
    validate_hostname_or_ip,
    validate_url_ssrf,
)


def _mock_dns_resolver_for_ip(ip_str: str):
    """Returns a mock DNS resolver callable that returns the specified IP."""
    def _resolver(host: str, port: int):
        family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip_str, port))]
    return _resolver


class TestSSRFValidator:
    """Tests for IP address and URL validation functions."""

    def test_is_ip_allowed_function(self) -> None:
        """Verifies is_ip_allowed directly for public and private IP objects."""
        import ipaddress

        assert is_ip_allowed(ipaddress.ip_address("8.8.8.8")) is True
        assert is_ip_allowed(ipaddress.ip_address("1.1.1.1")) is True
        assert is_ip_allowed(ipaddress.ip_address("127.0.0.1")) is False
        assert is_ip_allowed(ipaddress.ip_address("169.254.169.254")) is False
        assert is_ip_allowed(ipaddress.ip_address("10.0.0.1")) is False
        assert is_ip_allowed(ipaddress.ip_address("::1")) is False

    @pytest.mark.parametrize(
        "blocked_ip",
        [
            "127.0.0.1",
            "127.0.0.2",
            "127.255.255.255",
            "::1",
            "169.254.169.254",  # AWS EC2 metadata
            "169.254.1.1",
            "fe80::1",          # IPv6 link local
            "10.0.0.1",         # RFC 1918
            "10.254.0.1",
            "172.16.0.1",       # RFC 1918
            "172.31.255.255",
            "192.168.0.1",      # RFC 1918
            "192.168.1.100",
            "100.64.0.1",       # Carrier grade NAT (RFC 6598)
            "100.127.255.255",
            "0.0.0.0",          # Unspecified
            "::",
            "224.0.0.1",        # Multicast
            "240.0.0.1",        # Reserved
            "::ffff:127.0.0.1", # IPv4 mapped IPv6 loopback
            "::ffff:169.254.169.254", # IPv4 mapped IPv6 metadata
        ],
    )
    def test_blocked_ips_rejected(self, blocked_ip: str) -> None:
        """Verifies that all restricted, link local, and private IPs fail security checks."""
        resolver = _mock_dns_resolver_for_ip(blocked_ip)
        with pytest.raises(SSRFSecurityError):
            validate_hostname_or_ip("test.internal", dns_resolver=resolver)

    def test_direct_ip_literals_checked(self) -> None:
        """Verifies that direct IP addresses in URLs are checked without DNS resolution."""
        with pytest.raises(SSRFSecurityError):
            validate_url_ssrf("http://127.0.0.1:8080/admin")

        with pytest.raises(SSRFSecurityError):
            validate_url_ssrf("http://169.254.169.254/latest/meta-data/")

        with pytest.raises(SSRFSecurityError):
            validate_url_ssrf("http://10.0.0.5/secrets")

    def test_public_ip_and_domains_allowed(self) -> None:
        """Verifies that public, globally routable IPs pass security checks."""
        # 8.8.8.8 and 1.1.1.1 are public DNS resolvers
        resolver = _mock_dns_resolver_for_ip("8.8.8.8")
        ips = validate_hostname_or_ip("dns.google", dns_resolver=resolver)
        assert ips == ["8.8.8.8"]

        url = validate_url_ssrf("https://example.com/hotels", dns_resolver=resolver)
        assert url == "https://example.com/hotels"

    @pytest.mark.parametrize(
        "invalid_scheme_url",
        [
            "file:///etc/passwd",
            "ftp://ftp.example.com/data",
            "gopher://gopher.example.com/",
            "data:text/html;base64,PHNjcmlwdD4=",
            "javascript:alert(1)",
        ],
    )
    def test_invalid_schemes_rejected(self, invalid_scheme_url: str) -> None:
        """Verifies that non HTTP / HTTPS schemes are strictly rejected."""
        with pytest.raises(SSRFSecurityError):
            validate_url_ssrf(invalid_scheme_url)


class TestSafeFetchPage:
    """Tests for safe_fetch_page ensuring redirect validation and payload bounds."""

    @pytest.mark.asyncio
    async def test_redirect_to_private_ip_is_blocked(self) -> None:
        """Verifies that an HTTP redirect targeting a link local metadata address fails safely (AC 18)."""
        # Mock transport that redirects from public URL to link local address
        def handler(request: httpx.Request) -> httpx.Response:
            if "example.com" in request.url.host:
                return httpx.Response(
                    302,
                    headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                )
            return httpx.Response(200, text="SECRET_METADATA")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        # Resolver returns public IP for initial request, but direct IP for redirect will be blocked
        resolver = _mock_dns_resolver_for_ip("93.184.216.34")

        with pytest.raises(SSRFSecurityError) as exc_info:
            await safe_fetch_page(
                "http://example.com/redirect-to-metadata",
                client=client,
                dns_resolver=resolver,
            )
        assert "restricted" in str(exc_info.value).lower() or "169.254.169.254" in str(exc_info.value)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_redirect_to_loopback_is_blocked(self) -> None:
        """Verifies that an HTTP redirect targeting 127.0.0.1 fails safely (AC 18)."""
        def handler(request: httpx.Request) -> httpx.Response:
            if "example.com" in request.url.host:
                return httpx.Response(
                    301,
                    headers={"Location": "http://127.0.0.1:8000/internal-api"},
                )
            return httpx.Response(200, text="LOCAL_SECRET")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        resolver = _mock_dns_resolver_for_ip("93.184.216.34")

        with pytest.raises(SSRFSecurityError) as exc_info:
            await safe_fetch_page(
                "http://example.com/redirect-to-loopback",
                client=client,
                dns_resolver=resolver,
            )
        assert "restricted" in str(exc_info.value).lower() or "127.0.0.1" in str(exc_info.value)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_max_redirects_enforced(self) -> None:
        """Verifies that infinite redirect loops are capped and raise SSRFSecurityError."""
        redirect_counter = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal redirect_counter
            redirect_counter += 1
            return httpx.Response(
                302,
                headers={"Location": f"http://example.com/hop-{redirect_counter}"},
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        resolver = _mock_dns_resolver_for_ip("93.184.216.34")

        with pytest.raises(SSRFSecurityError) as exc_info:
            await safe_fetch_page(
                "http://example.com/hop-0",
                client=client,
                max_redirects=3,
                dns_resolver=resolver,
            )
        assert "redirect limit exceeded" in str(exc_info.value).lower()
        await client.aclose()

    @pytest.mark.asyncio
    async def test_payload_size_limit_enforced(self) -> None:
        """Verifies that responses exceeding the max payload byte limit are rejected."""
        large_body = "A" * 1024 * 100  # 100 KB

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=large_body)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        resolver = _mock_dns_resolver_for_ip("93.184.216.34")

        with pytest.raises(SSRFPayloadSizeExceededError) as exc_info:
            await safe_fetch_page(
                "http://example.com/large",
                client=client,
                max_payload_bytes=1024 * 50,  # 50 KB max
                dns_resolver=resolver,
            )
        assert "payload size exceeded" in str(exc_info.value).lower()
        await client.aclose()

    @pytest.mark.asyncio
    async def test_valid_page_fetches_successfully(self) -> None:
        """Verifies that a valid public page with permitted status returns body text."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body><h1>Public Content</h1></body></html>")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        resolver = _mock_dns_resolver_for_ip("93.184.216.34")

        text, final_url, status = await safe_fetch_page(
            "http://example.com/valid",
            client=client,
            dns_resolver=resolver,
        )
        assert "Public Content" in text
        assert status == 200
        assert "example.com" in final_url
        await client.aclose()
