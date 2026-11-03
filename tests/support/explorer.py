"""Exhaustive, bounded model checking of the state machine.

This does a breadth-first search over every reachable state for a given ``Limits``,
answering every pending effect with every answer class from ``environment.outcomes``. It
plays the role TLC plays for a TLA+ spec, except the model is the production code itself.

States are deduplicated by an *abstract key* that drops what cannot change future behavior:
the event sequence number, timestamps, effect ids, and the ledger. The ledger can be dropped
because exploration uses zero spend and an effectively unlimited budget. Budget behavior is
covered separately by the property tests.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Hashable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache

from msa.core import Edge, RunState, start, transition
from msa.core.effects import RunNegotiationRound
from msa.core.events import NegotiationRoundCompleted
from msa.core.values import Escalation, Limits, OutcomeReason, Phase, PlanGate
from tests.support.environment import outcomes
from tests.support.factory import run_started

type Key = Hashable


def abstract_key(state: RunState) -> Key:
    return (
        state.phase,
        state.base_tree,
        state.head_tree,
        state.plan,
        state.replans,
        state.revisions,
        state.negotiation_round,
        state.build,
        state.evidence,
        state.findings,
        tuple(sorted((e.command for e in state.pending), key=repr)),
        state.outcome,
    )


@dataclass
class Exploration:
    limits: Limits
    states: dict[Key, RunState] = field(default_factory=dict)
    successors: dict[Key, set[Key]] = field(default_factory=lambda: defaultdict(set))
    edges: set[Edge] = field(default_factory=set)
    transitions: int = 0
    stalled_negotiations: list[str] = field(default_factory=list)

    @property
    def outcomes(self) -> set[OutcomeReason]:
        return {s.outcome.reason for s in self.states.values() if s.outcome is not None}

    def can_reach(self, target: Phase) -> set[Key]:
        """Keys from which some path reaches a state in ``target`` (reverse BFS)."""
        predecessors: dict[Key, set[Key]] = defaultdict(set)
        for src, dsts in self.successors.items():
            for dst in dsts:
                predecessors[dst].add(src)
        frontier = deque(k for k, s in self.states.items() if s.phase is target)
        seen = set(frontier)
        while frontier:
            for prev in predecessors[frontier.popleft()]:
                if prev not in seen:
                    seen.add(prev)
                    frontier.append(prev)
        return seen


def explore(limits: Limits, *, rich: bool = False) -> Exploration:
    result = Exploration(limits=limits)
    initial = start(run_started(limits)).state
    result.states[abstract_key(initial)] = initial
    queue = deque([initial])
    while queue:
        state = queue.popleft()
        key = abstract_key(state)
        at = state.updated_at + timedelta(seconds=1)
        for effect in state.pending:
            for event in outcomes(state, effect, at, rich=rich):
                step = transition(state, event)  # raises on any invariant violation
                result.transitions += 1
                result.edges.add(step.edge)
                _check_negotiation_progress(result, state, event, step.state, step.effects)
                nxt = abstract_key(step.state)
                result.successors[key].add(nxt)
                if nxt not in result.states:
                    result.states[nxt] = step.state
                    queue.append(step.state)
    return result


def _check_negotiation_progress(
    result: Exploration,
    before: RunState,
    event: object,
    after: RunState,
    effects: tuple[object, ...],
) -> None:
    """PLAN §2.4 invariant 4: another round is only scheduled if the open set shrank."""
    if not isinstance(event, NegotiationRoundCompleted):
        return
    rescheduled = any(isinstance(getattr(e, "command", None), RunNegotiationRound) for e in effects)
    if rescheduled and len(after.open_finding_ids) >= len(before.open_finding_ids):
        result.stalled_negotiations.append(
            f"round {before.negotiation_round}: {before.open_finding_ids} -> "
            f"{after.open_finding_ids}"
        )


MODEL_CHECK_CONFIGS: tuple[tuple[PlanGate, Escalation], ...] = tuple(
    (gate, escalation) for gate in PlanGate for escalation in Escalation
)


@cache
def explore_config(gate: PlanGate, escalation: Escalation) -> Exploration:
    """Exhaustive at the *production default* loop limits."""
    defaults = Limits(budget_usd_micros=1, wall_clock=timedelta(minutes=1))
    return explore(
        Limits(
            budget_usd_micros=10**15,
            wall_clock=timedelta(days=365),
            max_replans=defaults.max_replans,
            max_revisions=defaults.max_revisions,
            max_negotiation_rounds=defaults.max_negotiation_rounds,
            plan_gate=gate,
            escalation=escalation,
        )
    )
