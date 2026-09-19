"""Scrapling web extraction adapter for hotel stays and transit schedules.

Implements WebExtractionPort using Scrapling (https://github.com/D4Vinci/Scrapling)
for anti-bot resilient, high-speed page parsing and CSS selector extraction.

Invariants:
- All extractions are labeled INDICATIVE_SEARCH.
- Money is strictly Decimal; unparsed rates are omitted rather than coerced to 0.
- Safe regex price parsing strips currency symbols (₹, Rs, INR) and commas.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from scrapling import Fetcher, Selector

from travel.application.ports.web_extractor import WebExtractionPort
from travel.domain.evidence import EvidenceClass, FareObservation, HotelObservation
from travel.domain.money import Currency
from travel.domain.trips import TransportMode

logger = logging.getLogger(__name__)

# Regex pattern for extracting Indian Rupee numeric amounts
PRICE_PATTERN = re.compile(
    r"(?:₹|Rs\.?|INR)?\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)


def parse_inr_price(price_text: str | None) -> Decimal | None:
    """Safely extracts a Decimal price from currency string, returning None if unparseable."""
    if not price_text:
        return None
    cleaned = price_text.strip()
    match = PRICE_PATTERN.search(cleaned)
    if not match:
        return None
    numeric_str = match.group(1).replace(",", "")
    try:
        val = Decimal(numeric_str)
        return val if val >= Decimal("0") else None
    except (InvalidOperation, ValueError):
        return None


class ScraplingExtractionAdapter(WebExtractionPort):
    """Adapter executing web extraction via Scrapling Fetcher and Selector."""

    def __init__(
        self,
        fetcher: Any | None = None,
        timeout_seconds: int = 15,
        user_agent: str | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._timeout_seconds = timeout_seconds
        self._user_agent = (
            user_agent
            or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )

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

            # Selector searches for standard hotel card containers
            cards = page.css(".hotel-card, .listing-card, [data-testid='hotel-card'], .hotelListingCard")
            if not cards:
                # Fallback to general cards if specialized class missing
                cards = page.css(".card, .property-card")

            for card in cards:
                # Extract hotel name
                name_elem = card.css(".hotel-name, .property-name, h3, h4, .fontBold").first
                name: str | None = None
                if name_elem:
                    raw_text = getattr(name_elem, "text", str(name_elem))
                    if raw_text:
                        name = raw_text.strip()
                if not name or len(name) < 2:
                    continue

                # Extract price
                price_elem = card.css(".price, .price-val, .total-price, .room-rate").first
                price_text: str | None = None
                if price_elem:
                    price_text = getattr(price_elem, "text", str(price_elem))
                price = parse_inr_price(price_text)
                if price is None or price <= Decimal("0"):
                    # Strict non-coercion: never fabricate a zero price
                    continue

                # Extract rating
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

                # Extract link
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
                # Operator Name
                op_elem = card.css(".travels, .operator, .carrier-name, .bus-name").first
                operator: str | None = None
                if op_elem:
                    raw_op = getattr(op_elem, "text", str(op_elem))
                    if raw_op:
                        operator = raw_op.strip()
                if not operator:
                    continue

                # Fare Price
                price_elem = card.css(".fare, .price, .seat-fare").first
                price_text: str | None = None
                if price_elem:
                    price_text = getattr(price_elem, "text", str(price_elem))
                price = parse_inr_price(price_text)
                if price is None or price <= Decimal("0"):
                    continue

                # Timings
                dep_elem = card.css(".dp-time, .departure-time, .start-time").first
                arr_elem = card.css(".bp-time, .arrival-time, .end-time").first
                dep_time = getattr(dep_elem, "text", "08:00 AM").strip() if dep_elem else "08:00 AM"
                arr_time = getattr(arr_elem, "text", "02:00 PM").strip() if arr_elem else "02:00 PM"

                # Deep link
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
            # Perform resilient fetch with Scrapling
            fetch_func = getattr(self._fetcher, "get", Fetcher.get) if self._fetcher else Fetcher.get
            response = fetch_func(
                url,
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout_seconds,
            )
            if response and hasattr(response, "text") and response.text:
                return self.parse_hotel_html(
                    html_content=response.text,
                    destination=destination,
                    source_url=url,
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
            fetch_func = getattr(self._fetcher, "get", Fetcher.get) if self._fetcher else Fetcher.get
            response = fetch_func(
                url,
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout_seconds,
            )
            if response and hasattr(response, "text") and response.text:
                return self.parse_transit_html(
                    html_content=response.text,
                    origin=origin,
                    destination=destination,
                    mode=mode,
                    source_url=url,
                )
        except Exception as e:
            logger.info(
                "Live Scrapling transit query for '%s -> %s' unavailable (%s); failing closed without hallucination",
                origin,
                destination,
                str(e),
            )

        return []
