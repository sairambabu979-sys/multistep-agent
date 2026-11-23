"""Operator and approver actions on a journaled run, shared by the CLI and the API.

Every action builds one event, runs it through the core (which validates it: a person,
never an agent; the right effect pending; a legal verdict), and appends it to the journal
with compare-and-set. Nothing here drives the run: the caller resumes it if it wants to.

The actions map to the permissions of ADR-015 §4:

| action | event | who |
|---|---|---|
| pause | PauseRequested | operator |
| resume | Resumed (when paused) | operator |
| cancel | CancelRequested | operator |
| unblock | Unblocked | operator |
| approve, reject | PlanApproved/PlanRejected, ReleaseApproved/ReleaseRejected | approver |
| abort | HumanAborted, at any question | approver |
| rule | HumanRuled | approver |
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from msa.core import RunState, transition
from msa.core.effects import (
    AwaitHumanRuling,
    AwaitPlanApproval,
    AwaitReleaseApproval,
    AwaitResume,
    AwaitUnblock,
    Command,
    Effect,
)
from msa.core.events import (
    CancelRequested,
    Completion,
    EventBase,
    HumanAborted,
    HumanRuled,
    PauseRequested,
    PlanApproved,
    PlanRejected,
    ReleaseApproved,
    ReleaseRejected,
    Resumed,
    Unblocked,
)
from msa.core.ids import FindingId, RunId
from msa.core.values import Resolution, Verdict
from msa.runtime.clock import Clock
from msa.runtime.journal import SqliteJournal

GATES: tuple[type[Command], ...] = (AwaitPlanApproval, AwaitReleaseApproval)
QUESTIONS: tuple[type[Command], ...] = (
    AwaitPlanApproval,
    AwaitReleaseApproval,
    AwaitHumanRuling,
    AwaitUnblock,
    AwaitResume,
)
type Decision = Literal["approve", "reject"]


class NotWaiting(ValueError):
    """The run is not waiting for the answer given."""


@dataclass(frozen=True, slots=True)
class Done:
    event: EventBase
    state: RunState


def _commit(journal: SqliteJournal, state: RunState, event: EventBase) -> Done:
    step = transition(state, event)  # raises TransitionRejected; the journal is untouched
    journal.append(event, step.state, snapshot=True)
    return Done(event, step.state)


def _pending(state: RunState, kinds: Sequence[type[Command]]) -> Effect:
    effect = next((e for e in state.pending if isinstance(e.command, tuple(kinds))), None)
    if effect is None:
        waiting = ", ".join(e.command.kind for e in state.pending) or "nothing"
        wanted = " or ".join(k.model_fields["kind"].default for k in kinds)
        raise NotWaiting(
            f"run {state.run_id} is not waiting for {wanted}: it is {state.phase.name}, "
            f"waiting for {waiting}"
        )
    return effect


def answer(
    journal: SqliteJournal,
    run_id: RunId,
    clock: Clock,
    kinds: Sequence[type[Command]],
    make: Callable[[Effect, datetime], Completion],
) -> Done:
    state = journal.load(run_id)
    effect = _pending(state, kinds)
    return _commit(journal, state, make(effect, clock.stamp(state)))


def pause(journal: SqliteJournal, run_id: RunId, clock: Clock, *, by: str, reason: str) -> Done:
    state = journal.load(run_id)
    return _commit(journal, state, PauseRequested(at=clock.stamp(state), by=by, reason=reason))


def cancel(journal: SqliteJournal, run_id: RunId, clock: Clock, *, reason: str) -> Done:
    state = journal.load(run_id)
    return _commit(journal, state, CancelRequested(at=clock.stamp(state), reason=reason))


def resume(journal: SqliteJournal, run_id: RunId, clock: Clock, *, by: str) -> Done | None:
    """Answer a pause. None when the run is not paused (there is nothing to answer, and
    driving it is all a resume needs)."""
    state = journal.load(run_id)
    if not any(isinstance(e.command, AwaitResume) for e in state.pending):
        return None
    return answer(
        journal,
        run_id,
        clock,
        (AwaitResume,),
        lambda e, at: Resumed(at=at, effect_id=e.id, by=by),
    )


def unblock(
    journal: SqliteJournal, run_id: RunId, clock: Clock, *, by: str, note: str = ""
) -> Done:
    return answer(
        journal,
        run_id,
        clock,
        (AwaitUnblock,),
        lambda e, at: Unblocked(at=at, effect_id=e.id, by=by, note=note),
    )


def decide(
    journal: SqliteJournal,
    run_id: RunId,
    clock: Clock,
    *,
    by: str,
    decision: Decision,
    feedback: str = "",
) -> Done:
    """Approve or send back whichever gate is pending: the plan, or the release."""

    def make(effect: Effect, at: datetime) -> Completion:
        release = isinstance(effect.command, AwaitReleaseApproval)
        if decision == "approve":
            approved = ReleaseApproved if release else PlanApproved
            return approved(at=at, effect_id=effect.id, by=by)
        rejected = ReleaseRejected if release else PlanRejected
        return rejected(at=at, effect_id=effect.id, by=by, feedback=feedback or "sent back")

    return answer(journal, run_id, clock, GATES, make)


def abort(journal: SqliteJournal, run_id: RunId, clock: Clock, *, by: str, reason: str) -> Done:
    """Stop the run instead of answering whatever question it is waiting on."""
    return answer(
        journal,
        run_id,
        clock,
        QUESTIONS,
        lambda e, at: HumanAborted(at=at, effect_id=e.id, by=by, reason=reason),
    )


def rule(
    journal: SqliteJournal,
    run_id: RunId,
    clock: Clock,
    *,
    by: str,
    verdicts: Mapping[str, str],
) -> Done:
    allowed = {"upheld", "dismissed", "deferred"}
    if bad := sorted(f"{k}={v}" for k, v in verdicts.items() if v not in allowed):
        raise ValueError(f"{', '.join(bad)}: expected upheld, dismissed or deferred")
    resolutions = tuple(
        Resolution(finding_id=FindingId(k), verdict=Verdict(v)) for k, v in sorted(verdicts.items())
    )
    return answer(
        journal,
        run_id,
        clock,
        (AwaitHumanRuling,),
        lambda e, at: HumanRuled(at=at, effect_id=e.id, by=by, resolutions=resolutions),
    )
