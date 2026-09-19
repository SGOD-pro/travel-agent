"""Domain model for Road Transit, Fuel Math, and Route Budgeting.

Conforms strictly to docs/TRD.md:
- fuel_liters = route_distance_km / mileage_km_per_liter
- fuel_cost = fuel_liters * fuel_price_per_liter
- road_total = fuel_cost + applicable_tolls + parking + rental + applicable_fees_and_taxes
- Non-negative costs, positive mileage.
- Unverified tolls/fees remain explicit unknown BudgetLines (never coerced to 0).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from travel.domain.money import BudgetCategory, BudgetLine, Currency
from travel.domain.trips import TransportMode


class VehicleFuelProfile(BaseModel):
    """Vehicle mileage benchmarks and fuel pricing configuration."""

    model_config = ConfigDict(frozen=True)

    mode: TransportMode
    mileage_km_per_liter: Decimal = Field(..., gt=Decimal("0"))
    fuel_price_per_liter: Decimal = Field(..., gt=Decimal("0"))
    currency: Currency = Field(default=Currency.INR)
    source: str = Field(default="national_average_estimate")

    @classmethod
    def default_for_mode(
        cls,
        mode: TransportMode,
        fuel_price_per_liter: Decimal = Decimal("102.50"),
        currency: Currency = Currency.INR,
    ) -> VehicleFuelProfile:
        if mode == TransportMode.MOTORCYCLE_PETROL:
            return cls(
                mode=mode,
                mileage_km_per_liter=Decimal("40.0"),
                fuel_price_per_liter=fuel_price_per_liter,
                currency=currency,
                source="default_petrol_motorcycle_benchmark",
            )
        # Default to petrol car
        return cls(
            mode=TransportMode.CAR_PETROL,
            mileage_km_per_liter=Decimal("15.0"),
            fuel_price_per_liter=fuel_price_per_liter,
            currency=currency,
            source="default_petrol_car_benchmark",
        )


class RoadSegment(BaseModel):
    """Calculated road segment between two points."""

    model_config = ConfigDict(frozen=True)

    origin_name: str
    destination_name: str
    distance_km: Decimal = Field(..., ge=Decimal("0"))
    duration_minutes: int = Field(..., ge=0)
    toll_charges: Decimal | None = Field(
        default=None, description="Known toll cost, or None if unverified"
    )
    toll_unknown_reason: str | None = Field(
        default=None, description="Reason if toll rates are unverified for this corridor"
    )

    @field_validator("toll_unknown_reason")
    @classmethod
    def validate_toll_reason(cls, v: str | None, info: object) -> str | None:
        data = getattr(info, "data", {})
        if data.get("toll_charges") is None and not v:
            return "Toll plaza rates unverified on this corridor"
        return v


class RoadBudgetCalculator:
    """Computes exact fuel and road transit budget lines."""

    @staticmethod
    def calculate_segment_budget(
        segment: RoadSegment,
        vehicle_profile: VehicleFuelProfile,
    ) -> list[BudgetLine]:
        lines: list[BudgetLine] = []

        # 1. Fuel Calculation
        # fuel_liters = route_distance_km / mileage_km_per_liter
        fuel_liters = (segment.distance_km / vehicle_profile.mileage_km_per_liter).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        fuel_cost = (fuel_liters * vehicle_profile.fuel_price_per_liter).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )

        lines.append(
            BudgetLine(
                category=BudgetCategory.TRANSPORT,
                amount=fuel_cost,
                currency=vehicle_profile.currency,
                basis=f"fuel_{vehicle_profile.mode.value} ({segment.distance_km}km @ {vehicle_profile.mileage_km_per_liter}km/l)",
                quantity=Decimal("1"),
                source_ref=vehicle_profile.source,
                estimated=True,
            )
        )

        # 2. Toll Calculation (Explicit Unknown if unverified)
        if segment.toll_charges is not None:
            lines.append(
                BudgetLine(
                    category=BudgetCategory.TOLLS_AND_FEES,
                    amount=segment.toll_charges,
                    currency=vehicle_profile.currency,
                    basis=f"toll_{segment.origin_name}_to_{segment.destination_name}",
                    quantity=Decimal("1"),
                    source_ref="official_plaza_rate",
                    estimated=False,
                )
            )
        else:
            lines.append(
                BudgetLine(
                    category=BudgetCategory.TOLLS_AND_FEES,
                    amount=None,
                    currency=vehicle_profile.currency,
                    basis=f"toll_{segment.origin_name}_to_{segment.destination_name}",
                    quantity=Decimal("1"),
                    source_ref="unverified_corridor",
                    estimated=True,
                    unknown_reason=segment.toll_unknown_reason or "Toll rates unverified",
                )
            )

        return lines
