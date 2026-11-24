""".github/workflows/live-run.yml: the properties of its human gate, held as tests.

The release job approves a run in a person's name, so what it can do and when it runs are
security properties: a person must hold it (an environment), it must name only who approved
it, and it must never hold the model key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from tests.support.scripts import ROOT

WORKFLOW = ROOT / ".github" / "workflows" / "live-run.yml"


def workflow() -> dict[str, Any]:
    loaded: dict[str, Any] = YAML(typ="safe").load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def steps_text(job: dict[str, Any]) -> str:
    return "\n".join(str(step) for step in job["steps"])


def test_the_release_waits_for_a_person_and_names_only_who_approved() -> None:
    release = workflow()["jobs"]["release"]
    assert release["environment"] == "live-release"  # GitHub holds it for the reviewer
    assert release["needs"] == "run"
    assert release["if"] == "needs.run.outputs.phase == 'human_approval'"
    assert release["permissions"] == {"contents": "read", "actions": "read"}
    text = steps_text(release)
    assert "live_run_approver.py live-release" in text
    assert 'approve "$RUN_ID" --by "$APPROVER"' in text
    assert text.index("live_run_approver.py") < text.index('approve "$RUN_ID"')


def test_the_release_never_holds_the_model_key() -> None:
    jobs = workflow()["jobs"]
    assert "secrets." not in str(jobs["release"])
    assert "ANTHROPIC_API_KEY" in str(jobs["run"])  # only the run spends


def test_only_a_run_that_reached_the_gate_saves_its_state_for_release() -> None:
    run = workflow()["jobs"]["run"]
    saved = [s for s in run["steps"] if "live-state" in str(s)]
    assert saved
    assert all(s["if"] == "steps.summary.outputs.phase == 'human_approval'" for s in saved)
    assert run["outputs"]["phase"] == "${{ steps.summary.outputs.phase }}"


def test_every_task_it_offers_exists() -> None:
    task = workflow()["on"]["workflow_dispatch"]["inputs"]["task"]
    for name in task["options"]:
        assert Path(ROOT / ".github" / "live-run" / f"{name}.md").is_file(), name
    assert task["default"] in task["options"]


def test_a_release_nobody_approved_reports_nothing_it_never_restored() -> None:
    release = workflow()["jobs"]["release"]
    after = [s for s in release["steps"] if s.get("if", "").startswith("always()")]
    assert after  # the summary and the upload still run when publishing fails
    assert all("steps.restore.outcome == 'success'" in s["if"] for s in after)


def test_a_seeded_run_says_so_first_and_takes_only_the_restock_task_uncrashed() -> None:
    run = workflow()["jobs"]["run"]
    steps = {step.get("name", ""): str(step.get("run", "")) for step in run["steps"]}
    inputs = steps["Check the inputs"]
    assert "seeded runs take the restock task and crash_after_seq 0" in inputs
    assert (
        "scripts/live_run_seeded.py"
        in steps["The run (real model, every stage), crashed and resumed"]
    )
    summary = steps["Outcome, spend, the crash and the PR description"]
    assert summary.index("was planted") < summary.index("live_run_summary.py")


def test_only_the_spending_job_waits_its_turn_a_waiting_release_holds_nobody() -> None:
    loaded = workflow()
    assert "concurrency" not in loaded  # not the whole workflow
    assert loaded["jobs"]["run"]["concurrency"] == {
        "group": "live-run-spend",
        "cancel-in-progress": False,
    }
    assert "concurrency" not in loaded["jobs"]["release"]
