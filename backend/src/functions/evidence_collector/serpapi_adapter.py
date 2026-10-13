"""SerpAPI search adapter conforming to spec 0002 AC 1.

Queries SerpAPI for organic search and place results.
Logs provider response latency and minor unit spend while enforcing timeouts and rate limits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from config.config import settings
from functions.evidence_collector.fixtures import (
    RawObservation,
    compute_content_hash,
    normalize_url,
)

logger = logging.getLogger(__name__)

# Standard minor unit cost per SerpAPI search query (50 paise in INR).
DEFAULT_SERPAPI_COST_MINOR: int = 50


def _safe_url_for_logging(url: str | httpx.URL) -> str:
    """Masks secret parameters such as api_key from URL representations for logging."""
    try:
        parsed = httpx.URL(str(url))
        if "api_key" in parsed.params:
            params = dict(parsed.params)
            params["api_key"] = "[REDACTED]"
            return str(parsed.copy_with(params=params))
        return str(parsed)
    except Exception:
        return "[URL_REDACTED]"


class SerpApiAdapterError(Exception):
    """Base exception for SerpAPI adapter failures."""


class SerpApiRateLimitError(SerpApiAdapterError):
    """Raised when SerpAPI returns HTTP 429 rate limit exceeded."""


class SerpApiTimeoutError(SerpApiAdapterError):
    """Raised when SerpAPI query times out."""


class SerpApiAdapter:
    """HTTP client adapter for querying SerpAPI organic search and places."""

    is_live_provider: bool = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        cost_per_query_minor: int = DEFAULT_SERPAPI_COST_MINOR,
        min_request_interval_seconds: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or settings.SERPAPI_API_KEY
        self._base_url = (base_url or settings.SERPAPI_BASE_URL).rstrip("/")
        self._timeout_seconds = timeout_seconds or float(settings.HTTP_REQUEST_TIMEOUT_SECONDS)
        self._cost_per_query_minor = cost_per_query_minor
        self._min_request_interval = min_request_interval_seconds
        self._client = client
        self._last_request_time: float = 0.0
        self._rate_limit_lock = asyncio.Lock()

        # Call tracking metrics
        self.call_count: int = 0
        self.total_cost_minor: int = 0
        self.last_latency_ms: float = 0.0

    async def _throttle_if_needed(self) -> None:
        """Enforces minimum interval between consecutive outgoing requests."""
        async with self._rate_limit_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._min_request_interval:
                await asyncio.sleep(self._min_request_interval - elapsed)
            self._last_request_time = time.monotonic()

    def _extract_publisher(self, url: str) -> str:
        """Extracts domain publisher identifier from a URL."""
        parsed = urlparse(url)
        return parsed.netloc.lower() or "google.com"

    def _parse_organic_results(
        self,
        organic_items: list[dict[str, Any]],
        retrieved_at: datetime,
    ) -> list[RawObservation]:
        """Maps SerpAPI organic search items to normalized RawObservation instances."""
        observations: list[RawObservation] = []
        for idx, item in enumerate(organic_items):
            link = str(item.get("link") or "").strip()
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or "").strip()

            if not link or not (title or snippet):
                continue

            excerpt_text = f"{title}: {snippet}" if snippet else title
            publisher = self._extract_publisher(link)
            canonical_link = normalize_url(link)
            content_hash = compute_content_hash(excerpt_text)

            artifact_metadata = {
                "position": item.get("position", idx + 1),
                "title": title,
                "displayed_link": item.get("displayed_link"),
            }

            observations.append(
                RawObservation(
                    source_url=link,
                    canonical_url=canonical_link,
                    publisher=publisher,
                    content_hash=content_hash,
                    retrieved_at=retrieved_at,
                    excerpt_text=excerpt_text,
                    excerpt_locator=f"serpapi:organic[{idx}].snippet",
                    extraction_method="SERPAPI_SEARCH",
                    artifact_ref=artifact_metadata,
                )
            )
        return observations

    def _parse_place_results(
        self,
        places_data: Any,
        retrieved_at: datetime,
    ) -> list[RawObservation]:
        """Maps SerpAPI local or places items to normalized RawObservation instances."""
        observations: list[RawObservation] = []
        place_items: list[dict[str, Any]] = []

        if isinstance(places_data, list):
            place_items = places_data
        elif isinstance(places_data, dict):
            place_items = places_data.get("places", []) or places_data.get("local_results", [])

        for idx, place in enumerate(place_items):
            title = str(place.get("title") or "").strip()
            address = str(place.get("address") or "").strip()
            rating = place.get("rating")
            reviews = place.get("reviews")
            gps = place.get("gps_coordinates") or {}

            if not title:
                continue

            details_parts: list[str] = [f"Place: {title}"]
            if address:
                details_parts.append(f"Address: {address}")
            if rating is not None:
                details_parts.append(f"Rating: {rating}")
            if reviews is not None:
                details_parts.append(f"Reviews: {reviews}")

            excerpt_text = " | ".join(details_parts)
            place_link = (
                place.get("link")
                or place.get("website")
                or f"https://www.google.com/maps/search/?api=1&query={title.replace(' ', '+')}"
            )
            canonical_link = normalize_url(place_link)
            publisher = self._extract_publisher(place_link)
            content_hash = compute_content_hash(excerpt_text)

            artifact_metadata = {
                "place_id": place.get("place_id"),
                "data_id": place.get("data_id"),
                "gps": gps,
                "rating": rating,
                "reviews": reviews,
            }

            observations.append(
                RawObservation(
                    source_url=place_link,
                    canonical_url=canonical_link,
                    publisher=publisher,
                    content_hash=content_hash,
                    retrieved_at=retrieved_at,
                    excerpt_text=excerpt_text,
                    excerpt_locator=f"serpapi:places[{idx}]",
                    extraction_method="SERPAPI_SEARCH",
                    artifact_ref=artifact_metadata,
                )
            )
        return observations

    async def search(
        self,
        query: str,
        limit: int = 5,
        *,
        engine: str = "google",
        location: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> list[RawObservation]:
        """Executes search query via SerpAPI and returns normalized RawObservation records.

        Enforces timeouts, tracks latency in milliseconds, increments minor unit costs,
        and parses organic and place search outputs.
        """
        if not self._api_key:
            raise SerpApiAdapterError(
                "SERPAPI_API_KEY is not configured; live search requires valid credentials"
            )

        params: dict[str, Any] = {
            "engine": engine,
            "q": query,
            "api_key": self._api_key,
            "num": limit,
            **(extra_params or {}),
        }
        if location:
            params["location"] = location

        url = f"{self._base_url}/search"
        retrieved_at = datetime.now(UTC)

        await self._throttle_if_needed()

        should_close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_seconds)
            should_close_client = True

        start_time = time.perf_counter()
        try:
            response = await client.get(url, params=params)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            self.last_latency_ms = elapsed_ms

            if response.status_code == 429:
                logger.warning("SerpAPI rate limit reached (HTTP 429) for query '%s'", query)
                raise SerpApiRateLimitError("SerpAPI rate limit exceeded (HTTP 429)")

            if response.status_code >= 400:
                clean_text = response.text[:200]
                if self._api_key:
                    clean_text = clean_text.replace(self._api_key, "[REDACTED]")
                logger.error(
                    "SerpAPI returned HTTP error %d: %s",
                    response.status_code,
                    clean_text,
                )
                raise SerpApiAdapterError(
                    f"SerpAPI query failed with status code {response.status_code}: {clean_text}"
                )

            data = response.json()
            self.call_count += 1
            self.total_cost_minor += self._cost_per_query_minor

            logger.info(
                "SerpAPI query '%s' succeeded (status %d) in %.2fms, charged %d minor units",
                query,
                response.status_code,
                elapsed_ms,
                self._cost_per_query_minor,
            )

            results: list[RawObservation] = []

            # 1. Parse organic results
            organic_raw = data.get("organic_results", [])
            if isinstance(organic_raw, list):
                results.extend(self._parse_organic_results(organic_raw, retrieved_at))

            # 2. Parse place or local results
            local_raw = data.get("local_results") or data.get("places_results")
            if local_raw:
                results.extend(self._parse_place_results(local_raw, retrieved_at))

            return results[:limit]

        except httpx.TimeoutException as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            self.last_latency_ms = elapsed_ms
            logger.warning(
                "SerpAPI query '%s' timed out after %.2fms (limit %.1fs)",
                query,
                elapsed_ms,
                self._timeout_seconds,
            )
            raise SerpApiTimeoutError(
                f"SerpAPI request timed out after {self._timeout_seconds}s"
            ) from e

        except SerpApiAdapterError:
            raise

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            self.last_latency_ms = elapsed_ms
            clean_err = str(e)
            if self._api_key:
                clean_err = clean_err.replace(self._api_key, "[REDACTED]")
            logger.exception("Unexpected error executing SerpAPI query '%s': %s", query, clean_err)
            raise SerpApiAdapterError(f"SerpAPI query error: {clean_err}") from e

        finally:
            if should_close_client:
                await client.aclose()
