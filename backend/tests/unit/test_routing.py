"""Unit tests for Road transit math, fuel calculations, and unverified tolls."""

from decimal import Decimal

from travel.domain.money import BudgetCategory, Currency
from travel.domain.routing import RoadBudgetCalculator, RoadSegment, VehicleFuelProfile
from travel.domain.trips import TransportMode


def test_car_fuel_budget_calculation() -> None:
    # 300km at 15 km/l = 20 liters. 20 liters * 100 INR/liter = 2000 INR
    profile = VehicleFuelProfile(
        mode=TransportMode.CAR_PETROL,
        mileage_km_per_liter=Decimal("15.0"),
        fuel_price_per_liter=Decimal("100.00"),
        currency=Currency.INR,
    )
    segment = RoadSegment(
        origin_name="Bengaluru",
        destination_name="Mysuru",
        distance_km=Decimal("300.0"),
        duration_minutes=240,
        toll_charges=Decimal("320.00"),
    )

    lines = RoadBudgetCalculator.calculate_segment_budget(segment, profile)
    assert len(lines) == 2

    # Fuel line
    fuel_line = lines[0]
    assert fuel_line.category == BudgetCategory.TRANSPORT
    assert fuel_line.amount == Decimal("2000.000000")
    assert fuel_line.is_known is True

    # Toll line
    toll_line = lines[1]
    assert toll_line.category == BudgetCategory.TOLLS_AND_FEES
    assert toll_line.amount == Decimal("320.00")
    assert toll_line.is_known is True


def test_unverified_toll_produces_explicit_unknown_line() -> None:
    profile = VehicleFuelProfile.default_for_mode(TransportMode.MOTORCYCLE_PETROL)
    segment = RoadSegment(
        origin_name="Delhi",
        destination_name="Jaipur",
        distance_km=Decimal("280.0"),
        duration_minutes=300,
        toll_charges=None,  # Unverified toll
        toll_unknown_reason="Motorcycle toll plaza rates unverified on NH48",
    )

    lines = RoadBudgetCalculator.calculate_segment_budget(segment, profile)
    assert len(lines) == 2

    toll_line = lines[1]
    assert toll_line.category == BudgetCategory.TOLLS_AND_FEES
    assert toll_line.amount is None
    assert toll_line.is_known is False
    assert toll_line.unknown_reason == "Motorcycle toll plaza rates unverified on NH48"
