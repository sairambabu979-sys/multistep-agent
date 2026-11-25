from datetime import date

from ledger import Ledger, Money


def test_postings_on_the_day_count():
    ledger = Ledger("EUR")
    ledger.post(date(2026, 1, 1), "cash", Money.of(10, "EUR"))
    ledger.post(date(2026, 1, 2), "cash", Money.of(5, "EUR"))
    ledger.post(date(2026, 1, 3), "cash", Money.of(1, "EUR"))
    assert ledger.balance("cash", as_of=date(2026, 1, 2)) == Money.of(15, "EUR")
    assert ledger.balance("cash", as_of=date(2026, 1, 1)) == Money.of(10, "EUR")
    assert ledger.balance("cash", as_of=date(2025, 12, 31)) == Money.of(0, "EUR")
    assert ledger.balance("cash") == Money.of(16, "EUR")
