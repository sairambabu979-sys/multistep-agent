"""Exhaustive model checking of the state machine at the production default limits.

For each (plan gate, escalation) configuration, every reachable state is visited by
answering every pending effect with every answer class. These are statements about *all*
runs within the bounds, not a sample of them.
"""

from __future__ import annotations

import pytest

from msa.core import TABLE
from msa.core.values import Escalation, OutcomeReason, Phase, PlanGate
from tests.support.explorer import MODEL_CHECK_CONFIGS, explore_config

pytestmark = pytest.mark.slow

CONFIGS = pytest.mark.parametrize(("gate", "escalation"), MODEL_CHECK_CONFIGS)


@CONFIGS
def test_every_reachable_live_state_is_waiting_on_something(
    gate: PlanGate, escalation: Escalation
) -> None:
    exploration = explore_config(gate, escalation)
    stuck = [
        key
        for key, state in exploration.states.items()
        if not state.is_terminal and not exploration.successors[key]
    ]
    assert stuck == []


@CONFIGS
def test_done_is_reachable_from_every_live_state(gate: PlanGate, escalation: Escalation) -> None:
    """Liveness (EF COMPLETED): no reachable state is doomed to fail or be cancelled."""
    exploration = explore_config(gate, escalation)
    can_finish = exploration.can_reach(Phase.COMPLETED)
    doomed = [
        state.phase
        for key, state in exploration.states.items()
        if not state.is_terminal and key not in can_finish
    ]
    assert doomed == []


@CONFIGS
def test_negotiation_never_repeats_a_round_without_progress(
    gate: PlanGate, escalation: Escalation
) -> None:
    assert explore_config(gate, escalation).stalled_negotiations == []


@CONFIGS
def test_pausing_and_resuming_returns_to_the_state_that_was_paused(
    gate: PlanGate, escalation: Escalation
) -> None:
    """Checked in every reachable state that accepts a pause: the same phase, the same work
    in flight, the same evidence and findings, the same loop counters."""
    exploration = explore_config(gate, escalation)
    assert exploration.pause_mismatches == []
    assert sum(s.phase is Phase.PAUSED for s in exploration.states.values()) > 1_000


@CONFIGS
def test_a_pause_is_refused_only_while_a_person_is_already_asked(
    gate: PlanGate, escalation: Escalation
) -> None:
    exploration = explore_config(gate, escalation)
    asking = [
        s for s in exploration.states.values() if not s.is_terminal and s.waiting_since is not None
    ]
    assert exploration.pause_refusals > 0
    # Each such state is explored once and refuses the one pause offered to it.
    assert exploration.pause_refusals == len(asking)


@CONFIGS
def test_every_phase_the_configuration_allows_is_reached(
    gate: PlanGate, escalation: Escalation
) -> None:
    reached = {s.phase for s in explore_config(gate, escalation).states.values()}
    expected = set(Phase) if gate is PlanGate.HUMAN else set(Phase) - {Phase.PLAN_APPROVAL}
    assert reached == expected


@CONFIGS
def test_state_space_is_explored_at_meaningful_depth(
    gate: PlanGate, escalation: Escalation
) -> None:
    """Guards against a silently trivial exploration (e.g. an environment model that fails
    every run at intake). Every loop must actually reach its cap."""
    exploration = explore_config(gate, escalation)
    limits = exploration.limits
    states = exploration.states.values()
    assert max(s.revisions for s in states) == limits.max_revisions
    assert max(s.negotiation_round for s in states) == limits.max_negotiation_rounds
    if gate is PlanGate.HUMAN:
        assert max(s.replans for s in states) == limits.max_replans
    assert len(exploration.states) > 1_000


def test_every_declared_edge_is_exercised() -> None:
    """Each (phase, event, target) the table permits is taken by some reachable run."""
    observed = {
        (edge.source, edge.event, edge.target)
        for config in MODEL_CHECK_CONFIGS
        for edge in explore_config(*config).edges
        if not edge.escape and edge.source is not None
    }
    assert TABLE.declared_edges() - observed == set()
    assert observed - TABLE.declared_edges() == set()


def test_every_rule_driven_outcome_is_reachable() -> None:
    reached = set().union(*(explore_config(*config).outcomes for config in MODEL_CHECK_CONFIGS))
    # Budget, deadline, cancel, and agent failures are global escapes, covered elsewhere.
    # Environment failures no longer end a run: they block it.
    assert reached == {
        OutcomeReason.COMPLETED,
        OutcomeReason.ADMISSION_DENIED,
        OutcomeReason.REQUEST_REJECTED,
        OutcomeReason.REPLAN_LIMIT,
        OutcomeReason.REVISION_LIMIT,
        OutcomeReason.HUMAN_ABORTED,
    }
