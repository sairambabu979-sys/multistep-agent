"""Whole-run properties under a fair environment: one that answers whatever is pending."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from msa.core import Step, replay, start, transition
from msa.core.effects import PublishOutcome
from msa.core.events import EVENT_ADAPTER, AnyEvent
from msa.core.values import Limits
from tests.support.environment import answers, limits_strategy, next_time
from tests.support.factory import run_started

# Generous: with the largest limits drawn here a run needs well under 150 events.
MAX_EVENTS = 400


def drive(limits: Limits, data: st.DataObject) -> tuple[list[AnyEvent], list[Step]]:
    first = run_started(limits)
    events: list[AnyEvent] = [first]
    steps = [start(first)]
    while not steps[-1].state.is_terminal and len(events) < MAX_EVENTS:
        state = steps[-1].state
        effect = data.draw(st.sampled_from(state.pending))
        event = data.draw(answers(state, effect, data.draw(next_time(state))))
        steps.append(transition(state, event))
        events.append(event)
    return events, steps


@given(limits=limits_strategy(), data=st.data())
def test_every_run_terminates_and_publishes_exactly_one_outcome(
    limits: Limits, data: st.DataObject
) -> None:
    _, steps = drive(limits, data)
    final = steps[-1]
    assert final.state.is_terminal, f"no terminal state after {MAX_EVENTS} events"
    assert final.state.outcome is not None
    published = [e for step in steps for e in step.effects if isinstance(e.command, PublishOutcome)]
    assert [p.command for p in published] == [PublishOutcome(outcome=final.state.outcome)]


@given(limits=limits_strategy(), data=st.data())
def test_every_prefix_of_the_journal_replays_to_the_state_it_produced(
    limits: Limits, data: st.DataObject
) -> None:
    events, steps = drive(limits, data)
    decoded = [EVENT_ADAPTER.validate_json(EVENT_ADAPTER.dump_json(e)) for e in events]
    for length in range(1, len(decoded) + 1):
        assert replay(decoded[:length]) == steps[length - 1].state


@given(limits=limits_strategy(), data=st.data())
def test_a_live_run_never_has_more_committed_than_its_budget(
    limits: Limits, data: st.DataObject
) -> None:
    _, steps = drive(limits, data)
    for step in steps:
        ledger = step.state.ledger
        if not step.state.is_terminal:
            assert ledger.spent.usd_micros + ledger.reserved_usd_micros <= limits.budget_usd_micros
        caps = [e.budget_usd_micros for e in step.effects if e.command.spends]
        assert all(cap is not None and cap > 0 for cap in caps)
