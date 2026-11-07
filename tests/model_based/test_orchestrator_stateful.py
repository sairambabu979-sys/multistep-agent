"""Model-based testing: Hypothesis drives the state machine with random interleavings.

The model checker answers effects with well-formed events in every combination. This test
adds what it leaves out: arbitrary spend (including overspend), tiny budgets and deadlines,
agent failures, redelivered and forged events, cancellation at any point, and a JSON round
trip of the journal. After every step it checks every invariant and replays the journal.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from msa.core import TABLE, Rejection, RunState, TransitionRejected, replay, start, transition
from msa.core.effects import RunNegotiationRound
from msa.core.events import (
    EVENT_ADAPTER,
    AnyEvent,
    CancelRequested,
    Completion,
    DeadlineExceeded,
    ImplementationCompleted,
    NegotiationRoundCompleted,
    VerificationPassed,
)
from msa.core.ids import EffectId
from msa.core.invariants import violations
from msa.core.values import Limits
from tests.support.environment import answers, limits_strategy, next_time
from tests.support.factory import BUILD, run_started


def _live(machine: OrchestratorMachine) -> bool:
    return machine.state is not None and not machine.state.is_terminal


class OrchestratorMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.state: RunState | None = None
        self.journal: list[AnyEvent] = []
        self.accepted_completions: list[Completion] = []
        self.issued: set[EffectId] = set()

    def _current(self) -> RunState:
        assert self.state is not None
        return self.state

    # ---------- delivery and transition-level properties

    def _deliver(self, event: AnyEvent) -> None:
        before = self._current()
        try:
            step = transition(before, event)
        except TransitionRejected:
            return  # rejection never changes state: nothing to record

        assert not before.is_terminal, "terminal runs must absorb every event"
        assert step.state.seq == before.seq + 1
        if not step.edge.escape:
            assert (step.edge.source, step.edge.event, step.edge.target) in TABLE.declared_edges()
        fresh = {e.id for e in step.effects}
        assert not fresh & self.issued, "effect ids must never be reused"
        self.issued |= fresh
        if isinstance(event, NegotiationRoundCompleted) and any(
            isinstance(e.command, RunNegotiationRound) for e in step.effects
        ):
            assert len(step.state.open_finding_ids) < len(before.open_finding_ids)

        self.state = step.state
        self.journal.append(event)
        if isinstance(event, Completion):
            self.accepted_completions.append(event)

    # ---------- rules

    @initialize(limits=limits_strategy())
    def begin(self, limits: Limits) -> None:
        first = run_started(limits)
        step = start(first)
        self.state = step.state
        self.journal = [first]
        self.issued = {e.id for e in step.effects}

    @precondition(_live)
    @rule(data=st.data())
    def answer_a_pending_effect(self, data: st.DataObject) -> None:
        state = self._current()
        effect = data.draw(st.sampled_from(state.pending))
        at = data.draw(next_time(state))
        self._deliver(data.draw(answers(state, effect, at)))

    @precondition(lambda self: bool(self.accepted_completions))
    @rule(data=st.data())
    def redeliver_an_accepted_completion(self, data: st.DataObject) -> None:
        """At-least-once delivery: a second copy of any completion must be rejected."""
        state = self._current()
        original = data.draw(st.sampled_from(self.accepted_completions))
        copy = original.model_copy(update={"at": max(original.at, state.updated_at)})
        with pytest.raises(TransitionRejected):
            transition(state, copy)

    @precondition(_live)
    @rule(data=st.data())
    def deliver_a_forged_completion(self, data: st.DataObject) -> None:
        """Completions with the wrong kind, a stale tree, or an unknown effect id."""
        state = self._current()
        at: datetime = data.draw(next_time(state))
        target = data.draw(
            st.sampled_from([*state.pending, None]).map(lambda e: e.id if e else None)
        )
        effect_id = target or EffectId(f"{state.run_id}:999:0")
        forgeries: list[AnyEvent] = [
            ImplementationCompleted(at=at, effect_id=effect_id),
            VerificationPassed(at=at, effect_id=effect_id, tree=BUILD),
        ]
        self._deliver(data.draw(st.sampled_from(forgeries)))

    @precondition(_live)
    @rule(data=st.data())
    def interrupt(self, data: st.DataObject) -> None:
        """Rarely: an operator cancels or the wall clock runs out."""
        if data.draw(st.integers(min_value=0, max_value=9)) != 0:
            return
        state = self._current()
        at = data.draw(next_time(state))
        interruptions: list[AnyEvent] = [
            CancelRequested(at=at, reason="operator"),
            DeadlineExceeded(at=at),
        ]
        self._deliver(data.draw(st.sampled_from(interruptions)))

    @precondition(lambda self: self.state is not None and self.state.is_terminal)
    @rule(data=st.data())
    def poke_a_finished_run(self, data: st.DataObject) -> None:
        """Terminal absorption: once a run has an outcome, nothing moves it."""
        state = self._current()
        at = data.draw(next_time(state))
        late: list[AnyEvent] = [CancelRequested(at=at, reason="late"), DeadlineExceeded(at=at)]
        event = data.draw(st.sampled_from(late))
        with pytest.raises(TransitionRejected) as rejected:
            transition(state, event)
        assert rejected.value.reason is Rejection.TERMINAL

    # ---------- state-level properties

    @invariant()
    def invariants_hold(self) -> None:
        if self.state is not None:
            assert violations(self.state) == ()

    @invariant()
    def journal_replays_to_the_current_state(self) -> None:
        if self.state is not None:
            assert replay(self.journal) == self.state

    @invariant()
    def journal_survives_a_json_round_trip(self) -> None:
        for event in self.journal:
            assert EVENT_ADAPTER.validate_json(EVENT_ADAPTER.dump_json(event)) == event


TestOrchestrator = OrchestratorMachine.TestCase
