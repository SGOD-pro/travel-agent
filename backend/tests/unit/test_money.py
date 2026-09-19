"""Unit tests for Decimal Money, BudgetLine, and non-coercion invariants."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from travel.domain.money import BudgetCategory, BudgetLine, BudgetSummary, Currency, Money


def test_money_decimal_precision() -> None:
    m1 = Money(amount=Decimal("1500.50"), currency=Currency.INR)
    m2 = Money(amount=Decimal("499.50"), currency=Currency.INR)
    total = m1 + m2
    assert total.amount == Decimal("2000.000000")
    assert total.currency == Currency.INR
    assert total.to_display_string() == "INR 2000.00"


def test_money_negative_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        Money(amount=Decimal("-10.00"), currency=Currency.INR)


def test_money_subtraction_negative_result_rejected() -> None:
    m1 = Money(amount=Decimal("10.00"), currency=Currency.INR)
    m2 = Money(amount=Decimal("20.00"), currency=Currency.INR)
    with pytest.raises(ValueError, match="cannot be negative"):
        _ = m1 - m2


def test_money_currency_mismatch_rejected() -> None:
    m_inr = Money(amount=Decimal("100"), currency=Currency.INR)
    m_usd = Money(amount=Decimal("100"), currency=Currency.USD)
    with pytest.raises(ValueError, match="Cannot add mismatched currencies"):
        _ = m_inr + m_usd


def test_budget_line_unknown_requires_reason() -> None:
    # Amount is None but unknown_reason is missing -> raises ValidationError
    with pytest.raises(ValidationError):
        BudgetLine(
            category=BudgetCategory.TOLLS_AND_FEES,
            amount=None,
            basis="per_plaza",
            unknown_reason="",
        )


def test_budget_summary_complete_when_all_known() -> None:
    lines = [
        BudgetLine(
            category=BudgetCategory.TRANSPORT,
            amount=Decimal("1200"),
            currency=Currency.INR,
            basis="fuel",
            quantity=Decimal("1"),
            estimated=True,
        ),
        BudgetLine(
            category=BudgetCategory.ACCOMMODATION,
            amount=Decimal("3500"),
            currency=Currency.INR,
            basis="room",
            quantity=Decimal("2"),
        ),
    ]
    summary = BudgetSummary.from_lines(lines, currency=Currency.INR)

    assert summary.is_complete is True
    assert summary.known_total.amount == Decimal("8200.000000")
    assert summary.unknown_count == 0
    assert summary.estimated_count == 1


def test_budget_summary_incomplete_when_unknown_present_no_zero_coercion() -> None:
    lines = [
        BudgetLine(
            category=BudgetCategory.TRANSPORT,
            amount=Decimal("1000"),
            currency=Currency.INR,
            basis="fuel",
        ),
        BudgetLine(
            category=BudgetCategory.TOLLS_AND_FEES,
            amount=None,
            currency=Currency.INR,
            basis="expressway_toll",
            unknown_reason="Toll plaza rates unverified for vehicle class",
        ),
    ]
    summary = BudgetSummary.from_lines(lines, currency=Currency.INR)

    # Invariant: Unknown costs do not coerce to zero and flag incomplete budget
    assert summary.is_complete is False
    assert summary.unknown_count == 1
    assert "Toll plaza rates unverified for vehicle class" in summary.unknown_reasons
    # Known total reflects ONLY known items, without pretending the unknown is 0
    assert summary.known_total.amount == Decimal("1000.000000")
