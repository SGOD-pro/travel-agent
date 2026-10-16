"""Live browser network controls and SSRF protection conforming to spec 0003 Slice 4.

Blocks requests to loopback, link local, RFC 1918 private, carrier grade NAT, and cloud
metadata endpoints (169.254.169.254, fd00:ec2::254).

Crucial security invariant:
Hostname route checks alone do NOT guarantee connection safety against DNS rebinding,
because an attacker controlled domain can pass hostname filtering while resolving or
rebinding to private IP addresses. Connection safety requires:
1. Pre navigation URL and DNS resolution validation.
2. In flight route interception on every outgoing request and redirect hop, resolving
   the destination address and verifying all resolved IP addresses against restricted subnets.
3. Explicit disabling of Service Workers (service_workers="block") to prevent caching bypass
   and offline request hijacking.
4. Explicit blocking of WebSockets (ws://, wss://, upgrade headers, and window.WebSocket)
   to prevent unrestricted bidirectional local sockets.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# Carrier grade NAT network (RFC 6598: 100.64.0.0/10)
_CARRIER_GRADE_NAT = ipaddress.ip_network("100.64.0.0/10")

# AWS IMDS endpoints
AWS_METADATA_IPV4 = "169.254.169.254"
AWS_METADATA_IPV6 = "fd00:ec2::254"


class BrowserSecurityError(Exception):
    """Raised when a URL, IP address, or network request violates security policies."""


def is_ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Returns True if the IP address is globally routable and public.

    Rejects loopback, link local, private, carrier grade NAT, multicast,
    unspecified, reserved ranges, and AWS EC2 metadata endpoints.
    """
    # Unpack IPv4 mapped IPv6 addresses (e.g. ::ffff:127.0.0.1, ::ffff:169.254.169.254)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _CARRIER_GRADE_NAT)
    ):
        return False

    # Explicit check for known cloud metadata endpoints
    ip_str = str(ip).lower()
    if ip_str == AWS_METADATA_IPV4 or ip_str == AWS_METADATA_IPV6:
        return False

    return True


def resolve_and_validate_host(
    hostname: str,
    port: int | None = None,
    *,
    dns_resolver: Callable[[str, int], list[Any]] | None = None,
    ip_validator: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool] | None = None,
) -> list[str]:
    """Resolves hostname and verifies that all resolved IP addresses are public and safe.

    Returns the list of safe resolved IP strings. Raises BrowserSecurityError if any address
    violates security policies.
    """
    check_ip = ip_validator or is_ip_allowed
    cleaned_host = hostname.strip().strip("[]")
    if not cleaned_host:
        raise BrowserSecurityError("Host name cannot be empty")

    # Fast path: direct IP literal
    try:
        ip_obj = ipaddress.ip_address(cleaned_host)
        if not check_ip(ip_obj):
            raise BrowserSecurityError(
                f"Direct IP address {cleaned_host} is in a restricted or private range"
            )
        return [str(ip_obj)]
    except ValueError:
        # Domain name requiring DNS resolution
        pass

    resolved_ips: list[str] = []
    effective_port = port or 80

    try:
        if dns_resolver is not None:
            addr_info = dns_resolver(cleaned_host, effective_port)
        else:
            addr_info = socket.getaddrinfo(
                cleaned_host,
                effective_port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
    except socket.gaierror as exc:
        raise BrowserSecurityError(f"DNS resolution failed for host {cleaned_host}: {exc}") from exc

    for _family, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as err:
            raise BrowserSecurityError(f"Invalid resolved IP address {ip_str}") from err

        if not check_ip(ip_obj):
            raise BrowserSecurityError(f"Host {cleaned_host} resolved to restricted IP {ip_str}")
        resolved_ips.append(ip_str)

    if not resolved_ips:
        raise BrowserSecurityError(f"Host {cleaned_host} resolved to zero IP addresses")

    return resolved_ips


def validate_navigation_url(
    url: str,
    *,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
    dns_resolver: Callable[[str, int], list[Any]] | None = None,
    ip_validator: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool] | None = None,
) -> str:
    """Validates top level navigation URL scheme and destination host before navigation.

    Returns the validated URL string or raises BrowserSecurityError.
    """
    if not url or not isinstance(url, str):
        raise BrowserSecurityError("URL must be a non empty string")

    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes:
        raise BrowserSecurityError(
            f"Scheme '{scheme}' is not permitted for browser navigation; allowed schemes are {allowed_schemes}"
        )

    hostname = parsed.hostname
    if not hostname:
        raise BrowserSecurityError(f"URL '{url}' does not contain a valid hostname")

    effective_port = parsed.port or (443 if scheme == "https" else 80)
    resolve_and_validate_host(
        hostname,
        port=effective_port,
        dns_resolver=dns_resolver,
        ip_validator=ip_validator,
    )
    return url.strip()


