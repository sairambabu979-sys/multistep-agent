"""The activities ``RunWorkflow`` starts: everything that touches the world.

The journal stays the system of record, so ``msa inspect``, the API and the workbench read
a Temporal-driven run exactly as they read any other. Each activity is safe to retry, as
Temporal will after a worker dies:

- ``create`` and ``commit`` recognize their own earlier attempt in the journal, so a retry
  after a lost reply is not mistaken for another writer;
- ``execute`` runs an effect only while the journal still has it pending, so an effect
  whose completion was journaled is never executed again, the interpreter's guarantee;
- ``settle`` writes reports, which are rewritten whole.

Executors come from a factory of the shape the service uses (run, environment, journal ->
interpreter), so simulated and live runs are built here exactly as anywhere else.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from msa.core.ids import EffectId, RunId
from msa.runtime.codec import CURRENT_EVENT_VERSION, decode_event, decode_state, encode_state
from msa.runtime.interpreter import Interpreter, execute_effect
from msa.runtime.journal import DuplicateRun, JournalConflict, SqlJournal
from msa.runtime.temporal.workflow import CommitInput, CreateInput, ExecuteInput, decode, encode

# (run, its stored environment, the journal) -> an interpreter for it, open while in use
type Factory = Callable[
    [RunId, dict[str, Any], SqlJournal], AbstractAsyncContextManager[Interpreter]
]


def _heartbeat_every() -> float:
    """A third of the heartbeat timeout the workflow set, so two may be lost."""
    timeout = activity.info().heartbeat_timeout
    return timeout.total_seconds() / 3 if timeout else 10.0


class Activities:
    def __init__(self, journal: Callable[[], SqlJournal], factory: Factory) -> None:
        self.journal = journal  # opens a connection to the run journal
        self.factory = factory

    def all(self) -> list[Callable[..., Any]]:
        return [self.create, self.load, self.execute, self.commit, self.settle]

    @activity.defn(name="create")
    async def create(self, args: CreateInput) -> str:
        first = decode(args.first)
        state = decode_state(args.state)
        with self.journal() as journal:
            try:
                journal.create(json.loads(args.environment), first, state)  # type: ignore[arg-type]
            except DuplicateRun as exc:
                if journal.events(state.run_id, upto=1) != [first]:
                    raise ApplicationError(str(exc), non_retryable=True) from exc
        return "ok"  # created now, or by an earlier attempt of this same activity

    @activity.defn(name="load")
    async def load(self, run_id: str) -> str:
        with self.journal() as journal:
            return encode_state(journal.load(RunId(run_id)))

    @activity.defn(name="execute")
    async def execute(self, args: ExecuteInput) -> str:
        run_id = RunId(args.run_id)
        with self.journal() as journal:
            effect = journal.load(run_id).pending_effect(EffectId(args.effect_id))
            if effect is None:
                return json.dumps({"stale": True})
            environment = journal.environment(run_id)
            async with self.factory(run_id, environment, journal) as interpreter:
                now = datetime.fromisoformat(args.now)
                work = asyncio.ensure_future(execute_effect(interpreter.executor, effect, now=now))
                try:
                    while not (await asyncio.wait({work}, timeout=_heartbeat_every()))[0]:
                        activity.heartbeat()  # alive; and how cancellation reaches us
                except asyncio.CancelledError:
                    work.cancel()
                    raise
                event = work.result()
        if event is None:
            return json.dumps({"parked": True})
        return json.dumps({"event": encode(event)})

    @activity.defn(name="commit")
    async def commit(self, args: CommitInput) -> str:
        event = decode_event(args.kind, CURRENT_EVENT_VERSION, args.event)
        state = decode_state(args.state)
        with self.journal() as journal:
            try:
                journal.append(event, state, snapshot=args.snapshot)
            except JournalConflict:
                landed = journal.events(state.run_id, after=state.seq - 1, upto=state.seq)
                if landed == [event] and journal.load(state.run_id, upto=state.seq) == state:
                    return "ok"  # an earlier attempt of this commit landed; its reply was lost
                return "conflict"
        return "ok"

    @activity.defn(name="settle")
    async def settle(self, run_id: str) -> str:
        """The run stopped advancing: a terminal run publishes its outcome, and a parked one
        writes what it waits for (``report.md`` for live runs)."""
        rid = RunId(run_id)
        with self.journal() as journal:
            state = journal.load(rid)
            async with self.factory(rid, journal.environment(rid), journal) as interpreter:
                interpreter.settled(state)
        return "ok"
