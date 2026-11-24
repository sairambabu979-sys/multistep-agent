from datetime import date

from ledger import Ledger, Money, StatementLine

EUR = "EUR"


def ledger():
    ledger = Ledger(EUR)
    ledger.post(date(2026, 1, 5), "cash", Money.of(100, EUR), "opening")
    ledger.post(date(2026, 1, 10), "cash", Money.of(-30, EUR), "groceries")
    ledger.post(date(2026, 1, 3), "cash", Money.of(7, EUR), "found")  # posted later
    ledger.post(date(2026, 1, 10), "cash", Money.of(-5, EUR), "coffee")
    ledger.post(date(2026, 1, 20), "cash", Money.of(1, EUR), "interest")
    ledger.post(date(2026, 1, 10), "rent", Money.of(500, EUR), "another account")
    return ledger


def test_the_lines_in_range_with_a_running_balance():
    assert ledger().statement("cash", date(2026, 1, 5), date(2026, 1, 10)) == [
        StatementLine(date(2026, 1, 5), "opening", Money.of(100, EUR), Money.of(107, EUR)),
        StatementLine(date(2026, 1, 10), "groceries", Money.of(-30, EUR), Money.of(77, EUR)),
        StatementLine(date(2026, 1, 10), "coffee", Money.of(-5, EUR), Money.of(72, EUR)),
    ]


def test_an_empty_range():
    assert ledger().statement("cash", date(2026, 2, 1), date(2026, 2, 28)) == []


def test_the_balance_before_the_start_counts():
    (line,) = ledger().statement("cash", date(2026, 1, 20), date(2026, 1, 31))
    assert line.balance == Money.of(73, EUR)