class RouteSecurityInterceptor:
    """In flight Playwright route handler enforcing SSRF, scheme, and WebSocket policies.

    Inspects every outgoing request, subresource fetch, and redirect hop:
    - Blocks WebSocket connection requests (resource_type == 'websocket' or ws/wss schemes).
    - Blocks dangerous protocols (file, ftp, gopher, javascript).
    - Resolves destination hostname on every request to prevent DNS rebinding attacks.
    - Aborts requests targeting private, loopback, or cloud metadata endpoints.
    """

    def __init__(
        self,
        *,
        dns_resolver: Callable[[str, int], list[Any]] | None = None,
        ip_validator: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool] | None = None,
    ) -> None:
        self.dns_resolver = dns_resolver
        self.ip_validator = ip_validator
        self.blocked_requests: list[dict[str, Any]] = []

    async def __call__(self, route: Any) -> None:
        """Playwright route interception callback."""
        request = route.request
        url = request.url
        resource_type = getattr(request, "resource_type", "")
        headers = getattr(request, "headers", {}) or {}

        # 1. Detect and abort WebSockets
        is_websocket = (
            resource_type == "websocket"
            or headers.get("upgrade", "").lower() == "websocket"
            or url.startswith(("ws://", "wss://"))
        )
        if is_websocket:
            msg = f"WebSocket request blocked: {url}"
            logger.warning(msg)
            self.blocked_requests.append({"url": url, "reason": msg})
            await route.abort("blockedbyclient")
            return

        # 2. Protocol and scheme validation
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()

        # Allow harmless data: or blob: subresources for fonts/images, but disallow navigation
        if scheme in ("data", "blob"):
            is_navigation = False
            if hasattr(request, "is_navigation_request"):
                try:
                    is_navigation = request.is_navigation_request()
                except Exception:
                    is_navigation = False
            elif resource_type in ("document", "frame"):
                is_navigation = True

            if is_navigation:
                msg = f"Disallowed navigation scheme: {scheme} ({url[:60]})"
                logger.warning(msg)
                self.blocked_requests.append({"url": url, "reason": msg})
                await route.abort("blockedbyclient")
                return

            await route.continue_()
            return

        if scheme not in ("http", "https"):
            msg = f"Disallowed scheme: '{scheme}' in request: {url}"
            logger.warning(msg)
            self.blocked_requests.append({"url": url, "reason": msg})
            await route.abort("blockedbyclient")
            return

        # 3. Hostname and DNS resolution (anti DNS rebinding)
        hostname = parsed.hostname
        if not hostname:
            msg = f"Missing hostname in request URL: {url}"
            logger.warning(msg)
            self.blocked_requests.append({"url": url, "reason": msg})
            await route.abort("blockedbyclient")
            return

        port = parsed.port or (443 if scheme == "https" else 80)
        try:
            await asyncio.to_thread(
                resolve_and_validate_host,
                hostname,
                port=port,
                dns_resolver=self.dns_resolver,
                ip_validator=self.ip_validator,
            )
        except BrowserSecurityError as exc:
            msg = f"SSRF security policy blocked request to {url}: {exc}"
            logger.warning(msg)
            self.blocked_requests.append({"url": url, "reason": msg})
            await route.abort("blockedbyclient")
            return
        except Exception as exc:
            msg = f"DNS resolution failed for {url}: {exc}"
            logger.warning(msg)
            self.blocked_requests.append({"url": url, "reason": msg})
            await route.abort("blockedbyclient")
            return

        # All checks passed
        await route.continue_()

    async def check_response(self, response: Any, page: Any = None) -> None:
        """Inspects HTTP responses for redirects and validates destination addresses."""
        status = getattr(response, "status", 0)
        if status in (301, 302, 303, 307, 308):
            headers = getattr(response, "headers", {}) or {}
            location = headers.get("location")
            if location:
                resolved_url = urljoin(getattr(response, "url", ""), location)
                try:
                    await asyncio.to_thread(
                        validate_navigation_url,
                        resolved_url,
                        dns_resolver=self.dns_resolver,
                        ip_validator=self.ip_validator,
                    )
                except BrowserSecurityError as exc:
                    msg = f"Redirect target blocked by security policy: {exc} ({resolved_url})"
                    logger.warning(msg)
                    self.blocked_requests.append({"url": resolved_url, "reason": msg})
                    if page is not None and hasattr(page, "is_closed") and not page.is_closed():
                        try:
                            await page.close()
                        except Exception:
                            pass
