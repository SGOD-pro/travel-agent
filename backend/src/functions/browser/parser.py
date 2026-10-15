"""Scrapling parser for rendered HTML content conforming to spec 0003 AC-6.

Extracts lodging, transit, and general travel entities from rendered DOM snapshots
using Scrapling CSS selectors without importing functions.evidence_collector.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from scrapling import Selector

logger = logging.getLogger(__name__)

PRICE_PATTERN = re.compile(
    r"(?:₹|Rs\.?|INR)?\s*([0-9]+(?:,[0-9]+)*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)


def parse_inr_price(price_text: str | None) -> Decimal | None:
    """Safely extracts a Decimal price from currency string, returning None if unparseable.

    Strict zero coercion prohibition: missing, negative, or invalid currency strings return None.
    """
    if not price_text:
        return None
    cleaned = price_text.strip()
    if "-" in cleaned:
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


def parse_rendered_entities(
    html_content: str,
    category: str,
    canonical_url: str,
) -> list[dict[str, Any]]:
    """Parses structured domain entities from rendered HTML content via Scrapling."""
    if not html_content or not html_content.strip():
        return []

    try:
        page = Selector(html_content)
    except Exception as exc:
        logger.warning("Scrapling failed to parse HTML for %s: %s", canonical_url, exc)
        return []

    category_lower = category.lower().strip()
    if "lodging" in category_lower or "hotel" in category_lower:
        return _parse_lodging_entities(page, canonical_url)
    elif "transit" in category_lower or "fare" in category_lower or "train" in category_lower or "bus" in category_lower:
        return _parse_transit_entities(page, canonical_url)
    else:
        return _parse_general_entities(page, canonical_url)


def _parse_lodging_entities(page: Selector, canonical_url: str) -> list[dict[str, Any]]:
    """Parses hotel and lodging observation entities."""
    observations: list[dict[str, Any]] = []
    cards = page.css(".hotel-card, .property-card, .listing, .resort-card, [data-testid='hotel-card'], .card")
    if not cards:
        cards = page.css("article, section.property, .item")

    for card in cards:
        title_elem = card.css(".hotel-name, .property-name, .hotel-title, .title, h2, h3, .name").first
        name: str | None = None
        if title_elem:
            raw_title = getattr(title_elem, "text", str(title_elem))
            if raw_title:
                name = raw_title.strip()
        if not name or len(name) < 2:
            continue

        price_elem = card.css(".price, .price-val, .room-rate, .total-price, .rate").first
        price_text: str | None = None
        if price_elem:
            price_text = getattr(price_elem, "text", str(price_elem))
        price = parse_inr_price(price_text)
        if price is None:
            continue

        rating_elem = card.css(".rating, .rating-badge, [data-testid='rating'], .score").first
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

        obs_dict: dict[str, Any] = {
            "property_name": name,
            "price_per_night": str(price),
            "currency": "INR",
        }
        if rating_val is not None:
            obs_dict["rating"] = rating_val
        observations.append(obs_dict)

    return observations


def _parse_transit_entities(page: Selector, canonical_url: str) -> list[dict[str, Any]]:
    """Parses transit and fare observation entities."""
    observations: list[dict[str, Any]] = []
    cards = page.css(".fare-row, .bus-item, .train-item, .schedule-card, .trip-card, [data-testid='bus-card'], tr.fare-entry")
    if not cards:
        cards = page.css(".card, .row")

    for card in cards:
        op_elem = card.css(".operator, .carrier, .travels, .carrier-name, .bus-name, .train-name").first
        operator: str | None = None
        if op_elem:
            raw_op = getattr(op_elem, "text", str(op_elem))
            if raw_op:
                operator = raw_op.strip()
        if not operator:
            continue

        price_elem = card.css(".fare, .price, .seat-fare, .rate").first
        price_text: str | None = None
        if price_elem:
            price_text = getattr(price_elem, "text", str(price_elem))
        price = parse_inr_price(price_text)
        if price is None:
            continue

        dep_elem = card.css(".departure-time, .dp-time, .start-time, .dep").first
        arr_elem = card.css(".arrival-time, .bp-time, .end-time, .arr").first
        dep_time = getattr(dep_elem, "text", "").strip() if dep_elem else None
        arr_time = getattr(arr_elem, "text", "").strip() if arr_elem else None

        obs_dict: dict[str, Any] = {
            "operator_name": operator,
            "price": str(price),
            "currency": "INR",
        }
        if dep_time:
            obs_dict["departure_time"] = dep_time
        if arr_time:
            obs_dict["arrival_time"] = arr_time
        observations.append(obs_dict)

    return observations


def _parse_general_entities(page: Selector, canonical_url: str) -> list[dict[str, Any]]:
    """Parses general destination, attraction, or advisory entities."""
    observations: list[dict[str, Any]] = []
    headings = page.css("h1, h2, h3")
    for heading in headings[:5]:
        text = getattr(heading, "text", "").strip()
        if text and len(text) > 3:
            observations.append({"title": text, "url": canonical_url})
    return observations
