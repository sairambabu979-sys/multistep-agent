"""Amounts of money in one currency, exact to the cent."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


class CurrencyMismatch(ValueError):
    """Two amounts in different currencies were combined."""


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    @classmethod
    def of(cls, amount: str | int | Decimal, currency: str) -> Money:
        """``amount`` in ``currency``, rounded half up to the cent."""
        return cls(Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP), currency)

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def times(self, factor: Decimal | int) -> Money:
        """This amount multiplied by ``factor``, rounded half up to the cent."""
        product = self.amount * Decimal(factor)
        return Money(product.quantize(CENT, rounding=ROUND_HALF_UP), self.currency)

    def split(self, parts: int) -> list[Money]:
        """This amount in ``parts`` shares, as equal as whole cents allow."""
        if parts < 1:
            raise ValueError("split into at least one part")
        cents = int((self.amount / CENT).to_integral_value())
        share, extra = divmod(cents, parts)
        return [
            Money(Decimal(share + (1 if index < extra else 0)) * CENT, self.currency)
            for index in range(parts)
        ]

    def _check(self, other: Money) -> None:
        if other.currency != self.currency:
            raise CurrencyMismatch(f"cannot combine {self.currency} with {other.currency}")

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"
