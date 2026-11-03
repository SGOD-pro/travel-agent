"""Nominatim lookup with per-process caching and public-service pacing."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from config.config import get_settings

_RESULT_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_REQUEST_LOCK = asyncio.Lock()
_LAST_REQUEST_AT = 0.0
_CACHE_TTL_SECONDS = 86_400.0


class NominatimGeocoder:
    """Queries OSM Nominatim without exceeding its public-service request rate."""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.NOMINATIM_USER_AGENT:
            raise RuntimeError(
                "NOMINATIM_USER_AGENT must identify the application and provide contact details."
            )
        if urlparse(settings.NOMINATIM_BASE_URL).scheme != "https":
            raise RuntimeError("NOMINATIM_BASE_URL must use HTTPS.")
        self._base_url = settings.NOMINATIM_BASE_URL.rstrip("/")
        self._user_agent = settings.NOMINATIM_USER_AGENT
        self._min_interval = settings.NOMINATIM_MIN_INTERVAL_SECONDS

    async def search(self, query: str) -> list[dict[str, Any]]:
        global _LAST_REQUEST_AT

        normalized_query = " ".join(query.casefold().split())
        cached = _RESULT_CACHE.get(normalized_query)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        async with _REQUEST_LOCK:
            cached = _RESULT_CACHE.get(normalized_query)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            remaining = self._min_interval - (time.monotonic() - _LAST_REQUEST_AT)
            if remaining > 0:
                await asyncio.sleep(remaining)

            _LAST_REQUEST_AT = time.monotonic()
            try:
                async with httpx.AsyncClient(
                    timeout=10,
                    trust_env=False,
                    headers={"User-Agent": self._user_agent},
                ) as client:
                    response = await client.get(
                        f"{self._base_url}/search",
                        params={
                            "q": query,
                            "format": "jsonv2",
                            "addressdetails": "1",
                            "countrycodes": "in",
                            "limit": "3",
                        },
                    )
                    response.raise_for_status()
                    results = response.json()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Nominatim returned HTTP {exc.response.status_code}."
                ) from None
            except httpx.RequestError:
                raise RuntimeError("Nominatim lookup request failed.") from None
            if not isinstance(results, list):
                raise RuntimeError("Nominatim returned an invalid response.")
            if len(_RESULT_CACHE) >= 10_000:
                expired = [
                    key for key, (expires_at, _) in _RESULT_CACHE.items()
                    if expires_at <= time.monotonic()
                ]
                for key in expired:
                    _RESULT_CACHE.pop(key, None)
                if len(_RESULT_CACHE) >= 10_000:
                    _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))
            _RESULT_CACHE[normalized_query] = (
                time.monotonic() + _CACHE_TTL_SECONDS,
                results,
            )
            return results
