"""The dispatcher: ``start``, ``transition``, and ``replay``.

    transition(state, event) -> Step(state', effects, edge)

The pipeline for every event, in order:

1. Admission: reject events for terminal runs, a second ``RunStarted``, clock regressions,
   and completions for effects that are not pending (or of the wrong kind).
2. Bookkeeping: bump ``seq``, stamp ``updated_at``, retire the effect, settle its spend.
   A person's answer (to a plan gate or a dispute) also moves the deadline by however long
   they took: waiting for a person is not work.
3. Global guards: cancellation, deadline, budget overcommitment, agent failure/refusal.
   These may end the run from any phase. Then a pause, which may stop any phase that is not
   already waiting on a person.
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
from datetime import datetime

from msa.core.budget import InsufficientBudget, Ledger
from msa.core.effects import (
    AdmitRun,
    AnyCommand,
    AwaitResume,
    CancelEffects,
    Effect,
    PublishOutcome,
)
from msa.core.errors import InvariantViolation, Rejection, ReplayError, TransitionRejected
from msa.core.events import (
    AgentFailed,
    AgentRefused,
    CancelRequested,
    Completion,
    DeadlineExceeded,
    EffectFailed,
    EventBase,
    PauseRequested,
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
        phase=Phase.CREATED,
        started_at=event.at,
        updated_at=event.at,
        deadline=event.at + event.limits.wall_clock,
        task_digest=event.task_digest,
        limits=event.limits,
        ledger=Ledger(limit_usd_micros=event.limits.budget_usd_micros),
    )
    return _commit(None, event, Decision(state, (AdmitRun(),)), escape=False)


def transition(state: RunState, event: EventBase, *, table: TransitionTable = TABLE) -> Step:
    source = state.phase
    current = _admit(state, event)

    if (escape := _global_guard(current, event)) is not None:
        return _commit(source, event, escape, escape=True)
    if isinstance(event, PauseRequested):
        return _commit(source, event, _pause(current), escape=True)

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
    return fold(start(first).state, iterator, table=table)


def fold(
    state: RunState, events: Iterable[EventBase], *, table: TransitionTable = TABLE
) -> RunState:
    """Apply ``events`` to ``state`` in order: resume from a snapshot plus the journal tail."""
    for event in events:
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
    current = current.model_copy(
        update={
            "pending": tuple(e for e in state.pending if e.id != effect.id),
            "ledger": state.ledger.settle(effect.id, spend_of(event)),
        }
    )
    if effect.command.human:
        return _resume_clock(current, event.at)
    return current


def _resume_clock(state: RunState, now: datetime) -> RunState:
    """A person answered: the time they took moves the deadline, so it never expires while
    the run waits on them. The guards that follow see the moved deadline."""
    if state.waiting_since is None:  # pragma: no cover - an invariant while one is pending
        raise InvariantViolation(("a person answered unasked",))  # pragma: no mutate
    waited = now - state.waiting_since
    return state.model_copy(
        update={
            "deadline": state.deadline + waited,
            "paused": state.paused + waited,
            "waiting_since": None,
        }
    )


def _global_guard(state: RunState, event: EventBase) -> Decision | None:
    """Transitions that apply in every live phase, in priority order."""
    if isinstance(event, CancelRequested):
        return _terminal(state, OutcomeReason.CANCELLED, event.reason)
    if isinstance(event, DeadlineExceeded) or event.at > _deadline_at(state, event.at):
        return _terminal(state, OutcomeReason.DEADLINE_EXCEEDED, f"deadline {state.deadline}")
    if state.ledger.overcommitted:
        ledger = state.ledger
        detail = f"committed {ledger.committed_usd_micros} of {ledger.limit_usd_micros} micro-USD"
        return _terminal(state, OutcomeReason.BUDGET_EXHAUSTED, detail)
    if isinstance(event, AgentFailed):
        return _terminal(state, OutcomeReason.AGENT_FAILED, event.error)
    if isinstance(event, AgentRefused):
        return _terminal(state, OutcomeReason.AGENT_REFUSED, event.category or "uncategorized")
    if isinstance(event, EffectFailed):
        return _terminal(state, OutcomeReason.EFFECT_FAILED, event.error)
    return None


def _deadline_at(state: RunState, now: datetime) -> datetime:
    """The deadline as it stands at ``now``. While a person is asked the clock is stopped,
    so the time since they were asked does not count: an operator's pause or a stray event
    then cannot expire a run that is only waiting. (An answer moves the deadline for good,
    in ``_resume_clock``, before this runs.)"""
    if state.waiting_since is None:
        return state.deadline
    return state.deadline + (now - state.waiting_since)


def _pause(state: RunState) -> Decision:
    """Cancel what is in flight and wait for an operator. The run resumes in the same
    phase, with the same work issued again (effects are idempotent by design)."""
    if state.phase is Phase.PAUSED or any(e.command.human for e in state.pending):
        raise TransitionRejected(
            Rejection.NOT_PAUSABLE, f"{state.phase} is already waiting on a person"
        )
    cancel = CancelEffects(effect_ids=tuple(e.id for e in state.pending))
    paused = state.model_copy(
        update={
            "phase": Phase.PAUSED,
            "resume_phase": state.phase,
            "resume_commands": tuple(e.command for e in state.pending),
            "pending": (),
            "ledger": state.ledger.release_all(),
        }
    )
    return Decision(paused, (cancel, AwaitResume()))


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
        raise InvariantViolation((f"{state.phase} run has no outcome",))  # pragma: no mutate
    commands: list[AnyCommand] = []
    if state.pending:
        commands.append(CancelEffects(effect_ids=tuple(e.id for e in state.pending)))
    commands.append(PublishOutcome(outcome=state.outcome))
    sealed = state.model_copy(
        update={
            "pending": (),
            "ledger": state.ledger.release_all(),
            "waiting_since": None,
            # A run that ends while paused or blocked will never resume.
            "resume_phase": None,
            "resume_commands": (),
        }
    )
    return sealed, tuple(commands)


def _issue(
    state: RunState, commands: tuple[AnyCommand, ...]
) -> tuple[RunState, tuple[Effect, ...]]:
    """Address commands with deterministic ids and fund the spending ones."""
    addressed = [(effect_id(state.run_id, state.seq, n), c) for n, c in enumerate(commands)]
    ledger = state.ledger.reserve([i for i, c in addressed if c.spends])
    effects = tuple(
        Effect(
            id=i,
            budget_usd_micros=r.usd_micros if (r := ledger.reservation_for(i)) else None,
            command=c,
        )
        for i, c in addressed
    )
    awaited = tuple(e for e in effects if e.command.awaits)
    update: dict[str, object] = {"pending": state.pending + awaited, "ledger": ledger}
    if any(e.command.human for e in awaited):
        update["waiting_since"] = state.updated_at  # the clock stops until they answer
    return state.model_copy(update=update), effects
