"""Unit tests for PdfExportService."""

import uuid
from datetime import date
from decimal import Decimal

from travel.application.services.export_service import PdfExportService
from travel.domain.money import BudgetCategory, BudgetLine, BudgetSummary, Currency
from travel.domain.trips import DestinationPoint, TransportMode, TripBrief


def test_pdf_export_basic_brief() -> None:
    trip_id = uuid.uuid4()
    brief = TripBrief(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        destinations=[
            DestinationPoint(name="Mysuru", latitude=12.2958, longitude=76.6394, stay_days=2),
            DestinationPoint(name="Coorg", latitude=12.4244, longitude=75.7382, stay_days=2),
        ],
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 5),
        adults=2,
        rooms=1,
        preferred_modes=[TransportMode.CAR_PETROL],
    )

    pdf_bytes = PdfExportService.generate_pdf(
        trip_id=trip_id,
        version=1,
        brief=brief,
    )

    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 2000


def test_pdf_export_with_schedule_and_budget() -> None:
    trip_id = uuid.uuid4()
    brief = TripBrief(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        destinations=[
            DestinationPoint(name="Mysuru", latitude=12.2958, longitude=76.6394, stay_days=2),
        ],
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 3),
        adults=2,
        rooms=1,
        preferred_modes=[TransportMode.CAR_PETROL],
    )

    schedule = {
        "stops": [
            {
                "position": 0,
                "name": "Bengaluru",
                "arrival_iso": "2026-10-01T09:00:00Z",
                "departure_iso": "2026-10-01T09:30:00Z",
                "stay_hours": 0.5,
            },
            {
                "position": 1,
                "name": "Mysuru",
                "arrival_iso": "2026-10-01T13:00:00Z",
                "departure_iso": "2026-10-03T10:00:00Z",
                "stay_hours": 45.0,
            },
        ],
        "legs": [
            {
                "from_stop": "Bengaluru",
                "to_stop": "Mysuru",
                "distance_km": 145.0,
                "duration_minutes": 210,
            },
        ],
    }

    lines = [
        BudgetLine(
            category=BudgetCategory.TRANSPORT,
            amount=Decimal("990.00"),
            currency=Currency.INR,
            basis="fuel_liters_9.6",
            estimated=True,
        ),
        BudgetLine(
            category=BudgetCategory.TOLLS_AND_FEES,
            amount=None,
            currency=Currency.INR,
            basis="unverified_tolls",
            unknown_reason="Toll tariff unquoted",
        ),
    ]
    summary = BudgetSummary.from_lines(lines, currency=Currency.INR)

    pdf_bytes = PdfExportService.generate_pdf(
        trip_id=trip_id,
        version=1,
        brief=brief,
        schedule=schedule,
        budget_summary=summary.model_dump(mode="json"),
    )

    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 2500
