"""Budget accounting with reservations.

Money is an integer number of micro-dollars. Floats are never used for money.

Every effect that can spend (an LLM call) is issued with a *reservation*: a slice of the
remaining budget it must not exceed. The interpreter enforces each slice (M3). Concurrent
effects get disjoint slices, so their sum can never exceed what was available. This is what
makes the core's budget invariant hold even when effects run in parallel:

    spent + reserved <= limit        for every non-terminal state
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import NonNegativeInt, PositiveInt

from msa.core._frozen import Frozen
from msa.core.ids import EffectId, ValidEffectId


class InsufficientBudget(Exception):
    """Raised when the remaining budget cannot fund the effects about to be issued."""


class Spend(Frozen):
    usd_micros: NonNegativeInt = 0
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0

    def __add__(self, other: Spend) -> Spend:
        return Spend(
            usd_micros=self.usd_micros + other.usd_micros,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


NO_SPEND = Spend()


class Reservation(Frozen):
    effect_id: ValidEffectId
    usd_micros: PositiveInt


class Ledger(Frozen):
    limit_usd_micros: PositiveInt
    spent: Spend = NO_SPEND
    reservations: tuple[Reservation, ...] = ()

    @property
    def reserved_usd_micros(self) -> int:
        return sum(r.usd_micros for r in self.reservations)

    @property
    def committed_usd_micros(self) -> int:
        return self.spent.usd_micros + self.reserved_usd_micros

    @property
    def available_usd_micros(self) -> int:
        return max(0, self.limit_usd_micros - self.committed_usd_micros)

    @property
    def overcommitted(self) -> bool:
        return self.committed_usd_micros > self.limit_usd_micros

    def reservation_for(self, effect_id: EffectId) -> Reservation | None:
        return next((r for r in self.reservations if r.effect_id == effect_id), None)

    def reserve(self, effect_ids: Sequence[EffectId]) -> Ledger:
        """Split the available budget equally across ``effect_ids``.

        Floor division guarantees the slices sum to at most the available amount.

        Raises:
            InsufficientBudget: if any slice would be zero micro-dollars.
        """
        if not effect_ids:
            return self
        share = self.available_usd_micros // len(effect_ids)
        if share <= 0:
            raise InsufficientBudget(
                f"{self.available_usd_micros} micro-USD available, "
                f"{len(effect_ids)} spending effect(s) to fund"
            )
        added = tuple(Reservation(effect_id=eid, usd_micros=share) for eid in effect_ids)
        return self.model_copy(update={"reservations": self.reservations + added})

    def settle(self, effect_id: EffectId, spend: Spend) -> Ledger:
        """Release the effect's reservation and record what it actually spent.

        Spend above the reservation is recorded truthfully. The money is gone either way.
        The resulting overcommitment is detected by the state machine, which then fails
        the run.
        """
        return self.model_copy(
            update={
                "spent": self.spent + spend,
                "reservations": tuple(r for r in self.reservations if r.effect_id != effect_id),
            }
        )

    def release_all(self) -> Ledger:
        """Drop every outstanding reservation (used when a run terminates)."""
        return self.model_copy(update={"reservations": ()})
