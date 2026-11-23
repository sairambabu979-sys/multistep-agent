"""``RunWorkflow``: one run, driven by Temporal.

    begin: create the run in the journal (or load a journaled one)
    loop:
        start an activity for every pending effect not already started or parked
        wait for the oldest started activity, a signal, or the deadline
        stamp the event, fold it through the core (rejected answers fail loudly)
        commit it to the journal (an activity), then adopt the new state

The rules are the interpreter's own (``execute_effect``, ``decide``, ``snapshot_due``),
so both drivers fail, park and snapshot alike. Two differences are deliberate:

- **Completions are taken in launch order.** A workflow must be deterministic, and
  launch order is deterministic whatever the timing. It costs nothing that matters:
  stages with several effects in flight wait for all of them anyway.
- **A parked run keeps waiting** for a signal instead of exiting: the server holds it,
  for as long as the person takes. The core has stopped the run's clock meanwhile.

Only code that is deterministic runs here: the core, the codec and the clocks below.
Everything else is an activity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from msa.core import RunState, start
    from msa.core.effects import CancelEffects
    from msa.core.events import AnyEvent, Completion, DeadlineExceeded, RunStarted
    from msa.core.ids import Digest, EffectId, RunId
    from msa.core.values import Limits
    from msa.runtime.clock import LogicalClock
    from msa.runtime.codec import (
        CURRENT_EVENT_VERSION,
        decode_event,
        decode_state,
        encode_event,
        encode_state,
        event_kind,
    )
    from msa.runtime.interpreter import decide, failure, snapshot_due

NAME = "aegis-run"
TASK_QUEUE = "aegis"

# An effect may take as long as an agent's whole turn budget; a worker that dies is noticed
# by its missing heartbeat, and the effect is retried on another (at-least-once, as after a
# crash of the interpreter). Executor errors never reach Temporal: they are failure events.
EFFECT_TIMEOUT = timedelta(hours=6)
EFFECT_RETRIES = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))
# The journal is the record: a commit is retried until it lands, never skipped.
JOURNAL_TIMEOUT = timedelta(minutes=1)
JOURNAL_RETRIES = RetryPolicy(maximum_interval=timedelta(minutes=1), maximum_attempts=0)


# ---------- inputs (Temporal's default converter handles dataclasses)


@dataclass
class Start:
    task_digest: str
    limits: str  # Limits as JSON
    environment: str  # how executors are rebuilt, as JSON


@dataclass
class RunSpec:
    run_id: str
    start: Start | None = None  # None: carry on with a run already in the journal
    clock: str = "system"  # or "logical": one tick per event, for simulations
    snapshot_every: int = 50
    heartbeat_seconds: int = 60  # how soon a dead worker's effect is retried elsewhere


@dataclass
class CreateInput:
    run_id: str
    environment: str
    first: str
    state: str


@dataclass
class ExecuteInput:
    run_id: str
    effect_id: str
    now: str


@dataclass
class CommitInput:
    run_id: str
    kind: str
    event: str
    state: str
    snapshot: bool


def encode(event: AnyEvent) -> str:
    """An event as a signal or activity payload."""
    return json.dumps({"kind": event_kind(event), "event": encode_event(event)})


def decode(payload: str) -> AnyEvent:
    data = json.loads(payload)
    return decode_event(data["kind"], CURRENT_EVENT_VERSION, data["event"])


# ---------- time, from Temporal


class WorkflowClock:
    """``SystemClock``, on the workflow's deterministic time."""

    def start_time(self) -> datetime:
        return workflow.now()

    def stamp(self, state: RunState) -> datetime:
        return max(workflow.now(), state.updated_at)

    def wait_budget(self, state: RunState) -> float | None:
        if state.waiting_since is not None:
            return None  # the clock is stopped while a person is asked something
        return max(0.0, (state.deadline - workflow.now()).total_seconds())


type Clock = WorkflowClock | LogicalClock


# ---------- the workflow


