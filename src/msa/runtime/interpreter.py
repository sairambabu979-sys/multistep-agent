"""The interpreter: the imperative shell that drives the pure core against the world.

    loop:
        launch an executor task for every pending effect not already running
        wait for the next completion (or the deadline)
        stamp it with the clock, compute the transition (pure; may be rejected)
        append the event to the journal       <- the durable commit point
        adopt the new state; run fire-and-forget effects (cancel, publish)

Crash semantics follow from the commit point:

* An effect whose completion was journaled is **never executed again**. On resume it is no
  longer pending. This is what stops a crash from paying for the same LLM call twice.
* An effect in flight at the crash is executed again on resume (at-least-once).
  Executors receive the effect id as an idempotency key and must tolerate a repeat.

Executor failures never crash the interpreter. An exception, a malformed answer, or an
answer the core rejects becomes ``AgentFailed`` (LLM effects) or ``EffectFailed`` (the
rest), so the run fails fast with a reason instead of stalling until its deadline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from msa.core import RunState, Step, TransitionRejected, start, transition
from msa.core.effects import CancelEffects, Effect
from msa.core.events import (
    AgentFailed,
    AnyEvent,
    CancelRequested,
    Completion,
    DeadlineExceeded,
    EffectFailed,
    RunStarted,
)
from msa.core.ids import Digest, EffectId, RunId
from msa.core.values import Limits
from msa.runtime.clock import EPOCH, Clock
from msa.runtime.journal import Journal, JournalConflict


class Executor(Protocol):
    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        """Perform ``effect`` and return its completion event.

        ``effect.id`` is an idempotency key: after a crash, an effect that was in flight is
        executed again with the same id.
        """
        ...


class Observer:
    """Hook points around every durable boundary. All no-ops; override what you need.

    Tracing (M9) and the crash-recovery tests both plug in here.
    """

    def effect_started(self, effect: Effect) -> None:
        """An executor task is about to run ``effect``."""

    def before_commit(self, event: AnyEvent, step: Step) -> None:
        """A transition was computed; the event is not yet durable."""

    def after_commit(self, event: AnyEvent, step: Step) -> None:
        """The event is durable; its new effects are not yet launched."""

    def rejected(self, event: AnyEvent, error: TransitionRejected) -> None:
        """The core rejected an event; nothing changed."""


type Inbox = asyncio.Queue[tuple[EffectId | None, AnyEvent]]


class Interpreter:
    def __init__(
        self,
        journal: Journal,
        executor: Executor,
        *,
        clock: Clock,
        observer: Observer | None = None,
        on_outcome: Callable[[RunState], None] | None = None,
        snapshot_every: int = 50,
    ) -> None:
        if snapshot_every < 1:
            raise ValueError("snapshot_every must be positive")
        self._journal = journal
        self._executor = executor
        self._clock = clock
        self._observer = observer or Observer()
        self._on_outcome = on_outcome
        self._snapshot_every = snapshot_every
        self._inbox: Inbox | None = None

    # ---------- entry points

    async def start(
        self,
        *,
        run_id: RunId,
        task_digest: Digest,
        limits: Limits,
        environment: dict[str, Any],
    ) -> RunState:
        first = RunStarted(
            at=self._clock.start_time(), run_id=run_id, task_digest=task_digest, limits=limits
        )
        step = start(first)
        self._observer.before_commit(first, step)
        self._journal.create(environment, first, step.state)
        self._observer.after_commit(first, step)
        return await self._drive(step.state)

    async def resume(self, run_id: RunId) -> RunState:
        return await self._drive(self._journal.load(run_id))

    def request_cancel(self, reason: str) -> None:
        """Cancel the run being driven: a terminal, journaled ABORTED outcome.

        (Interrupting the process instead, e.g. Ctrl-C, leaves the run resumable.)
        """
        if self._inbox is None:
            raise RuntimeError("no run is being driven")
        # Any `at` will do: every event is restamped by the clock when it is committed.
        self._inbox.put_nowait((None, CancelRequested(at=EPOCH, reason=reason)))

    # ---------- the loop

    async def _drive(self, state: RunState) -> RunState:
        inbox: Inbox = asyncio.Queue()
        self._inbox = inbox
        tasks: dict[EffectId, asyncio.Task[None]] = {}
        try:
            while not state.is_terminal:
                for effect in state.pending:
                    if effect.id not in tasks:
                        tasks[effect.id] = asyncio.create_task(self._run(effect, state, inbox))
                source, event = await self._next(state, inbox)
                try:
                    state = self._commit(state, source, event, tasks)
                except JournalConflict:
                    # Another writer (e.g. `msa cancel`) won; its history is the truth.
                    return self._journal.load(state.run_id)
        finally:
            self._inbox = None
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        if self._on_outcome is not None:
            # Published after the loop rather than on the PublishOutcome effect, so that
            # resuming an already-terminal run publishes again: at-least-once delivery.
            self._on_outcome(state)
        return state

    async def _next(self, state: RunState, inbox: Inbox) -> tuple[EffectId | None, AnyEvent]:
        budget = self._clock.wait_budget(state)
        try:
            source, event = await asyncio.wait_for(inbox.get(), timeout=budget)
        except TimeoutError:
            return None, DeadlineExceeded(at=self._clock.stamp(state))
        # The shell owns time: stamp on commit, so concurrent completions stay monotonic.
        return source, event.model_copy(update={"at": self._clock.stamp(state)})

    def _commit(
        self,
        state: RunState,
        source: EffectId | None,
        event: AnyEvent,
        tasks: dict[EffectId, asyncio.Task[None]],
    ) -> RunState:
        try:
            step = transition(state, event)
        except TransitionRejected as rejected:
            self._observer.rejected(event, rejected)
            effect = state.pending_effect(source) if source else None
            if effect is None:
                # Defensive: each task answers once, so a stale or duplicate answer cannot
                # arrive today. If one ever does, dropping it is the correct response.
                return state
            # The executor answered its own pending effect with something the core
            # rejects. That is an executor bug; fail the run loudly rather than stall.
            failure = _failure(effect, f"executor answer rejected: {rejected}", event.at)
            step = transition(state, failure)
            event = failure

        self._observer.before_commit(event, step)
        snapshot = step.state.is_terminal or step.state.seq % self._snapshot_every == 0
        self._journal.append(event, step.state, snapshot=snapshot)
        self._observer.after_commit(event, step)

        if source is not None:
            tasks.pop(source, None)
        for effect in step.effects:
            if isinstance(effect.command, CancelEffects):
                for effect_id in effect.command.effect_ids:
                    task = tasks.pop(effect_id, None)
                    if task is not None:
                        task.cancel()
        return step.state

    async def _run(self, effect: Effect, state: RunState, inbox: Inbox) -> None:
        self._observer.effect_started(effect)
        now = self._clock.stamp(state)
        try:
            event = await self._executor.execute(effect, now=now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # executor bugs become failure events, never crashes
            event = _failure(effect, f"{type(exc).__name__}: {exc}", now)
        else:
            if not (isinstance(event, Completion) and event.effect_id == effect.id):
                detail = f"executor returned {type(event).__name__} for {effect.id}"
                event = _failure(effect, detail, now)
        inbox.put_nowait((effect.id, event))  # unbounded queue: never blocks


def _failure(effect: Effect, error: str, at: datetime) -> AnyEvent:
    if effect.command.spends:
        return AgentFailed(at=at, effect_id=effect.id, error=error)
    return EffectFailed(at=at, effect_id=effect.id, error=error)
