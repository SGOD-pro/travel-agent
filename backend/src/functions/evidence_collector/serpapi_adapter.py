"""SerpAPI search adapter conforming to spec 0002 AC 1.

Queries SerpAPI for organic search and place results.
Logs provider response latency and minor unit spend while enforcing timeouts and rate limits.
"""

from __future__ import annotations

import asyncio
import logging
import re
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


def _redact_text(text: str) -> str:
    """Redacts api_key parameter from string values."""
    if "api_key=" in text:
        return re.sub(r"api_key=[^&\s\'\"]+", "api_key=[REDACTED]", text)
    return text


def _redact_arg(arg: Any) -> Any:
    """Redacts api_key parameter from individual log record arguments."""
    if isinstance(arg, str):
        return _redact_text(arg)
    if isinstance(arg, httpx.URL):
        return _safe_url_for_logging(arg)
    if isinstance(arg, dict):
        sanitized_dict = {}
        for k, v in arg.items():
            if str(k).lower() in ("api_key", "token", "secret", "password", "authorization"):
                sanitized_dict[k] = "[REDACTED]"
            else:
                sanitized_dict[k] = _redact_arg(v)
        return sanitized_dict
    if isinstance(arg, (list, tuple)):
        sanitized_seq = [_redact_arg(x) for x in arg]
        return tuple(sanitized_seq) if isinstance(arg, tuple) else sanitized_seq
    arg_str = str(arg)
    if "api_key=" in arg_str:
        return _redact_text(arg_str)
    return arg


def _sanitize_exception(
    exc: BaseException | None,
    api_key: str | None = None,
    visited: set[int] | None = None,
) -> None:
    """Sanitizes exception arguments, request URLs, and chained causes to prevent credential leaks."""
    if exc is None:
        return

    if visited is None:
        visited = set()
    exc_id = id(exc)
    if exc_id in visited:
        return
    visited.add(exc_id)

    # 1. Sanitize exception args
    if hasattr(exc, "args") and exc.args:
        sanitized_args: list[Any] = []
        for a in exc.args:
            if isinstance(a, str):
                s = _redact_text(a)
                if api_key and api_key in s:
                    s = s.replace(api_key, "[REDACTED]")
                sanitized_args.append(s)
            elif isinstance(a, httpx.URL):
                sanitized_args.append(_safe_url_for_logging(a))
            else:
                s = str(a)
                if "api_key=" in s or (api_key and api_key in s):
                    s = _redact_text(s)
                    if api_key and api_key in s:
                        s = s.replace(api_key, "[REDACTED]")
                    sanitized_args.append(s)
                else:
                    sanitized_args.append(a)
        try:
            exc.args = tuple(sanitized_args)
        except Exception:
            pass

    # 2. Sanitize request URL if attached to exception
    req = getattr(exc, "request", None)
    if req is not None and hasattr(req, "url"):
        try:
            req.url = httpx.URL(_safe_url_for_logging(req.url))
        except Exception:
            pass

    # 3. Recurse into cause and context with cycle protection
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        _sanitize_exception(cause, api_key, visited)

    context = getattr(exc, "__context__", None)
    if context is not None and context is not exc:
        _sanitize_exception(context, api_key, visited)


