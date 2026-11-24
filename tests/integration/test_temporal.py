"""The Temporal driver (ADR-017), against a real Temporal server.

The server is Temporal's own test server, which the SDK downloads when the tests start.
This container may not run downloaded server binaries, so the tests run where
``MSA_TEST_TEMPORAL=1`` (CI sets it, and then a failure to start the server fails them
instead of skipping them).

What they show:

1. **The same run.** A simulated run driven by Temporal ends in exactly the state the
   in-process interpreter reaches with the same seed: timestamps, ledger, effect ids.
   Each effect runs once, and the journal it wrote verifies. On both journal backends.
2. **Deterministic.** The workflow's recorded history replays without a
   nondeterminism error.
3. **People answer by signal.** A run parks on a person, keeps waiting on the server, and
   carries on when the answer is signalled. An answer the run is not waiting for is
   dropped by the core, and a cancel ends the run.
4. **A worker that dies.** A worker process is SIGKILLed in the middle of an effect. A
   second worker takes the run over: the effect in flight runs again, nothing already
   journaled runs again, and the run ends exactly as the uninterrupted one does. This one
   runs on Temporal's real dev server (also downloaded by the SDK), which times out the
   dead worker's heartbeats as a production server does.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import socket
import time
from collections import Counter
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from msa.core import RunState
from msa.core.effects import AwaitPlanApproval, AwaitReleaseApproval, Effect
from msa.core.events import (
    AnyEvent,
    CancelRequested,
    PlanApproved,
    ReleaseApproved,
)
from msa.core.ids import EffectId, RunId
from msa.core.values import OutcomeReason, Phase
from msa.runtime.clock import EPOCH, LogicalClock
from msa.runtime.codec import decode_state
from msa.runtime.interpreter import Interpreter, Parked
from msa.runtime.journal import SqliteJournal, SqlJournal
from msa.runtime.simulation import Profile, SimulatedEnvironment, SimulationConfig, digest
from tests.support.factory import limits
from tests.support.journals import BACKENDS, Opener, opener_for

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("MSA_TEST_TEMPORAL") != "1",
        reason="set MSA_TEST_TEMPORAL=1 to run against Temporal's test server (CI does); "
        "the SDK downloads it, and this environment may not run downloaded servers",
    ),
]

temporal = pytest.importorskip("temporalio")

from temporalio.client import Client  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Replayer  # noqa: E402
from temporalio.worker.workflow_sandbox import (  # noqa: E402
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from msa.runtime.temporal.activities import Factory  # noqa: E402
from msa.runtime.temporal.client import (  # noqa: E402
    PASSTHROUGH,
    send,
    start_run,
    state_of,
    worker,
)
from msa.runtime.temporal.workflow import RunWorkflow  # noqa: E402

RUN = RunId("run-t")
LIMITS = limits(wall_clock=LogicalClock().tick * 10_000)  # never reached on the logical clock
SEEDS = (0, 3, 7)


# ---------- the world, simulated


def simulation(seed: int, profile: Profile = Profile.HAPPY) -> SimulationConfig:
    return SimulationConfig(seed=seed, profile=profile)


class People:
    """The simulator, except that every question for a person parks."""

    def __init__(self, inner: SimulatedEnvironment) -> None:
        self.inner = inner

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        if effect.command.human:
            raise Parked(f"waiting for a person: {effect.command.kind}")
        return await self.inner.execute(effect, now=now)


def factory(
    executions: Counter[EffectId],
    *,
    people: bool = False,
    parks: list[RunState] | None = None,
) -> Factory:
    @asynccontextmanager
    async def make(
        _: RunId, environment: dict[str, Any], journal: SqlJournal
    ) -> AsyncIterator[Interpreter]:
        world = SimulatedEnvironment(
            SimulationConfig.model_validate(environment),
            on_execute=lambda e: executions.update([e.id]),
        )
        yield Interpreter(
            journal,
            People(world) if people else world,
            clock=LogicalClock(),
            on_park=parks.append if parks is not None else None,
        )

    return make


def uninterrupted(directory: Path, seed: int) -> RunState:
    """The reference: the in-process interpreter, on the same seed."""
    config = simulation(seed)
    with SqliteJournal(directory / f"reference-{seed}.db") as journal:
        interpreter = Interpreter(
            journal, SimulatedEnvironment(config), clock=LogicalClock(), snapshot_every=3
        )
        return asyncio.run(
            interpreter.start(
                run_id=RUN,
                task_digest=digest("t"),
                limits=LIMITS,
                environment=config.model_dump(mode="json"),
            )
        )


async def begin(client: Client, config: SimulationConfig, **kwargs: Any) -> Any:
    return await start_run(
        client,
        RUN,
        task_digest=digest("t"),
        limits=LIMITS,
        environment=config.model_dump(mode="json"),
        clock="logical",
        snapshot_every=3,
        **kwargs,
    )


SCENARIO_SECONDS = 180  # a stuck workflow fails the test with a traceback, not the job


def within[T](scenario: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(scenario, SCENARIO_SECONDS))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(params=BACKENDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Opener]:
    yield from opener_for(request.param, tmp_path / "journal")


# ---------- 1 and 2: the same run, and deterministic


@pytest.mark.parametrize("seed", SEEDS)
def test_a_temporal_run_ends_exactly_as_the_interpreter_does(
    seed: int, tmp_path: Path, store: Opener
) -> None:
    reference = uninterrupted(tmp_path, seed)
    executions: Counter[EffectId] = Counter()

    async def scenario() -> tuple[RunState, Any]:
        async with (
            await WorkflowEnvironment.start_time_skipping() as env,
            worker(env.client, journal=store, factory=factory(executions)),
        ):
            handle = await begin(env.client, simulation(seed))
            final = decode_state(await handle.result())
            history = await handle.fetch_history()
        return final, history

    final, history = within(scenario())
    assert final == reference
    assert set(executions.values()) == {1}, executions  # each effect ran exactly once
    with store() as journal:
        assert journal.load(RUN) == final
        assert journal.verify(RUN).ok

    replayer = Replayer(
        workflows=[RunWorkflow],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(*PASSTHROUGH)
        ),
    )
    asyncio.run(replayer.replay_workflow(history))  # raises on any nondeterminism


# ---------- 3: people answer by signal


async def parked_on(
    client: Client, kinds: tuple[type, ...], reported: list[RunState] | None = None
) -> Effect:
    """The run's pending question for a person, once the workflow has parked on it.

    A question is pending as soon as the core asks it, before its activity has come back
    parked. A test that answers then still passes, since the workflow takes signals first,
    but the run never parks on that question. With ``reported`` (the worker's park reports),
    wait until the run has also reported parking on it. The parked-run test once failed in CI
    by answering first."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = await state_of(client, RUN)
        if state is not None:
            questions = [e for e in state.pending if isinstance(e.command, kinds)]
            if questions and all(e.command.human for e in state.pending):
                question = questions[0]
                if reported is None or any(
                    question.id in {e.id for e in park.pending} for park in reported
                ):
                    return question
        await asyncio.sleep(0.1)
    raise AssertionError("the run never parked on a person")


def test_a_parked_run_waits_on_the_server_and_carries_on_when_answered(store: Opener) -> None:
    executions: Counter[EffectId] = Counter()
    parks: list[RunState] = []
    config = simulation(0)

    async def scenario() -> RunState:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = env.client
            async with worker(
                client, journal=store, factory=factory(executions, people=True, parks=parks)
            ):
                handle = await begin(client, config)
                while True:
                    question = await parked_on(
                        client, (AwaitPlanApproval, AwaitReleaseApproval), reported=parks
                    )
                    # An answer for an effect the run is not waiting on: the core drops it.
                    stray = ReleaseApproved(at=EPOCH, effect_id=EffectId("run-t:999:0"), by="eve")
                    await send(client, RUN, stray)
                    if isinstance(question.command, AwaitPlanApproval):
                        answer: AnyEvent = PlanApproved(at=EPOCH, effect_id=question.id, by="ada")
                        await send(client, RUN, answer)
                        continue
                    answer = ReleaseApproved(at=EPOCH, effect_id=question.id, by="ada")
                    await send(client, RUN, answer)
                    return decode_state(await handle.result())

    final = within(scenario())
    assert final.phase is Phase.COMPLETED, final.outcome
    assert final.approved_by == "ada"  # not eve: her stray answer named no pending effect
    assert parks, "a parked run reports what it waits for"
    with store() as journal:
        kinds = [type(e).__name__ for e in journal.events(RUN)]
        assert "ReleaseApproved" in kinds
        assert journal.verify(RUN).ok


def test_a_cancel_signal_ends_a_parked_run(store: Opener) -> None:
    executions: Counter[EffectId] = Counter()

    async def scenario() -> RunState:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = env.client
            async with worker(client, journal=store, factory=factory(executions, people=True)):
                handle = await begin(client, simulation(0))
                await parked_on(client, (AwaitPlanApproval, AwaitReleaseApproval))
                await send(client, RUN, CancelRequested(at=EPOCH, reason="not needed after all"))
                return decode_state(await handle.result())

    final = within(scenario())
    assert final.phase is Phase.CANCELLED
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.CANCELLED
    with store() as journal:
        assert journal.load(RUN) == final


# ---------- 4: a worker that dies


BLOCKED = "run_unit_tests"  # the effect the first worker never finishes


def _dying_worker(port: int, db: str, log: str) -> None:
    """A worker in a process of its own, which hangs in the middle of ``BLOCKED``."""

    @asynccontextmanager
    async def make(
        _: RunId, environment: dict[str, Any], journal: SqlJournal
    ) -> AsyncIterator[Interpreter]:
        world = SimulatedEnvironment(SimulationConfig.model_validate(environment))

        class Hangs:
            async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
                with open(log, "a", encoding="utf-8") as f:
                    f.write(f"{effect.id} {effect.command.kind}\n")
                if effect.command.kind == BLOCKED:
                    await asyncio.Event().wait()  # until the process is killed
                return await world.execute(effect, now=now)

        yield Interpreter(journal, Hangs(), clock=LogicalClock())

    async def serve() -> None:
        client = await Client.connect(f"127.0.0.1:{port}")
        await worker(client, journal=lambda: SqliteJournal(db), factory=make).run()

    asyncio.run(serve())


def test_a_killed_worker_hands_the_run_to_another(tmp_path: Path) -> None:
    seed = 0
    reference = uninterrupted(tmp_path, seed)
    db, log = tmp_path / "runs.db", tmp_path / "first-worker.log"
    port = free_port()
    executions: Counter[EffectId] = Counter()

    async def scenario() -> tuple[RunState, set[EffectId]]:
        # The real dev server, not the time-skipping test server: a dead worker is noticed
        # only by its missing heartbeats, which the test server does not time out.
        async with await WorkflowEnvironment.start_local(port=port) as env:
            client = env.client
            child = multiprocessing.get_context("spawn").Process(
                target=_dying_worker, args=(port, str(db), str(log))
            )
            child.start()
            handle = await begin(client, simulation(seed), heartbeat_seconds=3)
            deadline = time.monotonic() + 60
            while BLOCKED not in (log.read_text() if log.exists() else ""):
                assert time.monotonic() < deadline, "the first worker never reached the effect"
                await asyncio.sleep(0.1)
            assert child.pid is not None
            os.kill(child.pid, signal.SIGKILL)  # no cleanup, no flush, no reply
            child.join(timeout=10)
            with SqliteJournal(db) as journal:
                done = {e.effect_id for e in journal.events(RUN) if hasattr(e, "effect_id")}
            async with worker(
                client, journal=lambda: SqliteJournal(db), factory=factory(executions)
            ):
                final = decode_state(await handle.result())
            return final, done

    final, journaled_before = within(scenario())
    assert final == reference
    assert not set(executions) & journaled_before, "a journaled effect ran again"
    first = [line.split() for line in log.read_text().splitlines()]
    blocked = next(EffectId(i) for i, kind in first if kind == BLOCKED)
    assert executions[blocked] == 1  # the effect in flight ran again, once
    with SqliteJournal(db) as journal:
        assert journal.verify(RUN).ok


# ---------- 5: the service and the CLI, over Temporal


def test_the_api_starts_runs_on_temporal_and_its_approvals_reach_the_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from msa.config import TemporalConfig  # noqa: PLC0415
    from msa.runtime.temporal.client import workflow_id  # noqa: PLC0415
    from msa.service.app import Settings, create_app  # noqa: PLC0415
    from msa.service.tokens import Role, TokenStore  # noqa: PLC0415

    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("MSA_USER_SKILLS", str(tmp_path / "none"))
    state_dir, repo, port = tmp_path / "state", tmp_path / "repo", free_port()
    repo.mkdir()
    store = TokenStore(state_dir / "tokens.json")
    operator = {"Authorization": f"Bearer {store.issue('olu', Role.OPERATOR)}"}
    approver = {"Authorization": f"Bearer {store.issue('ada', Role.APPROVER)}"}
    settings = Settings(
        repo=repo,
        state_dir=state_dir,
        poll_seconds=0.01,
        clock=LogicalClock(),
        temporal=TemporalConfig(address=f"127.0.0.1:{port}"),
    )
    executions: Counter[EffectId] = Counter()

    async def scenario() -> tuple[dict[str, Any], RunState]:
        async with (
            await WorkflowEnvironment.start_time_skipping(port=port) as env,
            worker(
                env.client,
                journal=settings.open_journal,
                factory=factory(executions, people=True),
            ),
        ):
            with TestClient(create_app(settings)) as api:
                body = {"task": "Add farewell()", "run_id": RUN, "simulate": {"seed": 0}}
                started = await asyncio.to_thread(
                    api.post, "/api/runs", json=body, headers=operator
                )
                assert started.status_code == 200, started.text
                assert started.json()["driver"] == "temporal"
                while True:
                    run = (
                        await asyncio.to_thread(api.get, f"/api/runs/{RUN}", headers=operator)
                    ).json()
                    if run.get("waiting_on_person") and "await_release_approval" in run["pending"]:
                        break
                    if "await_plan_approval" in run.get("pending", []):
                        await asyncio.to_thread(
                            api.post, f"/api/runs/{RUN}/approve", headers=approver
                        )
                    await asyncio.sleep(0.1)
                approved = await asyncio.to_thread(
                    api.post, f"/api/runs/{RUN}/approve", headers=approver
                )
                assert approved.status_code == 200, approved.text
                handle = env.client.get_workflow_handle(workflow_id(RUN))
                return approved.json(), decode_state(await handle.result())

    answer, final = within(scenario())
    assert answer["driver"] == "temporal"  # signalled, not journaled by the service
    assert final.phase is Phase.COMPLETED, final.outcome
    assert final.approved_by == "ada"
    with settings.open_journal() as journal:
        assert journal.load(RUN) == final
        assert journal.verify(RUN).ok


def test_the_cli_starts_answers_and_refuses_to_double_drive_a_temporal_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner  # noqa: PLC0415

    from msa.cli import app  # noqa: PLC0415
    from tests.support.llm import ROOT  # noqa: PLC0415

    port = free_port()
    config = tmp_path / "msa.toml"
    text = (ROOT / "msa.toml").read_text(encoding="utf-8")
    text = text.replace('# address = "localhost:7233"', f'address = "127.0.0.1:{port}"')
    config.write_text(text, encoding="utf-8")
    db = tmp_path / "state" / "runs.db"
    cli = CliRunner()
    executions: Counter[EffectId] = Counter()

    def msa(*args: str) -> Any:
        return cli.invoke(app, ["--config", str(config), *args, "--db", str(db)])

    async def scenario() -> RunState:
        async with (
            await WorkflowEnvironment.start_time_skipping(port=port) as env,
            worker(
                env.client,
                journal=lambda: SqliteJournal(db),
                factory=factory(executions, people=True),
            ),
        ):
            started = await asyncio.to_thread(
                msa, "simulate", "--seed", "0", "--profile", "happy", "--run-id", RUN, "--temporal"
            )
            assert started.exit_code == 0, started.output
            assert "started on Temporal" in started.output
            question = await parked_on(env.client, (AwaitPlanApproval, AwaitReleaseApproval))
            twice = await asyncio.to_thread(msa, "resume", RUN)
            assert twice.exit_code == 1
            assert "driven by Temporal" in twice.output
            while isinstance(question.command, AwaitPlanApproval):
                done = await asyncio.to_thread(msa, "approve", RUN, "--by", "ada")
                assert done.exit_code == 0, done.output
                question = await parked_on(env.client, (AwaitPlanApproval, AwaitReleaseApproval))
            done = await asyncio.to_thread(msa, "approve", RUN, "--by", "ada")
            assert done.exit_code == 0, done.output
            assert "sent to the run's Temporal workflow" in done.output
            return decode_state(await env.client.get_workflow_handle(f"aegis-{RUN}").result())

    final = within(scenario())
    assert final.phase is Phase.COMPLETED, final.outcome
    assert final.approved_by == "ada"
