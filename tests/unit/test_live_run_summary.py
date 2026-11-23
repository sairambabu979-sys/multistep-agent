"""scripts/live_run_summary.py: what the `live run` workflow reports, and when it goes red."""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path
from types import ModuleType

from msa.core.events import CancelRequested
from msa.core.ids import RunId
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import Profile, SimulationConfig
from tests.support.driver import Driver
from tests.support.factory import limits

ROOT = Path(__file__).resolve().parents[2]
RUN = RunId("run-1")


def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "live_run_summary", ROOT / "scripts" / "live_run_summary.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def journaled(db: Path, d: Driver) -> None:
    environment = SimulationConfig(seed=0, profile=Profile.HAPPY).model_dump(mode="json")
    with SqliteJournal(db) as journal:
        journal.create(environment, d.events[0], d.steps[0].state)  # type: ignore[arg-type]
        for event, step in zip(d.events[1:], d.steps[1:], strict=True):
            journal.append(event, step.state, snapshot=False)


def test_a_run_waiting_on_its_release_approval_reached_the_gate(tmp_path: Path) -> None:
    d = Driver(limits(wall_clock=timedelta(days=3650)))
    d.to_review()
    d.review()
    d.verify_release()
    db = tmp_path / "runs.db"
    journaled(db, d)
    (tmp_path / "runs" / RUN).mkdir(parents=True)
    (tmp_path / "runs" / RUN / "pr.md").write_text("# Add restock\n\nThe evidence.\n")
    summary = tmp_path / "summary.md"

    assert script().main(["--db", str(db), "--run", RUN, "--summary", str(summary)]) == 0
    text = summary.read_text(encoding="utf-8")
    assert "| phase | **human_approval** |" in text
    assert "waiting for a person to approve its release" in text
    assert "no workflow approves it" in text
    assert "# Add restock" in text  # the PR description, for the approver


def test_a_run_that_never_reached_the_gate_is_a_failure(tmp_path: Path) -> None:
    d = Driver(limits())
    d.intake()
    d.send(CancelRequested(at=d.tick(), reason="stopped"))
    db = tmp_path / "runs.db"
    journaled(db, d)

    text, reached = script().summarize(db, RUN, tmp_path / "runs" / RUN)
    assert not reached
    assert "| phase | **cancelled** |" in text
    assert "## The PR description" not in text  # none was written
