from decimal import Decimal

import pytest
from ledger import Money


@pytest.mark.parametrize(
    ("amount", "parts"),
    [("10.00", 3), ("0.05", 3), ("100.00", 7), ("0.01", 4), ("9.00", 3), ("12.34", 1)],
)
def test_the_shares_add_up_to_the_amount(amount, parts):
    whole = Money.of(amount, "EUR")
    shares = whole.split(parts)
    assert len(shares) == parts
    assert sum((s.amount for s in shares), Decimal(0)) == whole.amount
    assert max(s.amount for s in shares) - min(s.amount for s in shares) <= Decimal("0.01")
    amounts = [s.amount for s in shares]
    assert amounts == sorted(amounts, reverse=True)
    assert all(s.currency == "EUR" for s in shares)


def test_ten_euros_in_three():
    assert Money.of("10.00", "EUR").split(3) == [
        Money.of("3.34", "EUR"),
        Money.of("3.33", "EUR"),
        Money.of("3.33", "EUR"),
    ]
