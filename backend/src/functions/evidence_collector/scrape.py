"""Scrapling web extraction adapter for lodging, transit, festival, and destination pages.

Implements WebExtractionPort and raw page extraction using Scrapling CSS selectors.
Enforces SSRF validation on target addresses and detects pages requiring JavaScript (AC 13).

Invariants:
- All extractions are labeled INDICATIVE_SEARCH.
- Money is strictly Decimal; unparsed rates are omitted or marked unknown rather than coerced to 0.
- Safe regex price parsing strips currency symbols (₹, Rs, INR) and commas.
- Dynamic pages requiring JavaScript emit browser_requests without running headless browser.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from scrapling import Selector

from contracts.evidence import EvidenceClass, FareObservation, HotelObservation
from contracts.money import Currency
from contracts.trips import TransportMode
from functions.evidence_collector.fixtures import (
    RawObservation,
    compute_content_hash,
    normalize_url,
)
from functions.evidence_collector.ssrf import safe_fetch_page

logger = logging.getLogger(__name__)

# Regex pattern for extracting Indian Rupee numeric amounts
PRICE_PATTERN = re.compile(
    r"(?:₹|Rs\.?|INR)?\s*([0-9]+(?:,[0-9]+)*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)

# Patterns signaling that a web page requires JavaScript execution to render content
_JS_REQUIRED_PATTERNS = [
    re.compile(r"enable\s+javascript", re.IGNORECASE),
    re.compile(r"requires\s+javascript", re.IGNORECASE),
    re.compile(r"javascript\s+is\s+required", re.IGNORECASE),
    re.compile(r"need\s+to\s+enable\s+javascript", re.IGNORECASE),
    re.compile(r"please\s+turn\s+on\s+javascript", re.IGNORECASE),
    re.compile(r"checking\s+your\s+browser", re.IGNORECASE),
    re.compile(r"cf-browser-verification", re.IGNORECASE),
]


class WebExtractionPort(ABC):
    """Abstract port for web extraction of lodging and transit observations."""

    @abstractmethod
    async def extract_hotel_observations(
        self,
        destination: str,
        checkin_date: date,
        checkout_date: date,
        rooms: int = 1,
    ) -> list[HotelObservation]:
        """Extract hotel stay rate observations for a destination and date range."""
        ...

    @abstractmethod
    async def extract_transit_observations(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        mode: TransportMode,
    ) -> list[FareObservation]:
        """Extract transit schedule and fare observations between two points."""
        ...


def parse_inr_price(price_text: str | None) -> Decimal | None:
    """Safely extracts a Decimal price from currency string, returning None if unparseable.

    Strict zero coercion prohibition: missing, negative, or invalid currency strings return None.
    """
    if not price_text:
        return None
    cleaned = price_text.strip()
    if "-" in cleaned:
        # Negative prices are invalid for travel rates and fees
        return None
    match = PRICE_PATTERN.search(cleaned)
    if not match:
        return None
    numeric_str = match.group(1).replace(",", "")
    try:
        val = Decimal(numeric_str)
        return val if val > Decimal("0") else None
    except (InvalidOperation, ValueError):
        return None


def detect_dynamic_rendering_needed(html_content: str) -> tuple[bool, str | None]:
    """Inspects HTML to determine if dynamic JavaScript rendering is required (AC 13).

    Checks for noscript warnings, empty client side app containers with sparse body text,
    and anti bot challenge pages.
    """
    if not html_content or not html_content.strip():
        return False, None

    # 1. Check for noscript messages requiring JavaScript
    page = Selector(html_content)
    noscript_elements = page.css("noscript")
    for elem in noscript_elements:
        raw_text = getattr(elem, "text", str(elem))
        if raw_text:
            for pattern in _JS_REQUIRED_PATTERNS:
                if pattern.search(raw_text):
                    return True, "NOSCRIPT_JAVASCRIPT_REQUIRED"

    # 2. Check for anti bot challenge phrases in raw HTML
    for pattern in _JS_REQUIRED_PATTERNS:
        if pattern.search(html_content[:2000]):
            return True, "BROWSER_VERIFICATION_REQUIRED"

    # 3. Check for empty client app roots with minimal rendered text.
    # A single page app root container (e.g. #root, #app) without hydrated text indicates
    # JavaScript execution is needed. To avoid misclassifying short static pages (e.g. minimal
    # advisory notices), we require:
    #   a) The presence of an identifiable SPA root element container
    #   b) Total body text below 150 characters
    #   c) The absence of substantive narrative elements (<p>, <article>, <section>) with real text
    has_spa_root = bool(
        page.css("#root, #app, #__next, div[data-reactroot], app-root")
    )
    if has_spa_root:
        body_elem = page.css("body").first
        body_text = getattr(body_elem, "text", "").strip() if body_elem else ""
        # Strip excessive whitespace
        clean_text = " ".join(body_text.split())
        if len(clean_text) < 150:
            content_containers = page.css("p, article, section")
            has_substantive_text = any(
                len(" ".join((getattr(c, "text", "") or "").split())) >= 30
                for c in content_containers
            )
            if not has_substantive_text:
                return True, "EMPTY_SPA_ROOT_CONTAINER"

    return False, None


class ScraplingExtractionAdapter(WebExtractionPort):
    """Adapter executing web extraction via Scrapling CSS selectors and SSRF safe fetcher."""

    is_live_provider: bool = True

    def __init__(
        self,
        fetcher: Any | None = None,
        timeout_seconds: int = 20,
        user_agent: str | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._timeout_seconds = timeout_seconds
        self._user_agent = (
            user_agent
            or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36 SWENA/1.0"
        )
        self.browser_requests: list[dict[str, Any]] = []

    def _extract_publisher(self, url: str) -> str:
        parsed = urlparse(url)
        return parsed.netloc.lower() or "unknown"

    def parse_hotel_html(
        self,
        html_content: str,
        destination: str,
        provider: str = "makemytrip_scrapling_candidate",
        source_url: str = "https://www.makemytrip.com/hotels/",
    ) -> list[HotelObservation]:
        """Parses lodging listings from HTML using Scrapling CSS selectors."""
        observations: list[HotelObservation] = []
        try:
            page = Selector(html_content)

            cards = page.css(".hotel-card, .listing-card, [data-testid='hotel-card'], .hotelListingCard")
            if not cards:
                cards = page.css(".card, .property-card")

            for card in cards:
                name_elem = card.css(".hotel-name, .property-name, h3, h4, .fontBold").first
                name: str | None = None
                if name_elem:
                    raw_text = getattr(name_elem, "text", str(name_elem))
                    if raw_text:
                        name = raw_text.strip()
                if not name or len(name) < 2:
                    continue

                price_elem = card.css(".price, .price-val, .total-price, .room-rate").first
                price_text: str | None = None
                if price_elem:
                    price_text = getattr(price_elem, "text", str(price_elem))
                price = parse_inr_price(price_text)
                if price is None or price <= Decimal("0"):
                    # Strict non coercion: never fabricate a zero price
                    logger.debug(
                        "Scrapling hotel card '%s' skipped: unparseable price %r",
                        name,
                        price_text,
                    )
                    continue

                rating_elem = card.css(".rating, .rating-badge, [data-testid='rating']").first
                rating_val: float | None = None
                if rating_elem:
                    raw_rating = getattr(rating_elem, "text", str(rating_elem))
                    if raw_rating:
                        clean_rating = re.search(r"([0-5](?:\.[0-9])?)", raw_rating.strip())
                        if clean_rating:
                            try:
                                rating_val = float(clean_rating.group(1))
                            except (ValueError, TypeError):
                                rating_val = None

                link_elem = card.css("a").first
                deep_link = source_url
                if link_elem and hasattr(link_elem, "attrib"):
                    href = link_elem.attrib.get("href")
                    if href:
                        deep_link = href if href.startswith("http") else f"https://www.makemytrip.com{href}"

                observations.append(
                    HotelObservation(
                        property_name=name,
                        location_name=destination,
                        price_per_night=price,
                        currency=Currency.INR,
                        rating=rating_val,
                        evidence_class=EvidenceClass.INDICATIVE_SEARCH,
                        provider=provider,
                        source_url=source_url,
                        observed_at=datetime.now(UTC),
                        deep_link=deep_link,
                        taxes_included=False,
                        taxes_unknown=True,
                    )
                )
        except Exception as e:
            logger.warning("Scrapling hotel extraction failed to parse content: %s", str(e))

        return observations

    def parse_transit_html(
        self,
        html_content: str,
        origin: str,
        destination: str,
        mode: TransportMode = TransportMode.BUS,
        provider: str = "redbus_scrapling_candidate",
        source_url: str = "https://www.redbus.in/bus-tickets/",
    ) -> list[FareObservation]:
        """Parses transit fare listings from HTML using Scrapling CSS selectors."""
        observations: list[FareObservation] = []
        try:
            page = Selector(html_content)

            cards = page.css(".bus-item, .fare-row, .trip-card, [data-testid='bus-card']")
            if not cards:
                cards = page.css(".card, tr.fare-entry")

            for card in cards:
                op_elem = card.css(".travels, .operator, .carrier-name, .bus-name").first
                operator: str | None = None
                if op_elem:
                    raw_op = getattr(op_elem, "text", str(op_elem))
                    if raw_op:
                        operator = raw_op.strip()
                if not operator:
                    continue

                price_elem = card.css(".fare, .price, .seat-fare").first
                price_text: str | None = None
                if price_elem:
                    price_text = getattr(price_elem, "text", str(price_elem))
                price = parse_inr_price(price_text)
                if price is None or price <= Decimal("0"):
                    continue

                dep_elem = card.css(".dp-time, .departure-time, .start-time").first
                arr_elem = card.css(".bp-time, .arrival-time, .end-time").first
                dep_time = getattr(dep_elem, "text", "").strip() if dep_elem else ""
                arr_time = getattr(arr_elem, "text", "").strip() if arr_elem else ""

                link_elem = card.css("a").first
                deep_link = source_url
                if link_elem and hasattr(link_elem, "attrib"):
                    href = link_elem.attrib.get("href")
                    if href:
                        deep_link = href if href.startswith("http") else f"https://www.redbus.in{href}"

                observations.append(
                    FareObservation(
                        origin=origin,
                        destination=destination,
                        operator_name=operator,
                        departure_time=dep_time,
                        arrival_time=arr_time,
                        price=price,
                        currency=Currency.INR,
                        mode=mode,
                        evidence_class=EvidenceClass.INDICATIVE_SEARCH,
                        provider=provider,
                        source_url=source_url,
                        observed_at=datetime.now(UTC),
                        deep_link=deep_link,
                    )
                )
        except Exception as e:
            logger.warning("Scrapling transit extraction failed to parse content: %s", str(e))

        return observations

    def parse_destination_html(
        self,
        html_content: str,
        source_url: str,
        retrieved_at: datetime | None = None,
    ) -> list[RawObservation]:
        """Parses destination attractions and places of interest using CSS selectors (AC 2)."""
        observations: list[RawObservation] = []
        now = retrieved_at or datetime.now(UTC)
        canonical_url = normalize_url(source_url)
        publisher = self._extract_publisher(source_url)

        try:
            page = Selector(html_content)
            items = page.css(
                ".attraction-item, .poi-card, .place-card, article.destination, "
                "section.place, .attraction-card, .listing-item"
            )
            if not items:
                items = page.css(".card, article")

            for idx, item in enumerate(items):
                name_elem = item.css(".attraction-name, .place-name, h2, h3, .title").first
                name: str | None = None
                if name_elem:
                    raw_text = getattr(name_elem, "text", str(name_elem))
                    if raw_text:
                        name = raw_text.strip()
                if not name or len(name) < 2:
                    continue

                hours_elem = item.css(".timings, .opening-hours, .hours, .schedule").first
                hours = getattr(hours_elem, "text", "").strip() if hours_elem else ""

                fee_elem = item.css(".fee, .entry-fee, .ticket-price, .cost, .price").first
                fee_text = getattr(fee_elem, "text", "").strip() if fee_elem else ""
                parsed_fee = parse_inr_price(fee_text)

                addr_elem = item.css(".address, .location, .locality").first
                address = getattr(addr_elem, "text", "").strip() if addr_elem else ""

                parts = [f"Attraction: {name}"]
                if hours:
                    parts.append(f"Hours: {hours}")
                if parsed_fee is not None:
                    parts.append(f"Fee: INR {parsed_fee}")
                elif fee_text:
                    parts.append(f"Fee Notice: {fee_text}")
                if address:
                    parts.append(f"Location: {address}")

                excerpt = " | ".join(parts)
                content_hash = compute_content_hash(excerpt)

                artifact_metadata: dict[str, Any] = {
                    "name": name,
                    "hours": hours or None,
                    "address": address or None,
                    "fee_inr": str(parsed_fee) if parsed_fee is not None else None,
                }

                observations.append(
                    RawObservation(
                        source_url=source_url,
                        canonical_url=canonical_url,
                        publisher=publisher,
                        content_hash=content_hash,
                        retrieved_at=now,
                        excerpt_text=excerpt,
                        excerpt_locator=f"css:.attraction-item[{idx}]",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref=artifact_metadata,
                    )
                )
        except Exception as e:
            logger.warning("Scrapling destination extraction failed to parse content: %s", str(e))

        return observations

    def parse_festival_html(
        self,
        html_content: str,
        source_url: str,
        retrieved_at: datetime | None = None,
    ) -> list[RawObservation]:
        """Parses festival schedules and road advisories using CSS selectors (AC 2)."""
        observations: list[RawObservation] = []
        now = retrieved_at or datetime.now(UTC)
        canonical_url = normalize_url(source_url)
        publisher = self._extract_publisher(source_url)

        try:
            page = Selector(html_content)
            items = page.css(
                ".festival-item, .advisory-item, .event-card, article.advisory, "
                ".traffic-update, .schedule-item, .notice-item"
            )
            if not items:
                items = page.css(".card, article, section")

            for idx, item in enumerate(items):
                title_elem = item.css(".event-name, .advisory-title, h2, h3, .headline").first
                title: str | None = None
                if title_elem:
                    raw_text = getattr(title_elem, "text", str(title_elem))
                    if raw_text:
                        title = raw_text.strip()
                if not title or len(title) < 2:
                    continue

                timing_elem = item.css(".timings, .event-dates, .schedule, .timing").first
                timing = getattr(timing_elem, "text", "").strip() if timing_elem else ""

                notice_elem = item.css(".restrictions, .closure, .advisory-text, p.notice, p").first
                notice = getattr(notice_elem, "text", "").strip() if notice_elem else ""

                parts = [f"Notice: {title}"]
                if timing:
                    parts.append(f"Timing: {timing}")
                if notice:
                    parts.append(f"Details: {notice}")

                excerpt = " | ".join(parts)
                content_hash = compute_content_hash(excerpt)

                observations.append(
                    RawObservation(
                        source_url=source_url,
                        canonical_url=canonical_url,
                        publisher=publisher,
                        content_hash=content_hash,
                        retrieved_at=now,
                        excerpt_text=excerpt,
                        excerpt_locator=f"css:.advisory-item[{idx}]",
                        extraction_method="SCRAPLING_HTTP",
                        artifact_ref={"title": title, "timing": timing, "notice": notice},
                    )
                )
        except Exception as e:
            logger.warning("Scrapling festival extraction failed to parse content: %s", str(e))

        return observations

    async def extract_page(
        self,
        url: str,
        *,
        category: str = "general",
        html_override: str | None = None,
        dns_resolver: Any | None = None,
    ) -> tuple[list[RawObservation], list[dict[str, Any]]]:
        """Fetches page under strict SSRF checks, parses structure, or flags browser needs (AC 13, AC 18).

        Returns a tuple of (extracted_observations, browser_requests).
        """
        retrieved_at = datetime.now(UTC)
        canonical_url = normalize_url(url)
        publisher = self._extract_publisher(url)

        # 1. Fetch HTML safely under SSRF protections if not overridden
        if html_override is not None:
            html_content = html_override
            effective_url = url
        else:
            # safe_fetch_page validates SSRF internally on initial request and each redirect hop
            html_content, effective_url, _ = await safe_fetch_page(
                url,
                timeout_seconds=self._timeout_seconds,
                dns_resolver=dns_resolver,
            )

        # 2. Check for dynamic JavaScript rendering requirement (AC 13)
        needs_browser, browser_reason = detect_dynamic_rendering_needed(html_content)
        if needs_browser:
            logger.info(
                "Page at %s requires JavaScript rendering (%s); emitting browser request",
                url,
                browser_reason,
            )
            browser_request = {
                "url": url,
                "canonical_url": canonical_url,
                "reason": browser_reason,
                "category": category,
                "requested_at": retrieved_at.isoformat(),
            }
            self.browser_requests.append(browser_request)

            # Record placeholder observation flagged for browser execution
            placeholder_text = (
                f"[DYNAMIC_PAGE: JAVASCRIPT_REQUIRED] Dynamic rendering required for {url} ({browser_reason})"
            )
            placeholder_hash = compute_content_hash(placeholder_text)
            placeholder_obs = RawObservation(
                source_url=effective_url,
                canonical_url=canonical_url,
                publisher=publisher,
                content_hash=placeholder_hash,
                retrieved_at=retrieved_at,
                excerpt_text=placeholder_text,
                excerpt_locator="html",
                extraction_method="NEEDS_BROWSER",
                artifact_ref={"browser_reason": browser_reason},
            )
            return [placeholder_obs], [browser_request]

        # 3. Static extraction by category
        if category in ("destination", "poi", "attraction"):
            obs = self.parse_destination_html(html_content, effective_url, retrieved_at)
            if obs:
                return obs, []

        elif category in ("festival", "traffic", "advisory", "restriction"):
            obs = self.parse_festival_html(html_content, effective_url, retrieved_at)
            if obs:
                return obs, []

        # Fallback to general page text extraction
        page = Selector(html_content)
        main_elem = page.css("main, article, .content, #content, body").first
        raw_text = getattr(main_elem, "text", "") if main_elem else ""
        clean_text = " ".join(raw_text.split())[:1000]

        if not clean_text:
            return [], []

        obs = [
            RawObservation(
                source_url=effective_url,
                canonical_url=canonical_url,
                publisher=publisher,
                content_hash=compute_content_hash(clean_text),
                retrieved_at=retrieved_at,
                excerpt_text=clean_text,
                excerpt_locator="main:text",
                extraction_method="SCRAPLING_HTTP",
            )
        ]
        return obs, []

    async def extract_hotel_observations(
        self,
        destination: str,
        checkin_date: date,
        checkout_date: date,
        rooms: int = 1,
    ) -> list[HotelObservation]:
        """Queries permitted web target using Scrapling fetcher or falls back cleanly."""
        url = f"https://www.makemytrip.com/hotels/{destination.lower().replace(' ', '-')}-hotels.html"
        try:
            # Perform safe SSRF fetch
            html_content, effective_url, _ = await safe_fetch_page(
                url,
                timeout_seconds=self._timeout_seconds,
            )
            return self.parse_hotel_html(
                html_content=html_content,
                destination=destination,
                source_url=effective_url,
            )
        except Exception as e:
            logger.info(
                "Live Scrapling hotel query for '%s' unavailable (%s); failing closed without hallucination",
                destination,
                str(e),
            )
            return []

    async def extract_transit_observations(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        mode: TransportMode = TransportMode.BUS,
    ) -> list[FareObservation]:
        """Queries permitted transit target using Scrapling fetcher or falls back cleanly."""
        route_slug = f"{origin.lower()}-to-{destination.lower()}"
        url = f"https://www.redbus.in/bus-tickets/{route_slug}"
        try:
            html_content, effective_url, _ = await safe_fetch_page(
                url,
                timeout_seconds=self._timeout_seconds,
            )
            return self.parse_transit_html(
                html_content=html_content,
                origin=origin,
                destination=destination,
                mode=mode,
                source_url=effective_url,
            )
        except Exception as e:
            logger.info(
                "Live Scrapling transit query for '%s -> %s' unavailable (%s); failing closed without hallucination",
                origin,
                destination,
                str(e),
            )
            return []


# Alias maintained for backwards compatibility with existing collector imports
ScraplingWebExtractionAdapter = ScraplingExtractionAdapter
