"""Routing and leg cost computation."""

from __future__ import annotations

from decimal import Decimal

from contracts.money import Currency, Money
from contracts.trips import TransportMode


def estimate_transit_cost(mode: TransportMode, distance_km: float) -> Money:
    """Provides indicative base transit cost estimation by mode and distance in India."""
    rate_per_km = {
        TransportMode.FLIGHT: Decimal("8.0"),
        TransportMode.TRAIN: Decimal("1.8"),
        TransportMode.BUS: Decimal("1.2"),
        TransportMode.CAB: Decimal("15.0"),
        TransportMode.FERRY: Decimal("3.0"),
        TransportMode.WALK: Decimal("0.0"),
    }.get(mode, Decimal("2.0"))

    total = max(Decimal("50.0"), rate_per_km * Decimal(str(distance_km)))
    return Money(amount=total.quantize(Decimal("1.0")), currency=Currency.INR)
