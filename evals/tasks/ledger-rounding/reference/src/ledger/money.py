"""Amounts of money in one currency, exact to the cent."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ledger.rounding import to_cents


class CurrencyMismatch(ValueError):
    """Two amounts in different currencies were combined."""


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    @classmethod
    def of(cls, amount: str | int | Decimal, currency: str) -> Money:
        """``amount`` in ``currency``, rounded half up to the cent."""
        return cls(to_cents(Decimal(amount)), currency)

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
        return Money(to_cents(product), self.currency)

    def split(self, parts: int) -> list[Money]:
        """This amount in ``parts`` shares, as equal as whole cents allow."""
        if parts < 1:
            raise ValueError("split into at least one part")
        share = to_cents(self.amount / parts, down=True)
        return [Money(share, self.currency) for _ in range(parts)]

    def _check(self, other: Money) -> None:
        if other.currency != self.currency:
            raise CurrencyMismatch(f"cannot combine {self.currency} with {other.currency}")

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"
