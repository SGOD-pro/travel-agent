"""Domain model for monetary amounts, currency precision, and budget completeness.

Enforces zero-hallucination and non-coercion invariants:
- Money amounts are strictly Decimal.
- Unknown costs cannot be silently coerced to zero.
- Budget aggregations flag completeness explicitly.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Currency(StrEnum):
    INR = "INR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"


class Money(BaseModel):
    """Immutable monetary value object."""

    model_config = ConfigDict(frozen=True)

    amount: Decimal = Field(..., description="Decimal monetary amount, non-negative")
    currency: Currency = Field(default=Currency.INR, description="ISO 4217 Currency code")

    @field_validator("amount")
    @classmethod
    def validate_non_negative(cls, v: Decimal) -> Decimal:
        if v < Decimal("0"):
            raise ValueError("Monetary amount cannot be negative")
        return v.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    def to_display_string(self) -> str:
        """Standard 2-decimal display format."""
        return (
            f"{self.currency.value} {self.amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"
        )

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency != other.currency:
            raise ValueError(
                f"Cannot add mismatched currencies: {self.currency} and {other.currency}"
            )
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency != other.currency:
            raise ValueError(
                f"Cannot subtract mismatched currencies: {self.currency} and {other.currency}"
            )
        if self.amount < other.amount:
            raise ValueError("Resulting monetary amount cannot be negative")
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def __mul__(self, factor: Decimal | int) -> Money:
        if factor < 0:
            raise ValueError("Multiplier cannot be negative")
        factor_dec = Decimal(str(factor))
        return Money(amount=self.amount * factor_dec, currency=self.currency)


class BudgetCategory(StrEnum):
    TRANSPORT = "transport"
    ACCOMMODATION = "accommodation"
    ACTIVITIES = "activities"
    FOOD = "food"
    TOLLS_AND_FEES = "tolls_and_fees"
    MISCELLANEOUS = "miscellaneous"


class BudgetLine(BaseModel):
    """A line item in a trip budget.

    An amount of None indicates an unknown cost (e.g. unquoted toll or fee).
    Unknown costs MUST NOT be coerced to zero.
    """

    model_config = ConfigDict(frozen=True)

    category: BudgetCategory
    amount: Decimal | None = Field(
        default=None, description="Decimal amount if known, None if unknown"
    )
    currency: Currency = Field(default=Currency.INR)
    basis: str = Field(
        ..., description="Description or pricing basis (e.g. 'per_vehicle', 'per_room')"
    )
    quantity: Decimal = Field(default=Decimal("1"), description="Quantity/multiplier")
    source_ref: str | None = Field(
        default=None, description="Evidence reference or assumption source"
    )
    estimated: bool = Field(
        default=False, description="True if based on heuristics/estimates rather than live quote"
    )
    unknown_reason: str | None = Field(
        default=None, description="Explanation when amount is unknown"
    )

    @field_validator("unknown_reason")
    @classmethod
    def validate_unknown_reason(cls, v: str | None, info: object) -> str | None:
        # If amount is None, unknown_reason must be provided
        data = getattr(info, "data", {})
        if data.get("amount") is None and not v:
            raise ValueError("unknown_reason must be specified when amount is unknown (None)")
        return v

    @property
    def is_known(self) -> bool:
        return self.amount is not None

    @property
    def total_known_amount(self) -> Money | None:
        if self.amount is None:
            return None
        return Money(amount=self.amount * self.quantity, currency=self.currency)


class BudgetSummary(BaseModel):
    """Aggregated budget total across multiple budget lines.

    Preserves the invariant: if any line item is unknown, the total is marked
    as incomplete (is_complete = False) and the unknown lines are enumerated.
    """

    model_config = ConfigDict(frozen=True)

    known_total: Money
    is_complete: bool
    unknown_count: int = 0
    unknown_reasons: list[str] = Field(default_factory=list)
    estimated_count: int = 0

    @classmethod
    def from_lines(
        cls, lines: list[BudgetLine], currency: Currency = Currency.INR
    ) -> BudgetSummary:
        total = Decimal("0")
        unknowns: list[str] = []
        estimated_count = 0

        for line in lines:
            if line.currency != currency:
                raise ValueError(
                    f"Line currency {line.currency} does not match summary target {currency}"
                )

            if line.amount is None:
                unknowns.append(line.unknown_reason or "Unknown reason")
            else:
                total += line.amount * line.quantity
                if line.estimated:
                    estimated_count += 1

        return cls(
            known_total=Money(amount=total, currency=currency),
            is_complete=len(unknowns) == 0,
            unknown_count=len(unknowns),
            unknown_reasons=unknowns,
            estimated_count=estimated_count,
        )
