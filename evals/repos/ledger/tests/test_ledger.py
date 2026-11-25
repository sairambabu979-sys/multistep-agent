from datetime import date

import pytest

from ledger import CurrencyMismatch, Ledger, Money

EUR = "EUR"


def test_a_transfer_moves_money_between_accounts() -> None:
    ledger = Ledger(EUR)
    ledger.post(date(2026, 1, 1), "cash", Money.of(100, EUR), "opening")
    ledger.transfer(date(2026, 1, 2), "cash", "rent", Money.of(40, EUR), "january")
    assert ledger.balance("cash") == Money.of(60, EUR)
    assert ledger.balance("rent") == Money.of(40, EUR)
    assert ledger.accounts() == ["cash", "rent"]


def test_a_ledger_keeps_to_its_currency() -> None:
    with pytest.raises(CurrencyMismatch):
        Ledger(EUR).post(date(2026, 1, 1), "cash", Money.of(1, "USD"))


def test_an_unknown_account_has_a_zero_balance() -> None:
    assert Ledger(EUR).balance("nobody") == Money.of(0, EUR)
