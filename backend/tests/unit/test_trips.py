"""Unit tests for TripBrief, Trip aggregate, and concurrency invariants."""

import uuid
from datetime import date

import pytest
from pydantic import ValidationError

from travel.domain.trips import (
    DestinationPoint,
    TransportMode,
    Trip,
    TripBrief,
    TripState,
)


def test_trip_brief_valid_days_calculation() -> None:
    brief = TripBrief(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        destinations=[
            DestinationPoint(name="Mysuru", latitude=12.2958, longitude=76.6394, stay_days=2)
        ],
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 5),
        adults=2,
        preferred_modes=[TransportMode.CAR_PETROL],
    )
    # Oct 1 to Oct 5 inclusive is 5 calendar days
    assert brief.total_days == 5
    assert brief.adults == 2
    assert len(brief.destinations) == 1


def test_trip_brief_invalid_date_range() -> None:
    with pytest.raises(ValidationError, match="end_date .* cannot precede start_date"):
        TripBrief(
            origin_name="Delhi",
            origin_lat=28.6139,
            origin_lng=77.2090,
            destinations=[DestinationPoint(name="Agra", latitude=27.1767, longitude=78.0081)],
            start_date=date(2026, 11, 10),
            end_date=date(2026, 11, 5),
        )


def test_trip_brief_empty_destinations_rejected() -> None:
    with pytest.raises(ValidationError):
        TripBrief(
            origin_name="Delhi",
            origin_lat=28.6139,
            origin_lng=77.2090,
            destinations=[],
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 5),
        )


def test_trip_aggregate_version_advance_success() -> None:
    owner = uuid.uuid4()
    trip = Trip(owner_id=owner, current_version=1, state=TripState.DRAFT)

    new_v = trip.advance_version(expected_base_version=1)
    assert new_v == 2
    assert trip.current_version == 2


def test_trip_aggregate_version_conflict_rejected() -> None:
    owner = uuid.uuid4()
    trip = Trip(owner_id=owner, current_version=3, state=TripState.DRAFT)

    # Trying to advance from an outdated base version 2 raises ValueError
    with pytest.raises(ValueError, match="Version conflict"):
        trip.advance_version(expected_base_version=2)
