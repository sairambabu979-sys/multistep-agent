"""The dispatcher: ``start``, ``transition``, and ``replay``.

    transition(state, event) -> Step(state', effects, edge)

The pipeline for every event, in order:

1. Admission: reject events for terminal runs, a second ``RunStarted``, clock regressions,
   and completions for effects that are not pending (or of the wrong kind).
2. Bookkeeping: bump ``seq``, stamp ``updated_at``, retire the effect, settle its spend.
3. Global guards: cancellation, deadline, budget overcommitment, agent failure/refusal.
   These may end the run from any phase.
4. Rule dispatch: look up ``(phase, event type)`` in the table and run the handler.
   The resulting phase must be one the rule declared.
5. Commit: assign deterministic effect ids, reserve budget for spending effects, seal
   terminal runs (cancel what is still pending, publish the outcome), and check every
   invariant.

Any rejection raises ``TransitionRejected`` and leaves the input state untouched, because
nothing here mutates.
"""

from __future__ import annotations

from collections.abc import Iterable

from msa.core.budget import InsufficientBudget, Ledger
from msa.core.effects import AnyCommand, CancelEffects, Effect, PrepareWorkspace, PublishOutcome
from msa.core.errors import InvariantViolation, Rejection, ReplayError, TransitionRejected
from msa.core.events import (
    AgentFailed,
    AgentRefused,
    CancelRequested,
    Completion,
    DeadlineExceeded,
    EventBase,
    RunStarted,
    spend_of,
)
from msa.core.ids import effect_id
from msa.core.invariants import violations
from msa.core.state import RunState
from msa.core.table import Decision, Edge, Step, TransitionTable
from msa.core.transitions import TABLE
from msa.core.values import Outcome, OutcomeReason, Phase


def start(event: RunStarted) -> Step:
    """Create a run. ``RunStarted`` is the only event that does not need a prior state."""
    state = RunState(
        run_id=event.run_id,
        seq=1,
        phase=Phase.INTAKE,
        started_at=event.at,
        updated_at=event.at,
        deadline=event.at + event.limits.wall_clock,
        task_digest=event.task_digest,
        limits=event.limits,
        ledger=Ledger(limit_usd_micros=event.limits.budget_usd_micros),
    )
    return _commit(None, event, Decision(state, (PrepareWorkspace(),)), escape=False)


def transition(state: RunState, event: EventBase, *, table: TransitionTable = TABLE) -> Step:
    source = state.phase
    current = _admit(state, event)

    if (escape := _global_guard(current, event)) is not None:
        return _commit(source, event, escape, escape=True)

    rule = table.get(source, type(event))
    if rule is None:
        raise TransitionRejected(
            Rejection.NOT_ACCEPTED_IN_PHASE, f"{type(event).__name__} in {source}"
        )
    decision = rule.handle(current, event)
    if decision.state.phase not in rule.targets:
        raise InvariantViolation(
            (
                f"rule {source} x {type(event).__name__} moved to undeclared phase "
                f"{decision.state.phase}",
            )
        )
    return _commit(source, event, decision, escape=False)


def replay(events: Iterable[EventBase], *, table: TransitionTable = TABLE) -> RunState:
    """Fold a journal back into the state it produced. The first event must be ``RunStarted``."""
    iterator = iter(events)
    first = next(iterator, None)
    if not isinstance(first, RunStarted):
        raise ReplayError(f"journal must begin with RunStarted, got {type(first).__name__}")
    state = start(first).state
    for event in iterator:
        try:
            state = transition(state, event, table=table).state
        except TransitionRejected as exc:
            raise ReplayError(f"event #{state.seq + 1} rejected on replay: {exc}") from exc
    return state


# ---------- pipeline stages


