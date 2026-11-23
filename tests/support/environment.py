"""Hypothesis strategies over the environment model in ``msa.runtime.simulation``.

The exhaustive model checker, these randomized tests, and ``msa simulate`` share one model
of the outside world, so all of them explore the same space of behaviors.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from hypothesis import strategies as st

from msa.core import RunState
from msa.core.budget import Spend
from msa.core.effects import Effect
from msa.core.events import AgentFailed, AgentRefused, AnyEvent, EffectFailed, SpendingCompletion
from msa.core.values import Escalation, Limits, PlanGate
from msa.runtime.simulation import outcomes


def limits_strategy() -> st.SearchStrategy[Limits]:
    return st.builds(
        Limits,
        budget_usd_micros=st.one_of(st.integers(min_value=1, max_value=2_000_000), st.just(10**12)),
        wall_clock=st.integers(min_value=1, max_value=240).map(lambda m: timedelta(minutes=m)),
        max_replans=st.integers(min_value=0, max_value=2),
        max_revisions=st.integers(min_value=0, max_value=4),
        max_negotiation_rounds=st.integers(min_value=1, max_value=3),
        plan_gate=st.sampled_from(PlanGate),
        escalation=st.sampled_from(Escalation),
    )


def next_time(state: RunState) -> st.SearchStrategy[datetime]:
    return st.integers(min_value=0, max_value=600).map(
        lambda s: state.updated_at + timedelta(seconds=s)
    )


@st.composite
def answers(draw: st.DrawFn, state: RunState, effect: Effect, at: datetime) -> AnyEvent:
    """A plausible answer to ``effect``, with realistic spend and occasional agent failure."""
    failures: list[AnyEvent] = (
        [
            AgentFailed(at=at, effect_id=effect.id, error="gateway retries exhausted"),
            AgentRefused(at=at, effect_id=effect.id, category="cyber"),
        ]
        if effect.command.spends
        else [EffectFailed(at=at, effect_id=effect.id, error="git merge failed")]
    )
    if draw(st.integers(min_value=0, max_value=19)) == 0:
        event = draw(st.sampled_from(failures))
    else:
        event = draw(st.sampled_from(outcomes(effect, at, rich=True)))
    if not isinstance(event, SpendingCompletion):
        return event
    cap = effect.budget_usd_micros or 0
    overspend = draw(st.integers(min_value=0, max_value=9)) == 0
    usd = draw(
        st.integers(min_value=cap + 1, max_value=2 * cap + 1)
        if overspend
        else st.integers(min_value=0, max_value=cap)
    )
    spend = Spend(
        usd_micros=usd,
        input_tokens=draw(st.integers(min_value=0, max_value=200_000)),
        output_tokens=draw(st.integers(min_value=0, max_value=64_000)),
    )
    # model_copy preserves the concrete event class, so this is still an AnyEvent member.
    return cast(AnyEvent, event.model_copy(update={"spend": spend}))
