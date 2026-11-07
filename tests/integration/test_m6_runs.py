"""M6 end to end (PLAN §6.2): whole journaled runs on a fixture repository.

Every executor is real: intake, the run's git worktree, the Planner and the Coder through
the gateway and the tool layer, VERIFYING with real ruff and pytest in the sandbox, and
finalize. Only the model is scripted (``ScriptedClient``). The Tester and Reviewer stages
are M6's pass-through stand-ins.

- I-01: the happy path reaches DONE, writes the report, and its event sequence matches
  ``golden/i01_happy_path.txt`` (regenerate with ``MSA_UPDATE_GOLDEN=1``).
- I-02: a lint failure goes VERIFYING -> REVISING -> VERIFYING -> TESTING, and the Coder's
  revision request carries the linter's report.
- I-14: a refusal ends the run FAILED, with the category in the outcome and the report.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from msa.config import MsaConfig
from msa.core import RunState
from msa.core.ids import RunId
from msa.core.values import OutcomeReason, Phase
from msa.llm import fake
from msa.llm.client import Request
from msa.llm.store import MemoryResponseStore
from msa.observability.report import timeline
from msa.runtime import live
from msa.runtime.clock import LogicalClock
from msa.runtime.journal import SqliteJournal
from msa.tools.audit import Violation
from msa.tools.workspace import content_hash
from tests.support.llm import config as base_config
from tests.support.repos import git, make_repo

GOLDEN = Path(__file__).parent / "golden"
RUN = RunId("run-1")
TASK = 'Add farewell(name) to src/greeter.py, returning "goodbye <name>", with a test.'

BASE_GREETER = '"""Greetings."""\n\n\ndef greet(name: str) -> str:\n    return f"hello {name}"\n'
FAREWELL = '\n\ndef farewell(name: str) -> str:\n    return f"goodbye {name}"\n'
GOOD_GREETER = BASE_GREETER + FAREWELL
LINTY_GREETER = GOOD_GREETER.replace('"""Greetings."""\n', '"""Greetings."""\n\nimport os\n')
FAREWELL_TEST = (
    "from greeter import farewell\n\n\ndef test_farewell() -> None:\n"
    '    assert farewell("ada") == "goodbye ada"\n'
)

PLAN = {
    "summary": "Add farewell next to greet, and a test for it.",
    "steps": [
        {"description": "Add farewell(name) to the greeter module.", "files": ["src/greeter.py"]},
        {"description": "Test farewell.", "files": ["tests/test_farewell.py"]},
    ],
    "acceptance_criteria": ['farewell("ada") returns "goodbye ada"'],
    "risks": [],
}
SUMMARY = {
    "summary": "Added farewell and its test.",
    "files_changed": ["src/greeter.py", "tests/test_farewell.py"],
    "checks_run": ["pytest: 2 passed"],
    "open_issues": [],
}


def tool_turn(*calls: dict[str, Any]) -> dict[str, Any]:
    return fake.message(fake.thinking(), *calls, stop_reason="tool_use")


def read(call_id: str, path: str) -> dict[str, Any]:
    return fake.tool_use(call_id, "read_file", {"path": path, "start_line": None, "end_line": None})


def edit(call_id: str, path: str, old: str, new: str, before: str) -> dict[str, Any]:
    arguments = {
        "path": path,
        "old": old,
        "new": new,
        "expected_hash": content_hash(before.encode()),
    }
    return fake.tool_use(call_id, "edit", arguments)


def planner_script() -> list[fake.Step]:
    return [tool_turn(read("p1", "src/greeter.py")), fake.answer(PLAN)]


def coder_script(new_greeter: str) -> list[fake.Step]:
    return [
        tool_turn(read("c1", "src/greeter.py")),
        tool_turn(
            edit("c2", "src/greeter.py", BASE_GREETER, new_greeter, BASE_GREETER),
            fake.tool_use(
                "c3", "create_file", {"path": "tests/test_farewell.py", "content": FAREWELL_TEST}
            ),
        ),
        tool_turn(fake.tool_use("c4", "run", {"command": "pytest", "paths": [], "keyword": None})),
        fake.answer(SUMMARY),
    ]


def tool_results(request: Request) -> list[dict[str, Any]]:
    content = request["messages"][-1]["content"]
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]


@dataclass
class Harness:
    repo: Path
    state_dir: Path
    config: MsaConfig
    violations: list[Violation]

    def run(self, client: fake.ScriptedClient) -> tuple[RunState, SqliteJournal]:
        environment = live.LiveEnvironment(
            repo=str(self.repo), state_dir=str(self.state_dir), task=TASK
        )
        journal = SqliteJournal(self.state_dir / "runs.db")
        interpreter = live.interpreter(
            environment,
            self.config,
            run_id=RUN,
            journal=journal,
            client=client,
            store=MemoryResponseStore(),
            clock=LogicalClock(),
            on_violation=self.violations.append,
        )
        final = asyncio.run(
            interpreter.start(
                run_id=RUN,
                task_digest=environment.task_digest,
                limits=self.config.limits.to_limits(),
                environment=environment.model_dump(mode="json"),
            )
        )
        return final, journal

    @property
    def run_dir(self) -> Path:
        return self.state_dir / "runs" / RUN


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    base = base_config()
    config = base.model_copy(
        update={
            "sandbox": base.sandbox.model_copy(update={"launcher": (sys.executable, "-m")}),
            "verify": base.verify.model_copy(
                update={"commands": ("ruff_check", "ruff_format_check", "pytest")}
            ),
        }
    )
    return Harness(make_repo("greeter", tmp_path / "repo"), tmp_path / "state", config, [])


def check_golden(name: str, lines: list[str]) -> None:
    path = GOLDEN / name
    text = "\n".join(lines) + "\n"
    if os.environ.get("MSA_UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


# ---------- I-01


def test_i01_happy_path_completes_reports_and_matches_the_golden_sequence(
    harness: Harness,
) -> None:
    client = fake.ScriptedClient(*planner_script(), *coder_script(GOOD_GREETER))
    final, journal = harness.run(client)
    with journal:
        assert final.phase is Phase.DONE, final.outcome
        assert final.outcome is not None
        assert final.outcome.reason is OutcomeReason.COMPLETED
        assert client.exhausted
        check_golden("i01_happy_path.txt", timeline(journal.events(RUN)))
        assert journal.load(RUN) == final

    # Every tool call succeeded, including the Coder's own test run in the sandbox.
    results = [r for request in client.requests for r in tool_results(request)]
    assert len(results) == 5
    assert not any(r.get("is_error") for r in results), results
    assert "2 passed" in results[-1]["content"]

    # The result is a patch the repository accepts; its checkout was never touched.
    patch = (harness.run_dir / "patch.diff").read_text(encoding="utf-8")
    assert "+def farewell(name: str) -> str:" in patch
    assert "+++ b/tests/test_farewell.py" in patch
    assert git(harness.repo, "status", "--porcelain") == ""
    git(harness.repo, "apply", "--check", str(harness.run_dir / "patch.diff"))

    report = (harness.run_dir / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# Run run-1\n\n**Outcome:** DONE: completed\n")
    for heading in (
        "## Task",
        "## Stand-ins",
        "## Plan",
        "## Timeline",
        "## Agent invocations",
        "## Coder summaries",
        "## Verifications",
        "## Patch",
    ):
        assert heading in report
    assert "- `request_review`: Reviewer (M8): no review; no findings" in report
    invocations = [
        json.loads(line)
        for line in (harness.run_dir / "invocations.jsonl").read_text().splitlines()
    ]
    assert [(i["role"], i["status"], i["turns"]) for i in invocations] == [
        ("planner", "ok", 2),
        ("coder", "ok", 4),
    ]
    assert harness.violations == []


def test_the_planner_sees_the_task_and_files_and_cannot_write(harness: Harness) -> None:
    client = fake.ScriptedClient(*planner_script(), *coder_script(GOOD_GREETER))
    harness.run(client)[1].close()
    first = client.requests[0]
    assert [t["name"] for t in first["tools"]] == ["read_file", "search"]
    prompt = first["messages"][0]["content"]
    assert f"<task>\n{TASK}\n</task>" in prompt
    assert "src/greeter.py" in prompt
    coder_first = client.requests[2]
    assert [t["name"] for t in coder_first["tools"]] == [
        "create_file",
        "edit",
        "git_diff",
        "read_file",
        "run",
        "search",
    ]
    assert "Add farewell next to greet" in coder_first["messages"][0]["content"]  # the plan
    # Within a role, the system prompt is the same bytes on every turn: the cache holds.
    assert len({json.dumps(r["system"]) for r in client.requests[2:]}) == 1


# ---------- I-02


def test_i02_lint_failure_is_revised_then_verified(harness: Harness) -> None:
    revision = [
        tool_turn(read("r1", "src/greeter.py")),
        tool_turn(edit("r2", "src/greeter.py", "\nimport os\n", "", LINTY_GREETER)),
        fake.answer({**SUMMARY, "summary": "Removed the unused import."}),
    ]
    client = fake.ScriptedClient(*planner_script(), *coder_script(LINTY_GREETER), *revision)
    final, journal = harness.run(client)
    with journal:
        assert final.phase is Phase.DONE, final.outcome
        lines = timeline(journal.events(RUN))
    phases = [line.strip().split("  ")[1] for line in lines]  # "SOURCE -> TARGET"
    i = phases.index("VERIFYING -> REVISING")
    assert phases[i - 1 : i + 3] == [
        "BUILDING -> VERIFYING",
        "VERIFYING -> REVISING",
        "REVISING -> VERIFYING",
        "VERIFYING -> TESTING",
    ]
    assert final.revisions == 1

    # The revision request carried the linter's own words, and started from the linty tree.
    revision_request = client.requests[len(planner_script()) + len(coder_script(""))]
    prompt = revision_request["messages"][0]["content"]
    assert "<failure_report>" in prompt
    assert "ruff_check: FAILED" in prompt
    assert "F401" in prompt
    verifications = [
        json.loads(line)
        for line in (harness.run_dir / "verifications.jsonl").read_text().splitlines()
    ]
    assert [(v["passed"], v["failed"]) for v in verifications] == [
        (False, ["ruff_check"]),
        (True, []),
    ]
    patch = (harness.run_dir / "patch.diff").read_text(encoding="utf-8")
    assert "import os" not in patch


# ---------- I-14


def test_i14_a_refusal_fails_the_run_with_its_category(harness: Harness) -> None:
    client = fake.ScriptedClient(fake.refusal("cyber"))
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.FAILED
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.AGENT_REFUSED
    assert final.outcome.detail == "cyber"
    report = (harness.run_dir / "report.md").read_text(encoding="utf-8")
    assert "**Outcome:** FAILED: agent_refused\n**Detail:** cyber" in report
    assert "## Patch" not in report


# ---------- boundaries the stand-ins and the tool layer keep


def test_the_coder_cannot_write_tester_paths_or_the_worktree_git_file(harness: Harness) -> None:
    denied = tool_turn(
        fake.tool_use("d1", "create_file", {"path": "tests/acceptance/test_x.py", "content": ""}),
        read("d2", ".git"),  # in a linked worktree, a file naming the git directory
    )
    client = fake.ScriptedClient(
        *planner_script(),
        tool_turn(read("c0", "src/greeter.py")),
        denied,
        _edit_git_file,
        *coder_script(GOOD_GREETER)[1:],
    )
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.DONE, final.outcome
    errors = [r for request in client.requests for r in tool_results(request) if r.get("is_error")]
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
