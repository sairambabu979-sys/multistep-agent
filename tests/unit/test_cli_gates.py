"""The human side of a run on the command line: pause and resume, unblock, approve a
release, and the refusal of any answer that does not come from a person (ADR-015).

Each run is journaled from a scripted history (``Driver``) under a simulated environment,
so ``--resume`` carries it on with the simulator, as ``msa resume`` would a crashed run.
The command line stamps events with the real clock, and the scripted history starts on a
fixed date, so these runs get a wall clock of years: a run past its deadline would
(rightly) expire on the next event that is not a person's answer."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from msa import cli
from msa.core import RunState
from msa.core.effects import RunIntegrationTests
from msa.core.events import IntegrationTestsFailed
from msa.core.ids import RunId
from msa.core.values import FailureKind, Phase, Triage
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import Profile, SimulationConfig
from tests.support.driver import Driver
from tests.support.factory import BUILD, digest, limits

runner = CliRunner()
RUN = "run-1"
YEARS = limits(wall_clock=timedelta(days=3650))


def msa(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "runs.db")


def journaled(db: str, d: Driver) -> None:
    """Journal ``d``'s history as a simulated run's, so it can be resumed."""
    environment = SimulationConfig(seed=0, profile=Profile.HAPPY).model_dump(mode="json")
    with SqliteJournal(db) as journal:
        journal.create(environment, d.events[0], d.steps[0].state)  # type: ignore[arg-type]
        for event, step in zip(d.events[1:], d.steps[1:], strict=True):
            journal.append(event, step.state, snapshot=False)


def load(db: str) -> RunState:
    with SqliteJournal(db) as journal:
        return journal.load(RunId(RUN))


def at_release() -> Driver:
    d = Driver(YEARS)
    d.to_review()
    d.review()
    d.verify_release()
    return d


def blocked() -> Driver:
    d = Driver(YEARS)
    d.intake()
    d.plan()
    d.build()
    d.static()
    d.unit()
    d.complete(
        RunIntegrationTests,
        IntegrationTestsFailed,
        tree=BUILD,
        triage=Triage(kind=FailureKind.ENVIRONMENT, owner=None),
        report=digest("no database"),
    )
    return d


# ---------- pause and resume


def test_pause_then_resume_continues_where_the_run_stopped(db: str) -> None:
    d = Driver(YEARS)
    d.intake()
    journaled(db, d)
    paused = msa("pause", RUN, "--by", "grace", "--reason", "deploy freeze", "--db", db)
    assert paused.exit_code == 0, paused.output
    assert paused.output.strip() == f"{RUN}: PAUSED (live, seq 5)"
    state = load(db)
    assert (state.phase, state.resume_phase) == (Phase.PAUSED, Phase.PLANNING)

    again = msa("pause", RUN, "--db", db)
    assert again.exit_code == 1
    assert "cannot pause: paused is already waiting on a person" in again.output

    resumed = msa("resume", RUN, "--by", "grace", "-q", "--db", db)
    assert resumed.exit_code == 0, resumed.output
    assert resumed.output.startswith(f"{RUN}: COMPLETED: completed")
    final = load(db)
    assert final.paused > state.paused  # the pause stopped the clock


def test_a_run_waiting_on_a_person_cannot_be_paused(db: str) -> None:
    journaled(db, at_release())
    result = msa("pause", RUN, "--db", db)
    assert result.exit_code == 1
    assert "cannot pause: human_approval is already waiting on a person" in result.output


# ---------- unblock


def test_unblock_reruns_the_blocked_stage(db: str) -> None:
    journaled(db, blocked())
    wrong = msa("approve", RUN, "--db", db)
    assert wrong.exit_code == 1
    assert "is not waiting for await_plan_approval or await_release_approval" in wrong.output
    result = msa(
        "unblock", RUN, "--note", "started postgres", "--by", "ops", "--no-resume", "--db", db
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"{RUN}: INTEGRATION_TESTING (live, seq 12)"


def test_unblock_can_stop_the_run_instead(db: str) -> None:
    journaled(db, blocked())
    result = msa("unblock", RUN, "--abort", "no database here", "--by", "ops", "--db", db)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"{RUN}: CANCELLED: human_aborted (ops: no database here)"


# ---------- the release


def test_approving_a_release_names_the_approver_and_publishes(db: str) -> None:
    journaled(db, at_release())
    result = msa("approve", RUN, "--by", "grace", "--no-resume", "--db", db)
    assert result.exit_code == 0, result.output
    state = load(db)
    assert state.approved_by == "grace"
    assert [e.command.kind for e in state.pending] == ["publish_release"]
    published = msa("resume", RUN, "-q", "--db", db)
    assert published.output.startswith(f"{RUN}: COMPLETED: completed (aegis/")


def test_a_rejected_release_goes_back_for_rework_with_the_feedback(db: str) -> None:
    journaled(db, at_release())
    result = msa(
        "approve", RUN, "--reject", "rename it", "--by", "grace", "--no-resume", "--db", db
    )
    assert result.exit_code == 0, result.output
    state = load(db)
    assert state.phase is Phase.REWORK
    (revise,) = state.pending
    assert revise.command.model_dump()["feedback"] == "grace: rename it"


@pytest.mark.parametrize("who", ["agent:coder", "agent:release-verifier", " "])
def test_no_agent_and_nobody_can_answer(db: str, who: str) -> None:
    journaled(db, at_release())
    for command in (["approve", RUN], ["unblock", RUN], ["rule", RUN, "c0-1=upheld"]):
        result = msa(*command, "--by", who, "--db", db)
        assert result.exit_code == 1, (command, result.output)
        assert "cannot answer: only a person can" in result.output
    assert load(db).approved_by is None
