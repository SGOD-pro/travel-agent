"""Bounded direct-media downloads with an explicit hostname allowlist."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import unquote, urlsplit

import httpx

from config.config import get_settings

ALLOWED_MEDIA_TYPES = ("image/", "video/", "audio/")


async def _resolve_public_host(hostname: str) -> None:
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            None,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ValueError("Media host could not be resolved.") from exc

    if not addresses:
        raise ValueError("Media host did not resolve to an address.")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("Media host resolves to a non-public address.")


async def download_direct_media(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bytes, str]:
    """Fetch one public media resource from an explicitly permitted hostname."""
    settings = get_settings()
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 80, 443)
    ):
        raise ValueError("Media URL must be a public HTTP(S) URL without credentials.")

    hostname = parsed.hostname.rstrip(".").lower()
    allowed_hosts = {host.rstrip(".").lower() for host in settings.MEDIA_ALLOWED_HOSTS}
    if not allowed_hosts:
        raise RuntimeError("MEDIA_ALLOWED_HOSTS must be configured before media downloads.")
    if hostname not in allowed_hosts:
        raise ValueError("Media URL hostname is not on the configured allowlist.")
    await _resolve_public_host(hostname)

    timeout = httpx.Timeout(settings.MEDIA_DOWNLOAD_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"User-Agent": "SWENA-Travel-Agent/0.1"},
        ) as client:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    raise ValueError(
                        "Media URL redirects are not supported; use its direct file URL."
                    )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise RuntimeError(
                        f"Media server returned HTTP {exc.response.status_code}."
                    ) from None
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if not content_type.startswith(ALLOWED_MEDIA_TYPES):
                    raise ValueError("URL response is not an image, video, or audio media file.")

                content_length = response.headers.get("content-length")
                maximum_bytes = 50 * 1024 * 1024
                if content_length and int(content_length) > maximum_bytes:
                    raise ValueError("Media download exceeds the 50 MiB size limit.")

                chunks: list[bytes] = []
                total_bytes = 0
                async for chunk in response.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > maximum_bytes:
                        raise ValueError("Media download exceeds the 50 MiB size limit.")
                    chunks.append(chunk)
    except httpx.RequestError:
        raise RuntimeError("Media server request failed.") from None

    if total_bytes == 0:
        raise ValueError("Media URL returned an empty file.")

    filename = unquote(parsed.path).replace("\\", "/").rsplit("/", 1)[-1] or "media"
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:120].strip(".")
    filename = filename or "media"
    return b"".join(chunks), filename
