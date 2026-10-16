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


async def _pipe_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Relays data bidirectionally between stream endpoints until EOF or error."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


class SecureEgressProxy:
    """In process asynchronous HTTP/HTTPS egress security proxy.

    Guarantees destination and connection safety:
    1. Intercepts all Chromium outbound connections before any socket connects to destination hosts.
    2. Resolves destination hostnames and validates that EVERY resolved IP is public and allowed.
    3. Explicitly binds outbound TCP sockets to the validated IP address literal, eliminating the
       validation to connection gap and defeating DNS rebinding.
    4. Immediately rejects disallowed destinations (private IPs, loopback, carrier grade NAT,
       and AWS metadata 169.254.169.254) with HTTP 403 Forbidden without transmitting external traffic.
    5. Covers navigation, redirects, subresources, and WebSocket upgrade handshakes.
    """

    def __init__(
        self,
        dns_resolver: Callable[[str, int], list[Any]] | None = None,
        ip_validator: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool] | None = None,
        port: int = 0,
    ) -> None:
        self.dns_resolver = dns_resolver
        self.ip_validator = ip_validator
        self.port = port
        self.server: asyncio.Server | None = None
        self.blocked_requests: list[dict[str, str]] = []
        self._active_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> int:
        """Starts the egress proxy server and returns the allocated port."""
        self.server = await asyncio.start_server(
            self._handle_client,
            "127.0.0.1",
            self.port,
        )
        self.port = self.server.sockets[0].getsockname()[1]
        logger.info("SecureEgressProxy started on 127.0.0.1:%d", self.port)
        return self.port

    async def stop(self) -> None:
        """Stops the egress proxy and terminates active connections."""
        if self._active_tasks:
            for task in list(self._active_tasks):
                task.cancel()
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
            self._active_tasks.clear()

        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        logger.info("SecureEgressProxy stopped on 127.0.0.1:%d", self.port)

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """Handles an incoming proxy connection from Chromium."""
        task = asyncio.current_task()
        if task:
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

        try:
            line = await client_reader.readline()
            if not line:
                client_writer.close()
                return

            parts = line.decode("latin1", errors="replace").strip().split()
            if len(parts) < 2:
                client_writer.close()
                return

            method, target = parts[0].upper(), parts[1]

            # Read request headers until empty line
            headers: list[bytes] = []
            has_ws_upgrade = False
            while True:
                h = await client_reader.readline()
                if not h or h in (b"\r\n", b"\n"):
                    break
                h_lower = h.lower()
                if b"upgrade" in h_lower and b"websocket" in h_lower:
                    has_ws_upgrade = True
                headers.append(h)

            # Block WebSocket upgrade handshakes at the egress boundary
            if has_ws_upgrade:
                msg = f"Egress proxy blocked WebSocket upgrade request: {target}"
                logger.warning(msg)
                self.blocked_requests.append({"url": target, "reason": msg})
                client_writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n\r\nWebSockets are disabled\r\n"
                )
                await client_writer.drain()
                client_writer.close()
                return

            if method == "CONNECT":
                # HTTPS tunnel
                if ":" in target:
                    host, port_str = target.split(":", 1)
                    try:
                        port = int(port_str)
                    except ValueError:
                        port = 443
                else:
                    host, port = target, 443
                is_https = True
            else:
                # HTTP request
                parsed = urlparse(target)
                host = parsed.hostname or target
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                is_https = False

            # Validate host and resolve all candidate IPs
            try:
                allowed_ips = await asyncio.to_thread(
                    resolve_and_validate_host,
                    host,
                    port=port,
                    dns_resolver=self.dns_resolver,
                    ip_validator=self.ip_validator,
                )
            except BrowserSecurityError as exc:
                msg = f"Egress proxy blocked connection to {target}: {exc}"
                logger.warning(msg)
                self.blocked_requests.append({"url": target, "reason": msg})
                client_writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n\r\nEgress blocked by security policy\r\n"
                )
                await client_writer.drain()
                client_writer.close()
                return
            except Exception as exc:
                msg = f"Egress proxy DNS resolution failed for {target}: {exc}"
                logger.warning(msg)
                self.blocked_requests.append({"url": target, "reason": msg})
                client_writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\n\r\nDNS resolution failed\r\n"
                )
                await client_writer.drain()
                client_writer.close()
                return

            # Bind outbound connection strictly to a validated IP literal with multi homed fallback
            remote_reader: asyncio.StreamReader | None = None
            remote_writer: asyncio.StreamWriter | None = None
            last_conn_exc: Exception | None = None
            for candidate_ip in allowed_ips:
                try:
                    remote_reader, remote_writer = await asyncio.open_connection(
                        host=candidate_ip,
                        port=port,
                    )
                    break
                except Exception as exc:
                    last_conn_exc = exc
                    logger.debug("Failed connecting to validated candidate IP %s:%d: %s", candidate_ip, port, exc)

            if remote_reader is None or remote_writer is None:
                logger.debug("Failed connecting to all validated IPs %s:%d: %s", allowed_ips, port, last_conn_exc)
                client_writer.write(b"HTTP/1.1 504 Gateway Timeout\r\n\r\n")
                await client_writer.drain()
                client_writer.close()
                return

            if is_https:
                # Establish tunnel for HTTPS
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()
                await asyncio.gather(
                    _pipe_stream(client_reader, remote_writer),
                    _pipe_stream(remote_reader, client_writer),
                )
            else:
                # Forward plain HTTP request
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                req_line = f"{method} {path} HTTP/1.1\r\n".encode("latin1")
                remote_writer.write(req_line)
                for h in headers:
                    remote_writer.write(h)
                remote_writer.write(b"\r\n")
                await remote_writer.drain()
                await asyncio.gather(
                    _pipe_stream(client_reader, remote_writer),
                    _pipe_stream(remote_reader, client_writer),
                )
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            logger.debug("Error handling proxy client: %s", exc)
        finally:
            try:
                client_writer.close()
            except Exception:
                pass

