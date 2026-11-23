from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from msa.core.budget import InsufficientBudget, Ledger, Spend
from msa.core.ids import EffectId


def ids(n: int) -> list[EffectId]:
    return [EffectId(f"run:1:{i}") for i in range(n)]


def test_spend_adds_componentwise() -> None:
    total = Spend(usd_micros=1, input_tokens=2, output_tokens=3) + Spend(
        usd_micros=10, input_tokens=20, output_tokens=30
    )
    assert total == Spend(usd_micros=11, input_tokens=22, output_tokens=33)


def test_reserve_splits_the_available_budget_equally_rounding_down() -> None:
    ledger = Ledger(limit_usd_micros=10).reserve(ids(3))
    assert [r.usd_micros for r in ledger.reservations] == [3, 3, 3]
    assert ledger.available_usd_micros == 1


def test_reserve_with_nothing_to_fund_is_a_no_op() -> None:
    ledger = Ledger(limit_usd_micros=10)
    assert ledger.reserve([]) is ledger


def test_reserve_refuses_zero_sized_slices() -> None:
    with pytest.raises(InsufficientBudget, match="2 spending effect"):
        Ledger(limit_usd_micros=1).reserve(ids(2))


def test_settle_releases_the_reservation_and_records_actual_spend() -> None:
    ledger = Ledger(limit_usd_micros=100).reserve(ids(2))
    settled = ledger.settle(ids(2)[0], Spend(usd_micros=7))
    assert settled.spent.usd_micros == 7
    assert [r.effect_id for r in settled.reservations] == [ids(2)[1]]
    assert settled.committed_usd_micros == 7 + 50


def test_overspend_is_recorded_truthfully_and_flagged() -> None:
    ledger = Ledger(limit_usd_micros=100).reserve(ids(1))
    settled = ledger.settle(ids(1)[0], Spend(usd_micros=150))
    assert settled.spent.usd_micros == 150
    assert settled.overcommitted
    assert settled.available_usd_micros == 0


def test_release_all_drops_every_reservation() -> None:
    ledger = Ledger(limit_usd_micros=100).reserve(ids(3)).release_all()
    assert ledger.reservations == ()
    assert ledger.reservation_for(ids(1)[0]) is None


@given(
    limit=st.integers(min_value=1, max_value=10**12),
    spent=st.integers(min_value=0, max_value=10**12),
    n=st.integers(min_value=1, max_value=8),
)
def test_reservations_never_exceed_what_was_available(limit: int, spent: int, n: int) -> None:
    ledger = Ledger(limit_usd_micros=limit, spent=Spend(usd_micros=spent))
    try:
        reserved = ledger.reserve(ids(n))
    except InsufficientBudget:
        assert ledger.available_usd_micros < n
        return
    assert reserved.reserved_usd_micros <= ledger.available_usd_micros
    assert all(r.usd_micros > 0 for r in reserved.reservations)
