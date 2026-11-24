"""A ledger of postings to named accounts, all in one currency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ledger.money import CurrencyMismatch, Money


@dataclass(frozen=True)
class Posting:
    day: date
    account: str
    amount: Money
    memo: str = ""


@dataclass
class Ledger:
    currency: str
    postings: list[Posting] = field(default_factory=list)

    def post(self, day: date, account: str, amount: Money, memo: str = "") -> None:
        if amount.currency != self.currency:
            raise CurrencyMismatch(f"this ledger is in {self.currency}, not {amount.currency}")
        self.postings.append(Posting(day, account, amount, memo))

    def transfer(self, day: date, source: str, target: str, amount: Money, memo: str = "") -> None:
        """Move ``amount`` from ``source`` to ``target``: two postings that sum to zero."""
        self.post(day, source, -amount, memo)
        self.post(day, target, amount, memo)

    def balance(self, account: str, as_of: date | None = None) -> Money:
        """The account's balance: every posting, or those up to and including ``as_of``."""
        total = Money.of(0, self.currency)
        for posting in self.postings:
            if posting.account == account and (as_of is None or posting.day <= as_of):
                total = total + posting.amount
        return total

    def accounts(self) -> list[str]:
        return sorted({p.account for p in self.postings})
