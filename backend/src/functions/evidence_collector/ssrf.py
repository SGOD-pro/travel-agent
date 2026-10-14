"""SSRF protection and defensive URL validation conforming to spec 0002 AC 18.

Blocks requests to loopback, link local, carrier grade NAT, and RFC 1918 private IP addresses.
Validates target addresses both prior to initial dispatch and across each HTTP redirect hop.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

# Carrier grade NAT network (RFC 6598: 100.64.0.0/10)
_CARRIER_GRADE_NAT = ipaddress.ip_network("100.64.0.0/10")

# Default limits
DEFAULT_MAX_REDIRECTS: int = 3
DEFAULT_MAX_PAYLOAD_BYTES: int = 5_242_880  # 5 MB
DEFAULT_TIMEOUT_SECONDS: float = 20.0


class SSRFSecurityError(Exception):
    """Raised when an address resolves to a restricted network range or disallowed scheme."""


class SSRFPayloadSizeExceededError(SSRFSecurityError):
    """Raised when response body exceeds maximum permitted byte size."""


def is_ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Returns True if the IP address is globally routable and public.

    Rejects loopback, link local, private, carrier grade NAT, multicast,
    unspecified, and reserved ranges.
    """
    # Unpack IPv4 mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    return not (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _CARRIER_GRADE_NAT)
    )


def validate_hostname_or_ip(
    hostname: str,
    port: int | None = None,
    *,
    dns_resolver: Any | None = None,
) -> list[str]:
    """Resolves hostname and verifies that all resolved IP addresses are public and safe.

    Returns the list of safe resolved IP addresses. Raises SSRFSecurityError if any address
    violates SSRF security policies.
    """
    cleaned_host = hostname.strip().strip("[]")
    if not cleaned_host:
        raise SSRFSecurityError("Host name cannot be empty")

    # Fast path: direct IP literal
    try:
        ip_obj = ipaddress.ip_address(cleaned_host)
        if not is_ip_allowed(ip_obj):
            raise SSRFSecurityError(
                f"Direct IP address {cleaned_host} is in a restricted or private range"
            )
        return [str(ip_obj)]
    except ValueError:
        # Host is a domain name requiring DNS resolution
        pass

    resolved_ips: list[str] = []
    # Port is only used by getaddrinfo for address info struct resolution;
    # DNS hostname address lookup itself is port independent. Defaults to 80.
    effective_port = port or 80

    try:
        if dns_resolver is not None:
            # Test hook or custom resolver
            addr_info = dns_resolver(cleaned_host, effective_port)
        else:
            addr_info = socket.getaddrinfo(
                cleaned_host,
                effective_port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
    except socket.gaierror as e:
        raise SSRFSecurityError(f"DNS resolution failed for host {cleaned_host}: {e}") from e

    for _family, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as err:
            raise SSRFSecurityError(f"Invalid resolved IP address {ip_str}") from err

        if not is_ip_allowed(ip_obj):
            raise SSRFSecurityError(
                f"Host {cleaned_host} resolved to restricted IP {ip_str}"
            )
        resolved_ips.append(ip_str)

    if not resolved_ips:
        raise SSRFSecurityError(f"Host {cleaned_host} resolved to zero IP addresses")

    return resolved_ips


def validate_url_ssrf(
    url: str,
    *,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
    dns_resolver: Any | None = None,
) -> str:
    """Validates URL scheme and resolves target hostname against SSRF blocklists.

    Returns the validated URL. Raises SSRFSecurityError on forbidden schemes or private IP ranges.
    """
    if not url or not isinstance(url, str):
        raise SSRFSecurityError("URL must be a non empty string")

    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes:
        raise SSRFSecurityError(
            f"Scheme '{scheme}' is not permitted; allowed schemes are {allowed_schemes}"
        )

    if not parsed.hostname:
        raise SSRFSecurityError(f"URL '{url}' does not contain a valid hostname")

    validate_hostname_or_ip(
        parsed.hostname,
        port=parsed.port,
        dns_resolver=dns_resolver,
    )
    return url


async def safe_fetch_page(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    dns_resolver: Any | None = None,
) -> tuple[str, str, int]:
    """Fetches a web page with strict SSRF checks before initial connect and across redirects.

    Follows redirects manually up to max_redirects hops.
    Validates every redirect target URL before following it.
    Streams body content and aborts if payload exceeds max_payload_bytes.

    Returns a tuple of (content_text, final_effective_url, status_code).
    """
    current_url = url.strip()
    effective_headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36 SWENA/1.0"
        ),
        **(headers or {}),
    }

    should_close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        should_close_client = True

    try:
        redirects_followed = 0

        while True:
            # Validate URL before making request
            validate_url_ssrf(current_url, dns_resolver=dns_resolver)

            # Stream response to guard memory and payload size
            response = await client.get(
                current_url,
                headers=effective_headers,
                follow_redirects=False,
            )

            # Check for redirect status codes (301, 302, 303, 307, 308)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise SSRFSecurityError(
                        f"Redirect response from {current_url} missing Location header"
                    )

                redirects_followed += 1
                if redirects_followed > max_redirects:
                    raise SSRFSecurityError(
                        f"Redirect limit exceeded ({max_redirects} hops) for {url}"
                    )

                current_url = urljoin(current_url, location.strip())
                continue

            # Read response body under payload size limits
            content_bytes = bytearray()
            async for chunk in response.aiter_bytes():
                content_bytes.extend(chunk)
                if len(content_bytes) > max_payload_bytes:
                    raise SSRFPayloadSizeExceededError(
                        f"Payload size exceeded limit of {max_payload_bytes} bytes for {current_url}"
                    )

            # Decode using apparent encoding or UTF 8 fallback
            try:
                decoded_text = content_bytes.decode(response.encoding or "utf-8", errors="replace")
            except Exception:
                decoded_text = content_bytes.decode("utf-8", errors="replace")

            return decoded_text, str(response.url), response.status_code

    finally:
        if should_close_client:
            await client.aclose()
