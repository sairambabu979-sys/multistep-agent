"""The Temporal activities, run in-process by the SDK's ``ActivityEnvironment``: no server.

Each is safe for Temporal to retry, as it will after a worker dies:

- ``create`` and ``commit`` recognize their own earlier attempt in the journal, and tell
  it apart from another writer's;
- ``execute`` runs an effect only while the journal still has it pending, parks a
  person's question, and turns executor bugs into failure events, as the interpreter does;
- ``settle`` publishes the outcome, or what a parked run waits for.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

temporalio = pytest.importorskip("temporalio")

from temporalio.exceptions import ApplicationError  # noqa: E402
from temporalio.testing import ActivityEnvironment  # noqa: E402

from msa.core import RunState, start, transition  # noqa: E402
from msa.core.effects import Effect  # noqa: E402
from msa.core.events import AnyEvent, CancelRequested, EffectFailed, RunStarted  # noqa: E402
from msa.core.ids import EffectId, RunId  # noqa: E402
from msa.runtime.clock import EPOCH, LogicalClock  # noqa: E402
from msa.runtime.codec import decode_state, encode_event, encode_state, event_kind  # noqa: E402
from msa.runtime.interpreter import Interpreter, Parked  # noqa: E402
from msa.runtime.journal import SqlJournal  # noqa: E402
from msa.runtime.simulation import SimulatedEnvironment, SimulationConfig, digest  # noqa: E402
from msa.runtime.temporal.activities import Activities  # noqa: E402
from msa.runtime.temporal.workflow import (  # noqa: E402
    CommitInput,
    CreateInput,
    ExecuteInput,
    decode,
    encode,
)
from tests.support.factory import limits  # noqa: E402
from tests.support.journals import BACKENDS, Opener, opener_for  # noqa: E402

RUN = RunId("run-a")
CONFIG = SimulationConfig(seed=0)
ENVIRONMENT = json.dumps(CONFIG.model_dump(mode="json"))


class Broken:
    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        raise RuntimeError("the executor fell over")


class Asks:
    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        raise Parked("waiting for a person")


@pytest.fixture(params=BACKENDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Opener]:
    yield from opener_for(request.param, tmp_path / "journal")


class World:
    """A factory whose executor can be swapped, and what settle was told."""

    def __init__(self) -> None:
        self.executions: Counter[EffectId] = Counter()
        self.executor: Any = None
        self.parked: list[RunState] = []
        self.outcomes: list[RunState] = []

    @asynccontextmanager
    async def make(
        self, _: RunId, environment: dict[str, Any], journal: SqlJournal
    ) -> AsyncIterator[Interpreter]:
        simulated = SimulatedEnvironment(
            SimulationConfig.model_validate(environment),
            on_execute=lambda e: self.executions.update([e.id]),
        )
        yield Interpreter(
            journal,
            self.executor or simulated,
            clock=LogicalClock(),
            on_park=self.parked.append,
            on_outcome=self.outcomes.append,
        )


def first_step() -> tuple[RunStarted, RunState]:
    first = RunStarted(at=EPOCH, run_id=RUN, task_digest=digest("t"), limits=limits())
    return first, start(first).state


def run(fn: Any, *args: Any) -> Any:
    environment = ActivityEnvironment()
    environment.info = dataclasses.replace(environment.info, heartbeat_timeout=timedelta(seconds=3))
    return asyncio.run(environment.run(fn, *args))


def created(store: Opener, world: World) -> tuple[Activities, RunState]:
    activities = Activities(store, world.make)
    first, state = first_step()
    assert run(activities.create, CreateInput(RUN, ENVIRONMENT, encode(first), encode_state(state)))
    return activities, state


def test_a_retried_create_is_recognized_and_a_different_run_is_refused(store: Opener) -> None:
    world = World()
    activities, state = created(store, world)
    first, _ = first_step()
    again = CreateInput(RUN, ENVIRONMENT, encode(first), encode_state(state))
    assert run(activities.create, again) == "ok"  # the lost reply's retry
    other = first.model_copy(update={"task_digest": digest("another task")})
    with pytest.raises(ApplicationError, match="already exists") as refused:
        run(activities.create, CreateInput(RUN, ENVIRONMENT, encode(other), again.state))
    assert refused.value.non_retryable
    assert decode_state(run(activities.load, RUN)) == state


def test_an_effect_runs_once_and_is_stale_once_journaled(store: Opener) -> None:
    world = World()
    activities, state = created(store, world)
    (admit,) = state.pending
    asked = ExecuteInput(RUN, admit.id, EPOCH.isoformat())
    answer = json.loads(run(activities.execute, asked))
    event = decode(answer["event"])
    step = transition(state, event)
    commit = CommitInput(
        RUN, event_kind(event), encode_event(event), encode_state(step.state), snapshot=False
    )
    assert run(activities.commit, commit) == "ok"
    assert run(activities.commit, commit) == "ok"  # a retry of a commit that landed
    assert json.loads(run(activities.execute, asked)) == {"stale": True}  # never again
    assert world.executions == Counter({admit.id: 1})


def test_a_rival_commit_is_a_conflict_not_a_retry(store: Opener) -> None:
    world = World()
    activities, state = created(store, world)
    (admit,) = state.pending
    event = decode(
        json.loads(
            run(activities.execute, ExecuteInput(RUN, admit.id, "2026-01-01T00:00:01+00:00"))
        )["event"]
    )
    ours = transition(state, event)
    run(
        activities.commit,
        CommitInput(
            RUN, event_kind(event), encode_event(event), encode_state(ours.state), snapshot=False
        ),
    )
    rival = CancelRequested(at=EPOCH + timedelta(seconds=5), reason="someone else")
    theirs = transition(state, rival)
    conflict = CommitInput(
        RUN, event_kind(rival), encode_event(rival), encode_state(theirs.state), snapshot=False
    )
    assert run(activities.commit, conflict) == "conflict"
    assert decode_state(run(activities.load, RUN)) == ours.state


def test_questions_park_and_executor_bugs_become_failures(store: Opener) -> None:
    world = World()
    activities, state = created(store, world)
    (admit,) = state.pending
    asked = ExecuteInput(RUN, admit.id, EPOCH.isoformat())
    world.executor = Asks()
    assert json.loads(run(activities.execute, asked)) == {"parked": True}
    world.executor = Broken()
    failed = decode(json.loads(run(activities.execute, asked))["event"])
    assert isinstance(failed, EffectFailed)
    assert failed.effect_id == admit.id
    assert failed.error == "RuntimeError: the executor fell over"


def test_settle_publishes_an_outcome_or_what_a_parked_run_waits_for(store: Opener) -> None:
    world = World()
    activities, state = created(store, world)
    assert run(activities.settle, RUN) == "ok"
    assert [s.seq for s in world.parked] == [state.seq]
    assert world.outcomes == []
    cancel = CancelRequested(at=EPOCH + timedelta(seconds=1), reason="stop")
    over = transition(state, cancel).state
    run(
        activities.commit,
        CommitInput(
            RUN, event_kind(cancel), encode_event(cancel), encode_state(over), snapshot=True
        ),
    )
    run(activities.settle, RUN)
    assert [s.phase for s in world.outcomes] == [over.phase]
