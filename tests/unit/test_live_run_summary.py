"""scripts/live_run_summary.py: what the `live run` workflow reports, and when it goes red."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from msa.core.events import CancelRequested
from msa.core.ids import RunId
from msa.runtime.artifacts import RunFiles
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import Profile, SimulationConfig
from tests.support.driver import Driver
from tests.support.factory import limits
from tests.support.scripts import load

RUN = RunId("run-1")


def script() -> ModuleType:
    return load("live_run_summary")


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


# ---------- a run crashed at seq 6 (implementation answered, acceptance tests pending)

CRASH = {
    "run": RUN,
    "after_seq": 6,
    "seq": 6,
    "event": "ImplementationCompleted",
    "phase": "implementing",
    "pending": [{"effect": "run-1:5:1", "kind": "author_acceptance_tests"}],
    "processes": 5,
    "effects": 6,  # seq 1 to 5 issued six effects, and every one of them had started
    "invocations": 2,  # the planner's and the coder's, before the crash
}
BEFORE = ["run-1:1:0", "run-1:2:0", "run-1:3:0", "run-1:4:0", "run-1:5:0", "run-1:5:1"]
AFTER = ["run-1:5:1", "run-1:7:0", "run-1:8:0", "run-1:9:0", "run-1:10:0", "run-1:11:0"]


def invocation(effect: str, role: str, turns: int = 1, replayed: int = 0) -> dict[str, Any]:
    return {
        "effect": effect,
        "kind": "-",
        "role": role,
        "status": "ok",
        "turns": turns,
        "replayed": replayed,
        "usd_micros": 1000,
    }


def jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def crashed(tmp_path: Path, started: list[str], *after: dict[str, Any]) -> Path:
    """A run that was crashed at seq 6, then resumed: ``started`` effects after the crash,
    and ``after`` agent invocations."""
    d = Driver(limits(wall_clock=timedelta(days=3650)))
    d.to_review()
    d.review()
    d.verify_release()
    db = tmp_path / "runs.db"
    journaled(db, d)
    run_dir = tmp_path / "runs" / RUN
    run_dir.mkdir(parents=True)
    jsonl(run_dir / "effects.jsonl", [{"effect": e, "kind": "-"} for e in BEFORE + started])
    before = [invocation("run-1:4:0", "planner"), invocation("run-1:5:0", "coder")]
    jsonl(run_dir / "invocations.jsonl", [*before, *after])
    return db


def test_a_resume_that_kept_to_the_journal_is_reported_with_its_replayed_turns(
    tmp_path: Path,
) -> None:
    db = crashed(
        tmp_path,
        AFTER,
        invocation("run-1:5:1", "tester", turns=4, replayed=3),  # the pending one, again
        invocation("run-1:12:0", "reviewer"),
    )
    text, reached = script().summarize(db, RUN, tmp_path / "runs" / RUN, CRASH)
    assert reached
    assert "(msa and 4 process(es) it had started) was SIGKILLed" in text
    assert "SIGKILLed once the journal held seq 6, after `ImplementationCompleted`" in text
    assert "to seq 14" in text
    assert "Pending at the crash: `run-1:5:1` (author_acceptance_tests)." in text
    assert "answered before the crash (5): none started again" in text  # seq 2 to 6
    assert "Started again, as in flight: `run-1:5:1`." in text
    assert "4 turn(s), 3 of them replayed from the response store" in text
    assert "| `run-1:5:1` | tester | ok | 4 | 3 | $0.0010 |" in text
    assert "run-1:5:0" not in text.split("| effect |", 1)[1]  # before the crash: not listed
    timeline = text.split("## Timeline", 1)[1].splitlines()
    marker = next(i for i, line in enumerate(timeline) if "SIGKILL" in line)
    assert timeline[marker - 1].startswith("  6  ")
    assert timeline[marker + 1].startswith("  7  ")


def test_an_answered_effect_started_again_after_the_crash_fails_the_run(tmp_path: Path) -> None:
    db = crashed(
        tmp_path,
        ["run-1:5:0", *AFTER],
        invocation("run-1:5:0", "coder"),
        invocation("run-1:5:1", "tester"),
    )
    evidence = tmp_path / "crash.json"
    evidence.write_text(json.dumps(CRASH | {"processes": 1}), encoding="utf-8")
    summary = tmp_path / "summary.md"
    argv = ["--db", str(db), "--run", RUN, "--crash", str(evidence), "--summary", str(summary)]

    assert script().main(argv) == 1  # waiting at the gate, but the resume broke ADR-007
    text = summary.read_text(encoding="utf-8")
    assert "| phase | **human_approval** |" in text
    assert "(msa alone: no sandboxed command was running at that instant)" in text
    assert "**Started again after the crash, though the journal held their answers: " in text
    assert "`run-1:5:0`.** ADR-007 says this never happens." in text


def test_a_stage_without_a_model_started_again_is_caught_too(tmp_path: Path) -> None:
    # load_context calls no model, so no invocation records it: only the effect log can.
    db = crashed(
        tmp_path, ["run-1:3:0", *AFTER], invocation("run-1:5:1", "tester", turns=4, replayed=3)
    )
    text, reached = script().summarize(db, RUN, tmp_path / "runs" / RUN, CRASH)
    assert not reached
    assert "though the journal held their answers: `run-1:3:0`." in text


def test_a_completed_run_names_its_approver_and_reports_its_phase(tmp_path: Path) -> None:
    d = Driver(limits(wall_clock=timedelta(days=3650)))
    d.to_review()
    d.review()
    d.verify_release()
    d.approve_release(by="github:ada")
    d.publish()
    db = tmp_path / "runs.db"
    journaled(db, d)
    output = tmp_path / "github-output"
    output.write_text("before=1\n", encoding="utf-8")

    assert script().main(["--db", str(db), "--run", RUN, "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == "before=1\nphase=completed\n"  # appended
    text, reached = script().summarize(db, RUN, tmp_path / "runs" / RUN)
    assert reached
    assert "Approved by `github:ada`, and published." in text


def test_a_line_the_kill_tore_neither_stops_the_summary_nor_hides_what_followed(
    tmp_path: Path,
) -> None:
    db = crashed(tmp_path, [], invocation("run-1:5:1", "tester"))
    run_dir = tmp_path / "runs" / RUN
    with (run_dir / "effects.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"effect": "run-1:5:1", "ki')  # torn by the kill
    RunFiles(run_dir).record("effects.jsonl", {"effect": "run-1:3:0", "kind": "-"})  # resumed

    text, reached = script().summarize(db, RUN, run_dir, CRASH)
    assert not reached  # the restart after the torn line is still seen
    assert "though the journal held their answers: `run-1:3:0`." in text
