from decimal import Decimal

import pytest

from ledger import CurrencyMismatch, Money


def test_amounts_are_rounded_half_up_to_the_cent() -> None:
    assert Money.of("1.005", "EUR").amount == Decimal("1.01")
    assert Money.of(3, "EUR").amount == Decimal("3.00")


def test_amounts_in_one_currency_add_and_subtract() -> None:
    total = Money.of("1.10", "EUR") + Money.of("2.20", "EUR") - Money.of("0.30", "EUR")
    assert total == Money.of("3.00", "EUR")


def test_currencies_never_mix() -> None:
    with pytest.raises(CurrencyMismatch):
        Money.of(1, "EUR") + Money.of(1, "USD")


def test_times_rounds_half_up() -> None:
    assert Money.of("0.15", "EUR").times(3) == Money.of("0.45", "EUR")
    assert Money.of("10.00", "EUR").times(Decimal("0.125")) == Money.of("1.25", "EUR")


def test_an_even_split_is_exact() -> None:
    assert Money.of("9.00", "EUR").split(3) == [Money.of("3.00", "EUR")] * 3
