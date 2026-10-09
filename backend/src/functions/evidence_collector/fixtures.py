"""Test only fixture adapters for evidence collection conforming to spec 0002.

These adapters provide canned search and page extraction results for offline testing.
They are strictly test only and must never be used as production fallbacks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def compute_content_hash(text: str) -> str:
    """Computes SHA256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Exact-match set of well-known tracking parameters to strip during URL normalization.
# Avoid prefix matching on 'source' or 'ref': legitimate params like
# source_id or reference would be stripped, causing distinct pages to share
# the same canonical URL and collide under the snapshot uniqueness constraint.
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "utm_id",
        "fbclid",
        "gclid",
        "msclkid",
        "ttclid",
        "twclid",
    }
)


def normalize_url(url: str) -> str:
    """Normalizes URL by lowercasing scheme/host, removing tracking query params and fragments."""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    query_items = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(query_items)

    path = parsed.path
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")

    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


@dataclass(frozen=True)
class RawObservation:
    """Immutable representation of an unpersisted observation extracted by an adapter."""

    source_url: str
    canonical_url: str
    publisher: str
    content_hash: str
    retrieved_at: datetime
    excerpt_text: str
    excerpt_locator: str
    extraction_method: str = "MANUAL_FIXTURE"
    published_at: datetime | None = None
    artifact_ref: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        *,
        source_url: str,
        publisher: str,
        excerpt_text: str,
        excerpt_locator: str = "fixture:body",
        extraction_method: str = "MANUAL_FIXTURE",
        retrieved_at: datetime | None = None,
        published_at: datetime | None = None,
        artifact_ref: dict[str, Any] | None = None,
        content_hash: str | None = None,
        canonical_url: str | None = None,
    ) -> RawObservation:
        """Helper creating a RawObservation with automatic URL normalization and hashing."""
        norm_url = canonical_url if canonical_url is not None else normalize_url(source_url)
        computed_hash = content_hash if content_hash is not None else compute_content_hash(excerpt_text)
        return cls(
            source_url=source_url,
            canonical_url=norm_url,
            publisher=publisher,
            content_hash=computed_hash,
            retrieved_at=retrieved_at or datetime.now(UTC),
            published_at=published_at,
            artifact_ref=artifact_ref,
            excerpt_text=excerpt_text,
            excerpt_locator=excerpt_locator,
            extraction_method=extraction_method,
        )


class FixtureSearchAdapter:
    """Test only fixture search adapter returning canned observations.

    Strictly test only: must never be injected as a production fallback.
    """

    def __init__(self, fixtures: list[RawObservation] | None = None) -> None:
        self.is_test_fixture = True
        self._fixtures = list(fixtures) if fixtures is not None else self._default_fixtures()
        self.call_count = 0

    @staticmethod
    def _default_fixtures() -> list[RawObservation]:
        now = datetime.now(UTC)
        return [
            RawObservation.create(
                source_url="https://kolkatatrafficpolice.gov.in/advisory2026?utm_source=feed",
                publisher="kolkatatrafficpolice.gov.in",
                excerpt_text="Central Avenue closed for heavy goods vehicles from 16:00 to 04:00 during Puja.",
                excerpt_locator="article.advisory p.traffic",
                extraction_method="MANUAL_FIXTURE",
                retrieved_at=now,
            ),
            RawObservation.create(
                source_url="https://dakshineswartemple.org/timings",
                publisher="dakshineswartemple.org",
                excerpt_text="Temple gates open at 06:00 and close at 12:30. Evening darshan 15:30 to 20:30.",
                excerpt_locator="section#timings div.darshan",
                extraction_method="MANUAL_FIXTURE",
                retrieved_at=now,
            ),
        ]

    async def search(self, query: str, limit: int = 5) -> list[RawObservation]:
        """Returns canned fixture observations matching query keywords or first default items."""
        self.call_count += 1
        query_lower = query.lower()
        matched = [
            f for f in self._fixtures
            if any(term in f.excerpt_text.lower() or term in f.source_url.lower() for term in query_lower.split())
        ]
        results = matched if matched else self._fixtures
        return results[:limit]


class FixtureWebExtractionAdapter:
    """Test only fixture page extraction adapter returning canned text excerpts.

    Strictly test only: must never be injected as a production fallback.
    """

    def __init__(self, fixtures_by_url: dict[str, RawObservation] | None = None) -> None:
        self.is_test_fixture = True
        self._fixtures = dict(fixtures_by_url) if fixtures_by_url is not None else {}
        self.call_count = 0

    async def extract(self, url: str) -> RawObservation | None:
        """Returns canned observation for the given URL if available."""
        self.call_count += 1
        norm_url = normalize_url(url)
        return self._fixtures.get(norm_url) or self._fixtures.get(url)
