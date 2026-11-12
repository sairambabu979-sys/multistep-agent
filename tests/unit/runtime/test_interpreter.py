"""The shell's failure handling: every way an executor can misbehave ends the run with a
reason, never a crash or a silent stall."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from msa.cli import app
from msa.core import RunState, TransitionRejected
from msa.core.effects import Effect, RunTestSuite
from msa.core.events import AnyEvent, CancelRequested, TestsPassed, VerificationPassed
from msa.core.ids import EffectId, RunId
from msa.core.values import OutcomeReason, Phase
from msa.runtime.clock import LogicalClock, SystemClock
from msa.runtime.interpreter import Interpreter, Observer
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import (
    BASE,
    Profile,
    SimulatedEnvironment,
    SimulationConfig,
    digest,
)
from tests.support.driver import Driver
from tests.support.factory import limits, run_started

RUN = RunId("run-1")
HAPPY = SimulationConfig(seed=0, profile=Profile.HAPPY)


class Scripted:
    """The happy simulation, except for one effect kind, which misbehaves as instructed."""

    def __init__(self, kind: str, behavior: str) -> None:
        self.kind, self.behavior = kind, behavior
        self.cancelled: list[EffectId] = []
        self.calls: list[str] = []
        self._happy = SimulatedEnvironment(HAPPY)

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        self.calls.append(effect.command.kind)
        if effect.command.kind != self.kind:
            return await self._happy.execute(effect, now=now)
        match self.behavior:
            case "raise":
                raise RuntimeError("boom")
            case "wrong-id":
                event = self._happy.answer(effect, now)
                return event.model_copy(update={"effect_id": EffectId("run-1:999:0")})
            case "stale-tree":
                return VerificationPassed(at=now, effect_id=effect.id, tree=BASE)
            case "hang":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.append(effect.id)
                    raise
        raise AssertionError(self.behavior)  # pragma: no cover


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[SqliteJournal]:
    with SqliteJournal(tmp_path / "runs.db") as opened:
        yield opened


def run(interpreter: Interpreter, **limit_overrides: object) -> RunState:
    return asyncio.run(
        interpreter.start(
            run_id=RUN,
            task_digest=digest("t"),
            limits=limits(**limit_overrides),
            environment=HAPPY.model_dump(mode="json"),
        )
    )


def test_the_happy_path_completes_and_publishes_once(journal: SqliteJournal) -> None:
    published: list[RunState] = []
    interpreter = Interpreter(
        journal, SimulatedEnvironment(HAPPY), clock=LogicalClock(), on_outcome=published.append
    )
    final = run(interpreter)
    assert final.phase is Phase.DONE
    assert published == [final]
    assert journal.load(RUN) == final


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("request_plan", OutcomeReason.AGENT_FAILED),  # an LLM effect
        ("integrate_build", OutcomeReason.EFFECT_FAILED),  # a non-LLM effect
    ],
)
def test_an_executor_exception_fails_the_run_with_its_message(
    journal: SqliteJournal, kind: str, reason: OutcomeReason
) -> None:
    final = run(Interpreter(journal, Scripted(kind, "raise"), clock=LogicalClock()))
    assert final.outcome is not None
    assert (final.outcome.reason, final.outcome.detail) == (reason, "RuntimeError: boom")


def test_an_answer_for_the_wrong_effect_fails_the_run(journal: SqliteJournal) -> None:
    final = run(
        Interpreter(journal, Scripted("run_verification", "wrong-id"), clock=LogicalClock())
    )
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.EFFECT_FAILED
    assert "executor returned VerificationPassed" in final.outcome.detail


def test_an_answer_the_core_rejects_fails_the_run_instead_of_stalling(
    journal: SqliteJournal,
) -> None:
    rejected: list[str] = []

    class Watch(Observer):
        def rejected(self, event: AnyEvent, error: TransitionRejected) -> None:
            rejected.append(str(error))

    interpreter = Interpreter(
        journal, Scripted("run_verification", "stale-tree"), clock=LogicalClock(), observer=Watch()
    )
    final = run(interpreter)
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.EFFECT_FAILED
    assert final.outcome.detail.startswith("executor answer rejected: stale_evidence")
    assert len(rejected) == 1


def test_a_hung_effect_is_cut_off_by_the_wall_clock_deadline(journal: SqliteJournal) -> None:
    executor = Scripted("implement_plan", "hang")
    interpreter = Interpreter(journal, executor, clock=SystemClock())
    final = run(interpreter, wall_clock=timedelta(milliseconds=200))
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.DEADLINE_EXCEEDED
    assert len(executor.cancelled) == 1  # the hung executor was cancelled, not leaked


def test_request_cancel_aborts_the_run_and_cancels_in_flight_work(journal: SqliteJournal) -> None:
    executor = Scripted("implement_plan", "hang")
    interpreter = Interpreter(journal, executor, clock=LogicalClock())

    async def scenario() -> RunState:
        driving = asyncio.create_task(
            interpreter.start(
                run_id=RUN,
                task_digest=digest("t"),
                limits=limits(),
                environment=HAPPY.model_dump(mode="json"),
            )
        )
        while "implement_plan" not in executor.calls:
            await asyncio.sleep(0)
        interpreter.request_cancel("operator")
        return await driving

    final = asyncio.run(scenario())
    assert final.outcome is not None
    assert (final.outcome.reason, final.outcome.detail) == (OutcomeReason.CANCELLED, "operator")
    assert len(executor.cancelled) == 1
    assert journal.load(RUN) == final


def test_request_cancel_needs_a_run_in_progress(journal: SqliteJournal) -> None:
    interpreter = Interpreter(journal, SimulatedEnvironment(HAPPY), clock=LogicalClock())
    with pytest.raises(RuntimeError, match="no run is being driven"):
        interpreter.request_cancel("too early")


def test_resuming_a_finished_run_executes_nothing_and_publishes_again(
    journal: SqliteJournal,
) -> None:
    final = run(Interpreter(journal, SimulatedEnvironment(HAPPY), clock=LogicalClock()))
    executor = Scripted("none", "raise")
    published: list[RunState] = []
    again = Interpreter(journal, executor, clock=LogicalClock(), on_outcome=published.append)
    assert asyncio.run(again.resume(RUN)) == final
    assert executor.calls == []
    assert published == [final]


def test_a_rival_writer_wins_and_the_interpreter_adopts_its_history(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    """`msa cancel` from another process lands mid-run: the next commit conflicts, and the
    interpreter returns the journal's (cancelled) state instead of overwriting it."""

    class CancelsFromOutside:
        def __init__(self) -> None:
            self._happy = SimulatedEnvironment(HAPPY)

        async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
            command = effect.command
            if isinstance(command, RunTestSuite):
                # The real command, through its own connection, as another process would.
                result = CliRunner().invoke(app, ["cancel", RUN, "--db", str(journal.path)])
                assert result.exit_code == 0, result.output
                return TestsPassed(at=now, effect_id=effect.id, tree=command.tree)
            return await self._happy.execute(effect, now=now)

    final = run(Interpreter(journal, CancelsFromOutside(), clock=LogicalClock()))
    assert final.phase is Phase.ABORTED
    assert journal.load(RUN) == final


