import ast
import inspect
from decimal import Decimal

import ledger.money
from ledger import Money
from ledger.rounding import to_cents


def test_to_cents():
    assert to_cents(Decimal("1.005")) == Decimal("1.01")
    assert to_cents(Decimal("1.009"), down=True) == Decimal("1.00")
    assert to_cents(Decimal("-1.009"), down=True) == Decimal("-1.00")
    assert str(to_cents(Decimal(3))) == "3.00"


def test_money_rounds_only_through_to_cents():
    tree = ast.parse(inspect.getsource(ledger.money))
    quantize = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "quantize"]
    assert quantize == [], "money.py still calls quantize itself"


def test_every_result_is_unchanged():
    assert Money.of("1.005", "EUR").amount == Decimal("1.01")
    assert Money.of("0.15", "EUR").times(3) == Money.of("0.45", "EUR")
    assert Money.of("10.00", "EUR").times(Decimal("0.125")) == Money.of("1.25", "EUR")
    assert Money.of("10.00", "EUR").split(3) == [Money.of("3.33", "EUR")] * 3
