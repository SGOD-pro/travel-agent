"""Unit tests for Scrapling extraction adapter conforming to spec 0002 AC 2 and AC 13.

Verifies:
- Structured text parsing for lodging, transit, festival, and destination pages.
- Safe non zero coercing price parsing (AC 2).
- Dynamic page detection and browser escalation request emission (AC 13).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from contracts.evidence import EvidenceClass
from contracts.trips import TransportMode
from functions.evidence_collector.scrape import (
    ScraplingExtractionAdapter,
    detect_dynamic_rendering_needed,
    parse_inr_price,
)

SAMPLE_HOTEL_HTML = """
<div class="hotelListing">
    <div class="hotel-card">
        <h3 class="hotel-name">The Oberoi Grand</h3>
        <div class="price-val">₹12,500</div>
        <div class="rating-badge">4.8</div>
        <a href="/hotels/oberoi-grand">View Details</a>
    </div>
    <div class="hotel-card">
        <h3 class="hotel-name">Unpriced Heritage Villa</h3>
        <div class="price-val">Price upon request</div>
        <div class="rating-badge">4.2</div>
    </div>
    <div class="hotel-card">
        <h3 class="hotel-name">Invalid Negative Hotel</h3>
        <div class="price-val">₹-500</div>
    </div>
</div>
"""

SAMPLE_TRANSIT_HTML = """
<div class="transitList">
    <div class="bus-item">
        <div class="travels">Greenline Express</div>
        <div class="fare">Rs. 850</div>
        <div class="dp-time">07:00 AM</div>
        <div class="bp-time">01:30 PM</div>
        <a href="/tickets/greenline-850">Book</a>
    </div>
    <div class="bus-item">
        <div class="travels">Unquoted Shuttle</div>
        <div class="fare">Call for quote</div>
    </div>
</div>
"""

SAMPLE_DESTINATION_HTML = """
<div class="destinationsings">
    <div class="attraction-item">
        <h2 class="attraction-name">Dakshineswar Kali Temple</h2>
        <div class="timings">06:00 to 12:30, 15:30 to 20:30</div>
        <div class="entry-fee">Free entry</div>
        <div class="address">Dakshineswar, Kolkata 700076</div>
    </div>
    <div class="attraction-item">
        <h2 class="attraction-name">Victoria Memorial Hall</h2>
        <div class="timings">10:00 to 17:00</div>
        <div class="entry-fee">₹50 for Indian nationals</div>
        <div class="address">1 Queens Way, Kolkata 700071</div>
    </div>
</div>
"""

SAMPLE_FESTIVAL_HTML = """
<div class="advisories">
    <article class="advisory-item">
        <h2 class="advisory-title">Kolkata Traffic Advisory: Durga Puja 2026</h2>
        <div class="timings">Effective 2026-10-18 to 2026-10-24</div>
        <p class="advisory-text">Central Avenue closed for heavy commercial vehicles between 16:00 and 04:00.</p>
    </article>
</div>
"""

SAMPLE_DYNAMIC_NOSCRIPT_HTML = """
<!DOCTYPE html>
<html>
<head><title>Dynamic Portal</title></head>
<body>
    <noscript>You need to enable JavaScript to run this app.</noscript>
    <div id="root"></div>
</body>
</html>
"""

SAMPLE_DYNAMIC_EMPTY_SPA_HTML = """
<!DOCTYPE html>
<html>
<head><title>Single Page Application</title></head>
<body>
    <div id="app"></div>
    <script src="/bundle.js"></script>