def _admit(state: RunState, event: EventBase) -> RunState:
    if state.is_terminal:
        raise TransitionRejected(Rejection.TERMINAL, f"run is {state.phase}")
    if isinstance(event, RunStarted):
        raise TransitionRejected(Rejection.ALREADY_STARTED, f"run {state.run_id} exists")
    if event.at < state.updated_at:
        raise TransitionRejected(
            Rejection.CLOCK_REGRESSION, f"event at {event.at} precedes {state.updated_at}"
        )

    current = state.model_copy(update={"seq": state.seq + 1, "updated_at": event.at})
    if not isinstance(event, Completion):
        return current

    effect = state.pending_effect(event.effect_id)
    if effect is None:
        raise TransitionRejected(
            Rejection.UNKNOWN_EFFECT, f"{event.effect_id} is not pending (stale or duplicate)"
        )
    if effect.command.kind not in event.completes:
        raise TransitionRejected(
            Rejection.MISMATCHED_COMPLETION,
            f"{type(event).__name__} cannot complete {effect.command.kind}",
        )
    return current.model_copy(
        update={
            "pending": tuple(e for e in state.pending if e.id != effect.id),
            "ledger": state.ledger.settle(effect.id, spend_of(event)),
        }
    )


def _global_guard(state: RunState, event: EventBase) -> Decision | None:
    """Transitions that apply in every live phase, in priority order."""
    if isinstance(event, CancelRequested):
        return _terminal(state, OutcomeReason.CANCELLED, event.reason)
    if isinstance(event, DeadlineExceeded) or event.at > state.deadline:
        return _terminal(state, OutcomeReason.DEADLINE_EXCEEDED, f"deadline {state.deadline}")
    if state.ledger.overcommitted:
        ledger = state.ledger
        detail = f"committed {ledger.committed_usd_micros} of {ledger.limit_usd_micros} micro-USD"
        return _terminal(state, OutcomeReason.BUDGET_EXHAUSTED, detail)
    if isinstance(event, AgentFailed):
        return _terminal(state, OutcomeReason.AGENT_FAILED, event.error)
    if isinstance(event, AgentRefused):
        return _terminal(state, OutcomeReason.AGENT_REFUSED, event.category or "uncategorized")
    return None


def _terminal(state: RunState, reason: OutcomeReason, detail: str) -> Decision:
    outcome = Outcome(reason=reason, detail=detail)
    return Decision(state.model_copy(update={"phase": outcome.phase, "outcome": outcome}))


def _commit(source: Phase | None, event: EventBase, decision: Decision, *, escape: bool) -> Step:
    state, commands = decision.state, decision.commands
    if state.is_terminal:
        state, commands = _seal(state)
    try:
        state, effects = _issue(state, commands)
    except InsufficientBudget as exc:
        # Only live runs issue spending commands, so this recursion terminates: the
        # exhausted run is terminal and its sealing commands spend nothing.
        exhausted = _terminal(state, OutcomeReason.BUDGET_EXHAUSTED, str(exc)).state
        return _commit(source, event, Decision(exhausted), escape=True)
    edge = Edge(source, type(event).__name__, state.phase, escape)
    if problems := violations(state):
        raise InvariantViolation(problems)
    return Step(state=state, effects=effects, edge=edge)


def _seal(state: RunState) -> tuple[RunState, tuple[AnyCommand, ...]]:
    """A terminal run cancels whatever is still in flight and publishes its outcome."""
    if state.outcome is None:  # pragma: no cover - guarded by _terminal and _stop
        raise InvariantViolation((f"terminal phase {state.phase} without an outcome",))
    commands: list[AnyCommand] = []
    if state.pending:
        commands.append(CancelEffects(effect_ids=tuple(e.id for e in state.pending)))
    commands.append(PublishOutcome(outcome=state.outcome))
    sealed = state.model_copy(update={"pending": (), "ledger": state.ledger.release_all()})
    return sealed, tuple(commands)


def _issue(
    state: RunState, commands: tuple[AnyCommand, ...]
) -> tuple[RunState, tuple[Effect, ...]]:
    """Address commands with deterministic ids and fund the spending ones."""
    ids = [effect_id(state.run_id, state.seq, index) for index in range(len(commands))]
    ledger = state.ledger.reserve([i for i, c in zip(ids, commands, strict=True) if c.spends])
    effects = tuple(
        Effect(
            id=i,
            budget_usd_micros=r.usd_micros if (r := ledger.reservation_for(i)) else None,
            command=c,
        )
        for i, c in zip(ids, commands, strict=True)
    )
    awaited = tuple(e for e in effects if e.command.awaits)
    return state.model_copy(update={"pending": state.pending + awaited, "ledger": ledger}), effects
