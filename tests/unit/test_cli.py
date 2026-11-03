from __future__ import annotations

import sqlite3
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from msa import __version__, cli
from msa.core import start
from msa.core.graph import render_markdown
from msa.core.table import TransitionTable
from msa.core.transitions import TABLE
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import Profile, SimulationConfig, digest
from tests.support.factory import run_started

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_fsm_check_passes_for_the_production_table() -> None:
    result = runner.invoke(cli.app, ["fsm", "check"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("ok:")


def test_fsm_check_fails_with_reasons_for_a_broken_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "TABLE", TransitionTable(()))
    result = runner.invoke(cli.app, ["fsm", "check"])
    assert result.exit_code == 1
    assert "error: PLANNING is unreachable from INTAKE" in result.output


def test_fsm_diagram_prints_the_generated_page() -> None:
    result = runner.invoke(cli.app, ["fsm", "diagram"])
    assert result.exit_code == 0
    assert result.output == render_markdown(TABLE)


def test_fsm_diagram_writes_to_a_file(tmp_path: Path) -> None:
    target = tmp_path / "fsm.md"
    result = runner.invoke(cli.app, ["fsm", "diagram", "--output", str(target)])
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8") == render_markdown(TABLE)


def test_main_runs_the_app(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["msa", "version"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


# ---------- runs: simulate, runs, inspect, replay, resume, cancel


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "runs.db")


def msa(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


def test_simulate_prints_a_timeline_and_the_outcome(db: str) -> None:
    result = msa("simulate", "--profile", "happy", "--run-id", "r1", "--db", db)
    assert result.exit_code == 0, result.output
    assert "START -> INTAKE" in result.output
    assert "FINALIZING -> DONE" in result.output
    assert result.output.rstrip().endswith("r1: DONE: completed (artifact " + digest("patch") + ")")


def test_simulate_quietly_with_a_generated_run_id(db: str) -> None:
    result = msa("simulate", "--seed", "4", "-q", "--db", db)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("sim-4-")
    assert "transition" not in result.output


def test_simulate_refuses_a_duplicate_run_id(db: str) -> None:
    msa("simulate", "--run-id", "r1", "-q", "--db", db)
    result = msa("simulate", "--run-id", "r1", "-q", "--db", db)
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_runs_lists_every_run(db: str) -> None:
    assert msa("runs", "--db", db).output.strip() == "no runs"
    msa("simulate", "--profile", "happy", "--run-id", "r1", "-q", "--db", db)
    listing = msa("runs", "--db", db).output.splitlines()
    assert listing[0].split()[:4] == ["run", "phase", "seq", "outcome"]
    assert listing[1].split()[:2] == ["r1", "DONE"]


def test_inspect_shows_the_state_at_any_point_in_time(db: str) -> None:
    msa("simulate", "--profile", "happy", "--run-id", "r1", "-q", "--db", db)
    early = msa("inspect", "r1", "--at", "2", "--db", db)
    assert early.exit_code == 0, early.output
    assert "r1: PLANNING (live, seq 2)" in early.output
    assert "r1:2:0 request_plan" in early.output
    final = msa("inspect", "r1", "--json", "--db", db)
    assert '"phase": "done"' in final.output
    assert msa("inspect", "nope", "--db", db).exit_code == 1


def test_replay_verifies_a_journal_and_flags_tampering(db: str) -> None:
    msa("simulate", "--profile", "happy", "--run-id", "r1", "-q", "--db", db)
    ok = msa("replay", "r1", "--db", db)
    assert ok.exit_code == 0, ok.output
    assert ok.output.startswith("ok: ")
    raw = sqlite3.connect(db)
    raw.execute("UPDATE snapshots SET state = replace(state, '\"replans\":0', '\"replans\":1')")
    raw.commit()
    raw.close()
    tampered = msa("replay", "r1", "--db", db)
    assert tampered.exit_code == 1
    assert "divergence: snapshot at seq" in tampered.output
    assert msa("replay", "nope", "--db", db).exit_code == 1


def _live_run(db: str) -> None:
    """A run that crashed right after it was created: journaled up to RunStarted."""
    config = SimulationConfig(seed=0, profile=Profile.HAPPY)
    first = run_started(run_id="live")
    with SqliteJournal(db) as journal:
        journal.create(config.model_dump(mode="json"), first, start(first).state)


def test_resume_drives_a_live_run_to_completion(db: str) -> None:
    _live_run(db)
    result = msa("resume", "live", "-q", "--db", db)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("live: DONE: completed")
    assert msa("resume", "nope", "--db", db).exit_code == 1


def test_resume_refuses_an_environment_it_has_no_executors_for(db: str) -> None:
    first = run_started(run_id="real")
    with SqliteJournal(db) as journal:
        journal.create({"kind": "live", "model": "x"}, first, start(first).state)
    result = msa("resume", "real", "--db", db)
    assert result.exit_code == 1
    assert "no executors for environment kind 'live'" in result.output


def test_cancel_journals_an_aborted_outcome_once(db: str) -> None:
    _live_run(db)
    cancelled = msa("cancel", "live", "--reason", "wrong repo", "--db", db)
    assert cancelled.exit_code == 0, cancelled.output
    assert cancelled.output.strip() == "live: ABORTED: cancelled (wrong repo)"
    again = msa("cancel", "live", "--db", db)
    assert again.exit_code == 1
    assert "cannot cancel: run is aborted" in again.output
    assert msa("cancel", "nope", "--db", db).exit_code == 1


def test_ctrl_c_leaves_the_run_resumable(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(coroutine: Coroutine[Any, Any, Any]) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    _live_run(db)
    monkeypatch.setattr("msa.cli.asyncio.run", interrupted)
    result = msa("resume", "live", "-q", "--db", db)
    assert result.exit_code == 130
    assert "continue with `msa resume live`" in result.output
