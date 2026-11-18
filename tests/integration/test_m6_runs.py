"""The first slice end to end (PLAN §6.2): whole journaled runs on a fixture repository.

Every executor is real: intake, the run's git worktrees, the Planner, the Coder and (since
M7) the Tester through the gateway and the tool layer, VERIFYING and TESTING with real
ruff and pytest in the sandbox, and finalize. Only the model is scripted. The Reviewer
stage is still a pass-through stand-in.

- I-01: the happy path reaches DONE, writes the report, and its event sequence matches
  ``golden/i01_happy_path.txt`` (regenerate with ``MSA_UPDATE_GOLDEN=1``).
- I-02: a lint failure goes VERIFYING -> REVISING -> VERIFYING -> TESTING, and the Coder's
  revision request carries the linter's report.
- I-14: a refusal ends the run FAILED, with the category in the outcome and the report.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from msa.core.values import OutcomeReason, Phase
from msa.llm import fake
from msa.llm.client import Request
from msa.observability.report import timeline
from tests.support.repos import git
from tests.support.runs import (
    GOOD_GREETER,
    LINTY_GREETER,
    RUN,
    SUMMARY,
    TASK,
    Harness,
    RoleScripts,
    author_script,
    check_golden,
    coder_script,
    edit,
    make_harness,
    phases,
    planner_script,
    prompt_of,
    read,
    tool_results,
    tool_turn,
)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    return make_harness(tmp_path)


def records(harness: Harness, name: str) -> list[dict[str, Any]]:
    text = (harness.run_dir / name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


# ---------- I-01


def test_i01_happy_path_completes_reports_and_matches_the_golden_sequence(
    harness: Harness,
) -> None:
    client = RoleScripts(
        planner=planner_script(), coder=coder_script(GOOD_GREETER), tester=author_script()
    )
    final, journal = harness.run(client)
    with journal:
        assert final.phase is Phase.DONE, final.outcome
        assert final.outcome is not None
        assert final.outcome.reason is OutcomeReason.COMPLETED
        assert client.exhausted
        check_golden("i01_happy_path.txt", timeline(journal.events(RUN)))
        assert journal.load(RUN) == final

    # Every tool call succeeded, including the Coder's own test run in the sandbox.
    coder_results = [r for q in client.requests("coder") for r in tool_results(q)]
    assert len(coder_results) == 4
    assert not any(r.get("is_error") for r in coder_results), coder_results
    assert "2 passed" in coder_results[-1]["content"]

    # The result is a patch the repository accepts; its checkout was never touched.
    patch = (harness.run_dir / "patch.diff").read_text(encoding="utf-8")
    assert "+def farewell(name: str) -> str:" in patch
    assert "+++ b/tests/test_farewell.py" in patch
    assert "+++ b/tests/acceptance/test_goodbye.py" in patch  # the Tester's, integrated
    assert git(harness.repo, "status", "--porcelain") == ""
    git(harness.repo, "apply", "--check", str(harness.run_dir / "patch.diff"))

    report = harness.report()
    assert report.startswith("# Run run-1\n\n**Outcome:** DONE: completed\n")
    for heading in (
        "## Task",
        "## Stand-ins",
        "## Plan",
        "## Timeline",
        "## Agent invocations",
        "## Agent summaries",
        "## Verifications",
        "## Test runs",
        "## Patch",
    ):
        assert heading in report, heading
    assert "- `request_review`: Reviewer (M8): no review; no findings" in report
    assert "author_acceptance_tests`:" not in report  # the Tester is real now
    invocations = records(harness, "invocations.jsonl")
    assert sorted((i["role"], i["status"], i["turns"]) for i in invocations) == [
        ("coder", "ok", 4),
        ("planner", "ok", 2),
        ("tester", "ok", 2),
    ]
    assert harness.violations == []


def test_each_role_sees_its_inputs_and_only_its_tools(harness: Harness) -> None:
    client = RoleScripts(
        planner=planner_script(), coder=coder_script(GOOD_GREETER), tester=author_script()
    )
    harness.run(client)[1].close()
    first = client.requests("planner")[0]
    assert [t["name"] for t in first["tools"]] == ["read_file", "search"]
    assert f"<task>\n{TASK}\n</task>" in prompt_of(first)
    assert "src/greeter.py" in prompt_of(first)
    every_tool = ["create_file", "edit", "git_diff", "read_file", "run", "search"]
    for role in ("coder", "tester"):
        opening = client.requests(role)[0]
        assert [t["name"] for t in opening["tools"]] == every_tool
        assert "Add farewell next to greet" in prompt_of(opening)  # the plan
        # Within a role, the system prompt is the same bytes on every turn: the cache holds.
        assert len({json.dumps(r["system"]) for r in client.requests(role)}) == 1


# ---------- I-02


def test_i02_lint_failure_is_revised_then_verified(harness: Harness) -> None:
    revision = [
        tool_turn(read("r1", "src/greeter.py")),
        tool_turn(edit("r2", "src/greeter.py", "\nimport os\n", "", LINTY_GREETER)),
        fake.answer({**SUMMARY, "summary": "Removed the unused import."}),
    ]
    client = RoleScripts(
        planner=planner_script(),
        coder=[*coder_script(LINTY_GREETER), *revision],
        tester=author_script(),
    )
    final, journal = harness.run(client)
    with journal:
        assert final.phase is Phase.DONE, final.outcome
        lines = timeline(journal.events(RUN))
    seen = phases(lines)
    i = seen.index("VERIFYING -> REVISING")
    assert seen[i - 1 : i + 3] == [
        "BUILDING -> VERIFYING",
        "VERIFYING -> REVISING",
        "REVISING -> VERIFYING",
        "VERIFYING -> TESTING",
    ]
    assert final.revisions == 1

    # The revision request carried the linter's own words.
    prompt = prompt_of(client.requests("coder")[len(coder_script(""))])
    assert "<failure_report>" in prompt
    assert "ruff_check: FAILED" in prompt
    assert "F401" in prompt
    verifications = records(harness, "verifications.jsonl")
    assert [(v["passed"], v["failed"]) for v in verifications] == [
        (False, ["ruff_check"]),
        (True, []),
    ]
    patch = (harness.run_dir / "patch.diff").read_text(encoding="utf-8")
    assert "import os" not in patch


# ---------- I-14


def test_i14_a_refusal_fails_the_run_with_its_category(harness: Harness) -> None:
    client = RoleScripts(planner=[fake.refusal("cyber")])
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.FAILED
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.AGENT_REFUSED
    assert final.outcome.detail == "cyber"
    report = harness.report()
    assert "**Outcome:** FAILED: agent_refused\n**Detail:** cyber" in report
    assert "## Patch" not in report


# ---------- boundaries the tool layer keeps


def test_the_coder_cannot_write_tester_paths_or_the_worktree_git_file(harness: Harness) -> None:
    denied = tool_turn(
        fake.tool_use("d1", "create_file", {"path": "tests/acceptance/test_x.py", "content": ""}),
        read("d2", ".git"),  # in a linked worktree, a file naming the git directory
    )
    client = RoleScripts(
        planner=planner_script(),
        coder=[
            tool_turn(read("c0", "src/greeter.py")),
            denied,
            _edit_git_file,
            *coder_script(GOOD_GREETER)[1:],
        ],
        tester=author_script(),
    )
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.DONE, final.outcome
    errors = [r for q in client.requests("coder") for r in tool_results(q) if r.get("is_error")]
    assert [e["content"].split(":")[0] for e in errors] == ["CapabilityDenied", "CapabilityDenied"]
    assert "belongs to tester" in errors[0]["content"]
    assert "protected" in errors[1]["content"]
    assert [v.tool for v in harness.violations] == ["create_file", "edit"]


def _edit_git_file(request: Request) -> dict[str, Any]:
    """Quote the hash the read of .git returned, as a real model would, and try to edit it."""
    shown = next(r for r in tool_results(request) if r["tool_use_id"] == "d2")["content"]
    found = re.search(r"sha256:[0-9a-f]{64}", shown)
    assert found is not None, shown
    arguments = {
        "path": ".git",
        "old": "gitdir",
        "new": "gitdir: /tmp/x #",
        "expected_hash": found[0],
    }
    return tool_turn(fake.tool_use("d3", "edit", arguments))