</body>
</html>
"""


class TestPriceNonCoercion:
    """Verifies that price parsing rejects unparseable strings rather than coercing to zero."""

    @pytest.mark.parametrize(
        ("raw_price", "expected"),
        [
            ("₹1,250", Decimal("1250")),
            ("Rs. 450.50", Decimal("450.50")),
            ("INR 8000", Decimal("8000")),
            ("1500", Decimal("1500")),
            ("₹ 12,34,567", Decimal("1234567")),
        ],
    )
    def test_valid_prices_parsed(self, raw_price: str, expected: Decimal) -> None:
        assert parse_inr_price(raw_price) == expected

    @pytest.mark.parametrize(
        "invalid_price",
        [
            None,
            "",
            "   ",
            "Price upon request",
            "Call for tariff",
            "Free entry",
            "Not quoted",
            "N/A",
            "-₹500",
        ],
    )
    def test_unparseable_prices_return_none(self, invalid_price: str | None) -> None:
        """Never coerce unquoted, missing, or negative prices to zero (AC 2)."""
        assert parse_inr_price(invalid_price) is None


class TestScraplingExtraction:
    """Tests for structured HTML extraction using Scrapling CSS selectors."""

    def test_hotel_parsing_preserves_only_valid_prices(self) -> None:
        """Verifies that hotels with valid prices are extracted and unpriced items are skipped."""
        adapter = ScraplingExtractionAdapter()
        hotels = adapter.parse_hotel_html(
            SAMPLE_HOTEL_HTML,
            destination="Kolkata",
            source_url="https://example.com/hotels",
        )

        # Only Oberoi Grand has a valid positive price (₹12,500)
        assert len(hotels) == 1
        hotel = hotels[0]
        assert hotel.property_name == "The Oberoi Grand"
        assert hotel.price_per_night == Decimal("12500")
        assert hotel.rating == 4.8
        assert hotel.evidence_class == EvidenceClass.INDICATIVE_SEARCH
        assert hotel.taxes_unknown is True

    def test_transit_parsing_preserves_only_valid_fares(self) -> None:
        """Verifies that transit fares with valid prices are parsed and unquoted items skipped."""
        adapter = ScraplingExtractionAdapter()
        fares = adapter.parse_transit_html(
            SAMPLE_TRANSIT_HTML,
            origin="Kolkata",
            destination="Durgapur",
            mode=TransportMode.BUS,
        )

        assert len(fares) == 1
        fare = fares[0]
        assert fare.operator_name == "Greenline Express"
        assert fare.price == Decimal("850")
        assert fare.departure_time == "07:00 AM"
        assert fare.arrival_time == "01:30 PM"
        assert fare.evidence_class == EvidenceClass.INDICATIVE_SEARCH

    def test_destination_parsing_extracts_attractions_and_fees(self) -> None:
        """Verifies that destination attractions and entry fees are parsed properly (AC 2)."""
        adapter = ScraplingExtractionAdapter()
        obs = adapter.parse_destination_html(
            SAMPLE_DESTINATION_HTML,
            source_url="https://kolkatatourism.gov.in/attractions",
        )

        assert len(obs) == 2
        # First observation: Dakshineswar (free entry -> not coerced to zero Decimal price)
        obs1 = obs[0]
        assert "Dakshineswar Kali Temple" in obs1.excerpt_text
        assert "06:00 to 12:30" in obs1.excerpt_text
        assert obs1.publisher == "kolkatatourism.gov.in"
        assert obs1.extraction_method == "SCRAPLING_HTTP"

        # Second observation: Victoria Memorial (₹50 fee)
        obs2 = obs[1]
        assert "Victoria Memorial Hall" in obs2.excerpt_text
        assert "Fee: INR 50" in obs2.excerpt_text
        assert obs2.artifact_ref is not None
        assert obs2.artifact_ref["fee_inr"] == "50"

    def test_festival_parsing_extracts_advisories_and_closures(self) -> None:
        """Verifies that festival road closures and traffic advisories are parsed (AC 2)."""
        adapter = ScraplingExtractionAdapter()
        obs = adapter.parse_festival_html(
            SAMPLE_FESTIVAL_HTML,
            source_url="https://kolkatatrafficpolice.gov.in/advisory",
        )

        assert len(obs) == 1
        advisory = obs[0]
        assert "Kolkata Traffic Advisory" in advisory.excerpt_text
        assert "Central Avenue closed" in advisory.excerpt_text
        assert advisory.extraction_method == "SCRAPLING_HTTP"


class TestDynamicBrowserEscalation:
    """Verifies that dynamic pages requiring JavaScript emit browser_requests (AC 13)."""

    def test_detect_dynamic_rendering_with_noscript(self) -> None:
        """Verifies detection of noscript warning tags."""
        needed, reason = detect_dynamic_rendering_needed(SAMPLE_DYNAMIC_NOSCRIPT_HTML)
        assert needed is True
        assert reason == "NOSCRIPT_JAVASCRIPT_REQUIRED"

    def test_detect_dynamic_rendering_with_empty_spa_root(self) -> None:
        """Verifies detection of empty client side SPA containers with minimal text."""
        needed, reason = detect_dynamic_rendering_needed(SAMPLE_DYNAMIC_EMPTY_SPA_HTML)
        assert needed is True
        assert reason == "EMPTY_SPA_ROOT_CONTAINER"

    def test_static_html_does_not_require_browser(self) -> None:
        """Verifies that regular static content does not trigger dynamic browser escalation."""
        needed, reason = detect_dynamic_rendering_needed(SAMPLE_DESTINATION_HTML)
        assert needed is False
        assert reason is None

    @pytest.mark.asyncio
    async def test_extract_page_emits_browser_request_without_running_browser(self) -> None:
        """Verifies AC 13: placeholder observation recorded and browser_requests emitted without browser execution."""
        adapter = ScraplingExtractionAdapter()
        target_url = "https://dynamic.makemytrip.com/hotels/dynamic-spa"

        observations, browser_requests = await adapter.extract_page(
            target_url,
            category="lodging",
            html_override=SAMPLE_DYNAMIC_NOSCRIPT_HTML,
        )

        # 1. Emits a browser_request
        assert len(browser_requests) == 1
        req = browser_requests[0]
        assert req["url"] == target_url
        assert req["reason"] == "NOSCRIPT_JAVASCRIPT_REQUIRED"

        # 2. Returns placeholder observation flagged with NEEDS_BROWSER
        assert len(observations) == 1
        placeholder = observations[0]
        assert placeholder.extraction_method == "NEEDS_BROWSER"
        assert "[DYNAMIC_PAGE: JAVASCRIPT_REQUIRED]" in placeholder.excerpt_text
        assert placeholder.canonical_url == target_url

    def test_static_page_with_spa_root_and_paragraphs_not_misclassified(self) -> None:
        """Verifies Minor 1: short static advisory with substantive paragraph is not falsely flagged as SPA."""
        static_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Advisory</title></head>
        <body>
            <div id="root"></div>
            <article>
                <p>Public notification: Kalighat temple complex opens daily at 05:00 AM for devotees.</p>
            </article>
        </body>
        </html>
        """
        needed, reason = detect_dynamic_rendering_needed(static_html)
        assert needed is False
        assert reason is None

    def test_transit_parsing_empty_fallback_times_when_missing(self) -> None:
        """Verifies Nit 5: missing departure and arrival times fall back to empty string, not hardcoded times."""
        html = """
        <div class="bus-item">
            <div class="travels">Express Coach</div>
            <div class="fare">₹600</div>
        </div>
        """
        adapter = ScraplingExtractionAdapter()
        fares = adapter.parse_transit_html(html, origin="Kolkata", destination="Digha")
        assert len(fares) == 1
        assert fares[0].departure_time == ""
        assert fares[0].arrival_time == ""

    def test_hotel_parsing_logs_debug_on_skipped_unparseable_card(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verifies Minor 4: skipped hotel cards with unparseable prices produce debug logs."""
        import logging
        html = """
        <div class="hotel-card">
            <h3 class="hotel-name">Mystic Heritage</h3>
            <div class="price">Tariff on application</div>
        </div>
        """
        adapter = ScraplingExtractionAdapter()
        with caplog.at_level(logging.DEBUG):
            hotels = adapter.parse_hotel_html(html, destination="Kolkata")

        assert len(hotels) == 0
        assert any(
            "Mystic Heritage" in record.message and "skipped: unparseable price" in record.message
            for record in caplog.records
        )
