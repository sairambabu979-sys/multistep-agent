"""Rounding to the cent, in one place."""

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_cents(amount: Decimal, *, down: bool = False) -> Decimal:
    """``amount`` to the cent: half up, or toward zero when ``down``."""
    return amount.quantize(CENT, rounding=ROUND_DOWN if down else ROUND_HALF_UP)