class CredentialRedactionFilter(logging.Filter):
    """Redacts api_key and other secret tokens from log records, checking msg, args, and exc_info."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_text(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        if record.exc_info and len(record.exc_info) >= 2 and record.exc_info[1] is not None:
            _sanitize_exception(record.exc_info[1])
        if record.exc_text:
            record.exc_text = _redact_text(record.exc_text)
        return True


def configure_http_client_logging() -> None:
    """Configures credential redaction and restricts verbose request logging from HTTP client libraries."""
    for lib_name in ("httpx", "httpcore", __name__, "functions.evidence_collector.serpapi_adapter"):
        lib_logger = logging.getLogger(lib_name)
        if not any(isinstance(f, CredentialRedactionFilter) for f in lib_logger.filters):
            lib_logger.addFilter(CredentialRedactionFilter())
        if lib_name in ("httpx", "httpcore"):
            lib_logger.setLevel(logging.WARNING)


class SerpApiAdapterError(Exception):
    """Base exception for SerpAPI adapter failures."""


class SerpApiRateLimitError(SerpApiAdapterError):
    """Raised when SerpAPI returns HTTP 429 rate limit exceeded."""


class SerpApiTimeoutError(SerpApiAdapterError):
    """Raised when SerpAPI query times out."""


def apply_query_metadata(
    artifact_metadata: dict[str, Any],
    *,
    query_purpose: str | None = None,
    query_dest: str | None = None,
    selected_engine: str | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Apply query purpose, destination, and engine metadata to artifact ref."""
    if query_purpose:
        if overwrite or "query_purpose" not in artifact_metadata:
            artifact_metadata["query_purpose"] = query_purpose
        if overwrite or "purpose" not in artifact_metadata:
            artifact_metadata["purpose"] = query_purpose
        if query_purpose == "advisory":
            if overwrite or "category" not in artifact_metadata:
                artifact_metadata["category"] = "advisory"
    if query_dest:
        if overwrite or "destination" not in artifact_metadata:
            artifact_metadata["destination"] = query_dest
    if selected_engine:
        if overwrite or "engine" not in artifact_metadata:
            artifact_metadata["engine"] = selected_engine
    return artifact_metadata


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
        default_engine: str | None = None,
    ) -> None:
        self._api_key = api_key or settings.SERPAPI_API_KEY
        self._base_url = (base_url or settings.SERPAPI_BASE_URL).rstrip("/")
        self._timeout_seconds = timeout_seconds or float(settings.HTTP_REQUEST_TIMEOUT_SECONDS)
        self._cost_per_query_minor = cost_per_query_minor
        self._min_request_interval = min_request_interval_seconds
        self._client = client
        self._default_engine = default_engine
        self._last_request_time: float = 0.0
        self._rate_limit_lock = asyncio.Lock()

        # Call tracking metrics
        self.call_count: int = 0
        self.total_cost_minor: int = 0
        self.last_latency_ms: float = 0.0

        configure_http_client_logging()

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
        *,
        query_purpose: str | None = None,
        query_dest: str | None = None,
        selected_engine: str | None = None,
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

            artifact_metadata: dict[str, Any] = {
                "position": item.get("position", idx + 1),
                "title": title,
                "displayed_link": item.get("displayed_link"),
            }
            apply_query_metadata(
                artifact_metadata,
                query_purpose=query_purpose,
                query_dest=query_dest,
                selected_engine=selected_engine,
            )

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
        *,
        query_purpose: str | None = None,
        query_dest: str | None = None,
        selected_engine: str | None = None,
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

            artifact_metadata: dict[str, Any] = {
                "place_id": place.get("place_id"),
                "data_id": place.get("data_id"),
                "title": title,
                "address": address,
                "gps": gps,
                "gps_coordinates": gps,
                "rating": rating,
                "reviews": reviews,
            }
            apply_query_metadata(
                artifact_metadata,
                query_purpose=query_purpose,
                query_dest=query_dest,
                selected_engine=selected_engine,
            )

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

        query_purpose = (extra_params or {}).get("purpose") or (extra_params or {}).get(
            "query_purpose"
        )
        if query_purpose:
            query_purpose = str(query_purpose).strip().lower()

        query_dest = (extra_params or {}).get("destination")

        # Explicit purpose determines the permitted search engine
        if query_purpose in ("advisory", "hours"):
            # Road travel advisory and hours research route to web search (engine="google").
            selected_engine = "google"
        elif query_purpose in ("attractions", "dining", "lodging"):
            # Venue discovery MUST route to place search (engine="google_maps").
            # Reject or safely normalize venue + google and unsupported values.
            selected_engine = "google_maps"
        else:
            # Fallback when purpose is missing or unsupported
            passed_engine = (
                (extra_params or {}).get("engine")
                or (self._default_engine if engine == "google" else engine)
                or "google"
            )
            selected_engine = (
                passed_engine if passed_engine in ("google", "google_maps") else "google"
            )
        serpapi_param_keys = {
            "location",
            "hl",
            "gl",
            "google_domain",
            "start",
            "device",
            "safe",
        }
        filtered_extra = {k: v for k, v in (extra_params or {}).items() if k in serpapi_param_keys}
        params: dict[str, Any] = {
            **filtered_extra,
            "engine": selected_engine,
            "q": query,
            "api_key": self._api_key,
            "num": limit,
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

            # 1. Parse place or local results first to capture structured POIs with verified coordinates
            local_raw = data.get("local_results") or data.get("places_results")
            if local_raw:
                results.extend(
                    self._parse_place_results(
                        local_raw,
                        retrieved_at,
                        query_purpose=query_purpose,
                        query_dest=query_dest,
                        selected_engine=selected_engine,
                    )
                )

            # 2. Parse organic results to capture web articles and multi-publisher consensus citations
            organic_raw = data.get("organic_results", [])
            if isinstance(organic_raw, list):
                results.extend(
                    self._parse_organic_results(
                        organic_raw,
                        retrieved_at,
                        query_purpose=query_purpose,
                        query_dest=query_dest,
                        selected_engine=selected_engine,
                    )
                )

            return results[:limit]

        except httpx.TimeoutException as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            self.last_latency_ms = elapsed_ms
            _sanitize_exception(e, self._api_key)
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
            _sanitize_exception(e, self._api_key)
            clean_err = _redact_text(str(e))
            if self._api_key:
                clean_err = clean_err.replace(self._api_key, "[REDACTED]")
            logger.exception("Unexpected error executing SerpAPI query '%s': %s", query, clean_err)
            raise SerpApiAdapterError(f"SerpAPI query error: {clean_err}") from e

        finally:
            if should_close_client:
                await client.aclose()
