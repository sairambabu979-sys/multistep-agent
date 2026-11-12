from __future__ import annotations

from typing import Any

import pytest

from msa.core.effects import (
    AwaitHumanRuling,
    Effect,
    PublishOutcome,
    RequestPlan,
    RequestReview,
    ReviseChanges,
)
from msa.core.events import CancelRequested, PlanProduced, RevisionCompleted
from msa.core.ids import EffectId, FindingId
from msa.core.values import Outcome, OutcomeReason, Owner
from msa.runtime.clock import EPOCH, LogicalClock, SystemClock
from msa.runtime.codec import (
    CodecError,
    decode_event,
    encode_event,
    event_kind,
)
from msa.runtime.simulation import (
    BUILD,
    Profile,
    SimulatedEnvironment,
    SimulationConfig,
    outcomes,
    revised,
)
from tests.support.driver import Driver

# ---------- codec


def test_events_round_trip() -> None:
    event = CancelRequested(at=EPOCH, reason="x")
    assert decode_event(event_kind(event), 1, encode_event(event)) == event


def test_old_payloads_are_upcast_through_every_version() -> None:
    """A field renamed in v2 and a default added in v3 still load from a v1 row."""

    def v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
        data["reason"] = data.pop("why")
        return data

    def v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "reason": data["reason"].upper()}

    upcasters = {("cancel_requested", 1): v1_to_v2, ("cancel_requested", 2): v2_to_v3}
    legacy = '{"kind": "cancel_requested", "at": "2026-01-01T00:00:00Z", "why": "stop"}'
    event = decode_event("cancel_requested", 1, legacy, upcasters=upcasters, current=3)
    assert event == CancelRequested(at=EPOCH, reason="STOP")
    # A version step with no upcaster for this kind means "unchanged for this kind".
    sparse = {("cancel_requested", 2): v2_to_v3}
    current = '{"kind": "cancel_requested", "at": "2026-01-01T00:00:00Z", "reason": "go"}'
    assert decode_event("cancel_requested", 1, current, upcasters=sparse, current=3) == (
        CancelRequested(at=EPOCH, reason="GO")
    )


def test_payloads_from_a_newer_version_are_refused() -> None:
    with pytest.raises(CodecError, match="newer msa"):
        decode_event("cancel_requested", 2, "{}")


# ---------- clocks


def test_clocks_never_stamp_before_the_state() -> None:
    state = Driver().state
    future = state.model_copy(update={"updated_at": state.updated_at.replace(year=2999)})
    assert SystemClock().stamp(future) == future.updated_at
    assert LogicalClock().stamp(state) == state.updated_at + LogicalClock().tick
    assert LogicalClock().wait_budget(state) is None
    assert SystemClock().wait_budget(future.model_copy(update={"deadline": EPOCH})) == 0.0


# ---------- simulation


def _effect(command: Any, budget: int | None = 1_000_000) -> Effect:
    return Effect(id=EffectId("sim:2:0"), budget_usd_micros=budget, command=command)


def test_answers_are_a_pure_function_of_seed_and_effect() -> None:
    effect = _effect(RequestPlan(attempt=1))
    a = SimulatedEnvironment(SimulationConfig(seed=1)).answer(effect, EPOCH)
    b = SimulatedEnvironment(SimulationConfig(seed=1)).answer(effect, EPOCH)
    assert a == b


def test_spend_respects_both_the_reservation_and_the_per_call_cap() -> None:
    config = SimulationConfig(seed=0, max_call_usd_micros=500)
    for seed in range(50):
        env = SimulatedEnvironment(config.model_copy(update={"seed": seed}))
        tight = env.answer(_effect(RequestPlan(attempt=1), budget=100), EPOCH)
        loose = env.answer(_effect(RequestPlan(attempt=1)), EPOCH)
        assert isinstance(tight, PlanProduced)
        assert isinstance(loose, PlanProduced)
        assert tight.spend.usd_micros <= 100
        assert loose.spend.usd_micros <= 500


def test_the_happy_profile_always_takes_the_first_answer() -> None:
    effect = _effect(ReviseChanges(owner=Owner.CODER, tree=BUILD))
    env = SimulatedEnvironment(SimulationConfig(seed=0, profile=Profile.HAPPY))
    answer = env.answer(effect, EPOCH)
    assert isinstance(answer, RevisionCompleted)
    assert type(answer) is type(outcomes(effect, EPOCH)[0])
    assert answer.head_tree == revised(BUILD)


def test_revisions_produce_a_chain_of_distinct_trees() -> None:
    chain = [BUILD]
    for _ in range(5):
        chain.append(revised(chain[-1]))
    assert len(set(chain)) == len(chain)


def test_fire_and_forget_commands_have_no_answer() -> None:
    outcome = Outcome(reason=OutcomeReason.COMPLETED)
    with pytest.raises(ValueError, match="fire-and-forget"):
        outcomes(_effect(PublishOutcome(outcome=outcome), budget=None), EPOCH)


def test_the_rich_answer_set_extends_the_exhaustive_one() -> None:
    """The model checker's set is a subset of the randomized tests' set, never different."""
    review = _effect(RequestReview(tree=BUILD, cycle=0))
    ruling = _effect(AwaitHumanRuling(tree=BUILD, findings=(FindingId("a"),)), budget=None)
    for effect in (review, ruling):
        basic, rich = outcomes(effect, EPOCH), outcomes(effect, EPOCH, rich=True)
        assert len(rich) > len(basic)
        assert all(answer in rich for answer in basic)