@workflow.defn(name=NAME)
class RunWorkflow:
    def __init__(self) -> None:
        self._inbox: list[str] = []  # signalled events, oldest first
        self._state = ""
        self._handles: dict[EffectId, workflow.ActivityHandle[str]] = {}  # in launch order
        self._parked: set[EffectId] = set()

    @workflow.signal(name="event")
    def event(self, payload: str) -> None:
        """A person's answer (``msa approve``, ``msa rule``...), a pause, or a cancel."""
        self._inbox.append(payload)

    @workflow.query(name="state")
    def state(self) -> str:
        return self._state

    @workflow.run
    async def run(self, spec: RunSpec) -> str:
        clock: Clock = LogicalClock() if spec.clock == "logical" else WorkflowClock()
        state = await self._begin(spec, clock)
        handles, parked = self._handles, self._parked
        reported: frozenset[EffectId] = frozenset()
        while not state.is_terminal:
            self._state = encode_state(state)
            for effect in state.pending:
                if effect.id not in handles and effect.id not in parked:
                    handles[effect.id] = workflow.start_activity(
                        "execute",
                        ExecuteInput(spec.run_id, effect.id, clock.stamp(state).isoformat()),
                        result_type=str,
                        start_to_close_timeout=EFFECT_TIMEOUT,
                        heartbeat_timeout=timedelta(seconds=spec.heartbeat_seconds),
                        retry_policy=EFFECT_RETRIES,
                    )
            waiting = frozenset(e.id for e in state.pending)
            if waiting and waiting <= parked and waiting != reported:
                await self._journal("settle", spec.run_id)  # say what the run waits for
                reported = waiting
            try:
                source, event = await self._next(state, clock)
            except _JournalAhead:
                state = await self._reload(spec)
                continue
            if event is not None:
                state = await self._commit(spec, state, source, event)
        self._state = encode_state(state)
        await self._journal("settle", spec.run_id)  # publish the outcome
        return self._state

    async def _begin(self, spec: RunSpec, clock: Clock) -> RunState:
        if spec.start is None:
            return decode_state(await self._journal("load", spec.run_id))
        first = RunStarted(
            at=clock.start_time(),
            run_id=RunId(spec.run_id),
            task_digest=Digest(spec.start.task_digest),
            limits=Limits.model_validate_json(spec.start.limits),
        )
        step = start(first)
        created = CreateInput(
            spec.run_id, spec.start.environment, encode(first), encode_state(step.state)
        )
        await self._journal("create", created)
        return step.state

    async def _next(self, state: RunState, clock: Clock) -> tuple[EffectId | None, AnyEvent | None]:
        """The next event to fold, with the effect it answers; no event when an effect
        parked. Raises ``_JournalAhead`` when another writer completed the effect."""
        handles = self._handles
        head = next(iter(handles), None)

        def ready() -> bool:
            return bool(self._inbox) or (head is not None and handles[head].done())

        if not ready():
            budget = clock.wait_budget(state)
            if budget is not None and budget <= 0:
                return None, DeadlineExceeded(at=clock.stamp(state))
            try:
                await workflow.wait_condition(ready, timeout=budget)
            except TimeoutError:
                return None, DeadlineExceeded(at=clock.stamp(state))
        if self._inbox:  # people and operators first: a cancel should not queue
            event = decode(self._inbox.pop(0))
            source = event.effect_id if isinstance(event, Completion) else None
            return source, event.model_copy(update={"at": clock.stamp(state)})
        assert head is not None  # ready() held without a signal
        handle = handles.pop(head)
        effect = state.pending_effect(head)
        try:
            answer = json.loads(handle.result())
        except ActivityError as exc:  # the worker could not run it, after every retry
            if effect is None:
                return None, None
            cause = exc.cause or exc
            return head, failure(effect, f"the effect could not run: {cause}", clock.stamp(state))
        if answer.get("parked"):
            self._parked.add(head)
            return head, None
        if answer.get("stale"):  # completed in the journal by another writer
            raise _JournalAhead
        event = decode(answer["event"])
        return head, event.model_copy(update={"at": clock.stamp(state)})

    async def _commit(
        self, spec: RunSpec, state: RunState, source: EffectId | None, event: AnyEvent
    ) -> RunState:
        decided = decide(state, source, event)
        if decided is None:
            return state
        step, event = decided
        commit = CommitInput(
            spec.run_id,
            event_kind(event),
            encode_event(event),
            encode_state(step.state),
            snapshot_due(step.state, spec.snapshot_every),
        )
        if await self._journal("commit", commit) == "conflict":
            # Another writer appended first: its history is the truth, as it is for the
            # interpreter. Carry on from it.
            return await self._reload(spec)
        for effect in step.effects:
            if isinstance(effect.command, CancelEffects):
                for effect_id in effect.command.effect_ids:
                    cancelled = self._handles.pop(effect_id, None)
                    if cancelled is not None:
                        cancelled.cancel()
        self._forget(step.state)
        return step.state

    async def _reload(self, spec: RunSpec) -> RunState:
        current = decode_state(await self._journal("load", spec.run_id))
        self._forget(current)
        return current

    def _forget(self, state: RunState) -> None:
        """Drop work for effects that are no longer pending: answered, or cancelled."""
        pending = {e.id for e in state.pending}
        for effect_id in [i for i in self._handles if i not in pending]:
            self._handles.pop(effect_id).cancel()
        self._parked.intersection_update(pending)

    @staticmethod
    async def _journal(name: str, arg: object) -> str:
        result: str = await workflow.execute_activity(
            name,
            arg,
            result_type=str,
            start_to_close_timeout=JOURNAL_TIMEOUT,
            retry_policy=JOURNAL_RETRIES,
        )
        return result


class _JournalAhead(Exception):
    """The journal holds events this workflow has not seen: adopt them."""


__all__ = [
    "NAME",
    "TASK_QUEUE",
    "CommitInput",
    "CreateInput",
    "ExecuteInput",
    "RunSpec",
    "RunWorkflow",
    "Start",
    "decode",
    "encode",
]