def test_snapshot_interval_must_be_positive(journal: SqliteJournal) -> None:
    with pytest.raises(ValueError, match="positive"):
        Interpreter(journal, SimulatedEnvironment(HAPPY), clock=LogicalClock(), snapshot_every=0)


def test_logical_time_makes_runs_reproducible(tmp_path: Path) -> None:
    finals = []
    for name in ("a", "b"):
        with SqliteJournal(tmp_path / f"{name}.db") as journal:
            config = SimulationConfig(seed=42)
            interpreter = Interpreter(journal, SimulatedEnvironment(config), clock=LogicalClock())
            finals.append(
                asyncio.run(
                    interpreter.start(
                        run_id=RUN,
                        task_digest=digest("t"),
                        limits=limits(),
                        environment=config.model_dump(mode="json"),
                    )
                )
            )
    assert finals[0] == finals[1]


def _live(journal: SqliteJournal) -> RunState:
    d = Driver()
    d.intake()
    d.plan()  # BUILDING: two effects pending
    journal.create({"kind": "test"}, run_started(), d.steps[0].state)
    for event, step in zip(d.events[1:], d.steps[1:], strict=True):
        journal.append(event, step.state, snapshot=False)
    return d.state


def test_defensive_a_stale_answer_is_dropped_without_touching_the_journal(
    journal: SqliteJournal,
) -> None:
    state = _live(journal)
    seen: list[str] = []

    class Watch(Observer):
        def rejected(self, event: AnyEvent, error: TransitionRejected) -> None:
            seen.append(error.reason)

    interpreter = Interpreter(
        journal, SimulatedEnvironment(HAPPY), clock=LogicalClock(), observer=Watch()
    )
    stale = VerificationPassed(at=state.updated_at, effect_id=EffectId("run-1:1:0"), tree=BASE)
    assert interpreter._commit(state, EffectId("run-1:1:0"), stale, {}) is state
    assert seen == ["unknown_effect"]
    assert journal.load(RUN) == state


def test_defensive_cancelling_effects_without_running_tasks_is_harmless(
    journal: SqliteJournal,
) -> None:
    state = _live(journal)
    interpreter = Interpreter(journal, SimulatedEnvironment(HAPPY), clock=LogicalClock())
    cancel = CancelRequested(at=state.updated_at, reason="x")
    after = interpreter._commit(state, None, cancel, {})
    assert after.phase is Phase.ABORTED
    assert journal.load(RUN) == after
