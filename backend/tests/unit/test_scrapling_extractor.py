"""Unit tests for Scrapling web extraction adapter and evidence models.

Verifies:
1. Anti-hallucination: Indicative search observations never coerced to LIVE_OFFER.
2. Money precision: String prices parsed safely into Decimal; zero/negative rejected.
3. Selector extraction: Hotel and bus HTML structures parsed via Scrapling.
4. Fail-closed resilience: Network failures return empty lists without fabricating data.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from travel.domain.evidence import EvidenceClass, FareObservation, HotelObservation
from travel.domain.trips import TransportMode
from travel.infrastructure.scraping.scrapling_adapter import (
    ScraplingExtractionAdapter,
    parse_inr_price,
)


def test_parse_inr_price_precision() -> None:
    """Tests Decimal extraction across various Indian currency formatting conventions."""
    assert parse_inr_price("₹4,500") == Decimal("4500")
    assert parse_inr_price("INR 12,345.50") == Decimal("12345.50")
    assert parse_inr_price("Rs. 999") == Decimal("999")
    assert parse_inr_price("₹ 3,450 / night") == Decimal("3450")
    assert parse_inr_price("Free") is None
    assert parse_inr_price(None) is None
    assert parse_inr_price("Price on request") is None


def test_scrapling_hotel_html_parsing() -> None:
    """Tests Scrapling CSS selector extraction of hotel stay observations."""
    sample_html = """
    <html>
      <body>
        <div class="hotel-card">
          <h3 class="hotel-name">Madikeri Heritage Homestay</h3>
          <span class="price-val">₹3,850</span>
          <span class="rating">4.7</span>
          <a href="/hotels/madikeri-heritage">View Deal</a>
        </div>
        <div class="hotel-card">
          <h3 class="hotel-name">Coorg Plantation Resort</h3>
          <span class="price-val">₹6,200</span>
          <span class="rating">4.9</span>
          <a href="https://example.com/resort">Book</a>
        </div>
        <!-- Card with missing price must be skipped to avoid zero-coercion -->
        <div class="hotel-card">
          <h3 class="hotel-name">Unquoted Estate</h3>
          <span class="price-val">Contact property</span>
        </div>
      </body>
    </html>
    """

    adapter = ScraplingExtractionAdapter()
    observations = adapter.parse_hotel_html(
        html_content=sample_html,
        destination="Coorg",
        provider="makemytrip_scrapling_candidate",
    )

    assert len(observations) == 2

    # Verification of First Observation
    obs1 = observations[0]
    assert isinstance(obs1, HotelObservation)
    assert obs1.property_name == "Madikeri Heritage Homestay"
    assert obs1.price_per_night == Decimal("3850")
    assert obs1.rating == 4.7
    assert obs1.location_name == "Coorg"
    assert obs1.evidence_class == EvidenceClass.INDICATIVE_SEARCH
    assert obs1.taxes_unknown is True
    assert obs1.taxes_included is False
    assert obs1.deep_link == "https://www.makemytrip.com/hotels/madikeri-heritage"

    # Verification of Second Observation
    obs2 = observations[1]
    assert obs2.property_name == "Coorg Plantation Resort"
    assert obs2.price_per_night == Decimal("6200")
    assert obs2.rating == 4.9


def test_scrapling_transit_html_parsing() -> None:
    """Tests Scrapling CSS selector extraction of bus transit fares."""
    sample_html = """
    <html>
      <body>
        <div class="bus-item">
          <div class="operator">KSRTC Airavat Club Class</div>
          <span class="fare">₹820</span>
          <span class="dp-time">07:15 AM</span>
          <span class="bp-time">01:30 PM</span>
          <a href="/bus/ksrtc-blr-coorg">Select Seat</a>
        </div>
        <div class="bus-item">
          <div class="operator">Greenline Travels Volvo Multi-Axle</div>
          <span class="fare">₹1,050</span>
          <span class="dp-time">10:00 PM</span>
          <span class="bp-time">05:00 AM</span>
          <a href="/bus/greenline-coorg">Select Seat</a>
        </div>
      </body>
    </html>
    """

    adapter = ScraplingExtractionAdapter()
    fares = adapter.parse_transit_html(
        html_content=sample_html,
        origin="Bengaluru",
        destination="Madikeri",
        mode=TransportMode.BUS,
        provider="redbus_scrapling_candidate",
    )

    assert len(fares) == 2

    f1 = fares[0]
    assert isinstance(f1, FareObservation)
    assert f1.operator_name == "KSRTC Airavat Club Class"
    assert f1.price == Decimal("820")
    assert f1.departure_time == "07:15 AM"
    assert f1.arrival_time == "01:30 PM"
    assert f1.origin == "Bengaluru"
    assert f1.destination == "Madikeri"
    assert f1.mode == TransportMode.BUS
    assert f1.evidence_class == EvidenceClass.INDICATIVE_SEARCH


@pytest.mark.asyncio
async def test_scrapling_fail_closed_when_offline() -> None:
    """Tests that network failures or timeouts return empty lists rather than fabricating prices."""
    mock_fetcher = MagicMock()
    mock_fetcher.get.side_effect = ConnectionError("Network unreachable")

    adapter = ScraplingExtractionAdapter(fetcher=mock_fetcher)

    # Hotel extraction should fail-closed
    hotels = await adapter.extract_hotel_observations(
        destination="Mysuru",
        checkin_date=date(2026, 10, 1),
        checkout_date=date(2026, 10, 3),
    )
    assert hotels == []

    # Transit extraction should fail-closed
    transit = await adapter.extract_transit_observations(
        origin="Bengaluru",
        destination="Mysuru",
        travel_date=date(2026, 10, 1),
        mode=TransportMode.BUS,
    )
    assert transit == []


def test_malformed_html_does_not_crash() -> None:
    """Verifies that completely broken HTML gracefully yields empty observations."""
    adapter = ScraplingExtractionAdapter()
    broken_html = "<<<not valid html at all><<<>>>>"
    assert adapter.parse_hotel_html(broken_html, "Jaipur") == []
    assert adapter.parse_transit_html(broken_html, "Delhi", "Jaipur") == []
