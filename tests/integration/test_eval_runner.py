"""msa.evals.runner: whole runs over corpus tasks, with scripted roles, scored by hidden tests.

The scripts stand in for the model; every stage, gate and the hidden-test scoring is real.
The case that matters most: a change the run's own gates accept (the Tester's acceptance
test passes) that the hidden tests still reject is delivered but not resolved.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from msa.config import MsaConfig
from msa.evals import corpus, runner
from msa.llm import fake
from msa.llm.client import MessagesClient
from msa.runtime.live import LiveEnvironment
from msa.tools.workspace import content_hash
from tests.support.llm import config as base_config
from tests.support.runs import RoleScripts, create, read, tool_turn
from tests.support.scripts import ROOT

pytestmark = pytest.mark.slow

EVALS = ROOT / "evals"
WRAP = "src/textkit/wrap.py"
ACCEPTANCE = "tests/acceptance/test_wrap_keeps_lines.py"
BASE = (EVALS / "repos" / "textkit" / WRAP).read_text(encoding="utf-8")
OLD = "            line = word\n    return lines\n"
GOOD = "            line = word\n    if line:\n        lines.append(line)\n    return lines\n"
# Passes the Tester's one acceptance test, but keeps the last line only for one-line text.
WRONG = (
    "            line = word\n    if line and not lines:\n        lines.append(line)\n"
    "    return lines\n"
)
RISK = "'properly' is not defined: I read it as keeping the last line"
PLAN = {
    "summary": "Keep the line left over when wrap's loop ends.",
    "steps": [{"description": "Append the last line after the loop.", "files": [WRAP]}],
    "acceptance_criteria": ["wrap keeps the last line"],
    "risks": [RISK],
}
ACCEPTANCE_TEST = (
    "from textkit import wrap\n\n\n"
    "def test_a_single_word_is_kept() -> None:\n"
    '    assert wrap("hello", 10) == ["hello"]\n'
)


def _edit(new: str) -> dict[str, Any]:
    arguments = {"path": WRAP, "old": OLD, "new": new, "expected_hash": content_hash(BASE.encode())}
    return fake.tool_use("c2", "edit", arguments)


def scripts(fix: str) -> runner.ClientFactory:
    def make(_: MsaConfig, __: LiveEnvironment) -> MessagesClient:
        return RoleScripts(
            planner=[tool_turn(read("p1", WRAP)), fake.answer(PLAN)],
            coder=[
                tool_turn(read("c1", WRAP)),
                tool_turn(_edit(fix)),
                fake.answer(
                    {
                        "summary": "Keep the last line.",
                        "files_changed": [WRAP],
                        "checks_run": [],
                        "open_issues": [],
                    }
                ),
            ],
            tester=[
                tool_turn(create("t1", ACCEPTANCE, ACCEPTANCE_TEST)),
                fake.answer(
                    {
                        "summary": "The last line is kept.",
                        "tests": [f"{ACCEPTANCE}::test_a_single_word_is_kept"],
                        "criteria_covered": ["wrap keeps the last line"],
                        "open_issues": [],
                    }
                ),
            ],
            reviewer=[fake.answer({"summary": "The change is right.", "findings": []})],
        )

    return make


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MsaConfig:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    config = base_config()
    return config.model_copy(
        update={
            "sandbox": config.sandbox.model_copy(update={"launcher": (sys.executable, "-m")}),
            "verify": config.verify.model_copy(
                update={"commands": ("ruff_check", "ruff_format_check")}
            ),
        }
    )


def task(name: str) -> corpus.Task:
    (found,) = corpus.load(EVALS, frozenset({name}))
    return found


@pytest.mark.parametrize("arm", ["B", "D"])
def test_a_right_change_is_delivered_and_resolved(
    arm: str, settings: MsaConfig, tmp_path: Path
) -> None:
    result = asyncio.run(
        runner.run_one(
            task("textkit-wrap-last-line"),
            arm,
            settings,
            tmp_path / "run",
            scripts(GOOD),
            budget_usd=5,
        )
    )
    assert result.error is None, result.error
    assert result.phase == "human_approval"
    assert result.delivered
    assert result.resolved is True
    assert result.hidden_passed >= 4
    assert result.hidden_failed == []
    assert result.spend_usd > 0
    assert result.plan_risks == [RISK]
    assert result.findings == []


def test_a_change_the_gates_accept_but_the_hidden_tests_reject_is_not_resolved(
    settings: MsaConfig, tmp_path: Path
) -> None:
    result = asyncio.run(
        runner.run_one(
            task("textkit-wrap-last-line"),
            "B",
            settings,
            tmp_path / "run",
            scripts(WRONG),
            budget_usd=5,
        )
    )
    assert result.delivered  # every gate of the run passed, the Tester's test included
    assert result.resolved is False
    assert result.hidden_failed  # the hidden tests are what tell
    assert any("the_last_line_is_kept" in t for t in result.hidden_failed)


def test_a_trap_is_not_scored_but_its_plan_risks_are_kept(
    settings: MsaConfig, tmp_path: Path
) -> None:
    result = asyncio.run(
        runner.run_one(
            task("textkit-ambiguous-names"),
            "B",
            settings,
            tmp_path / "run",
            scripts(GOOD),
            budget_usd=5,
        )
    )
    assert result.expect == "flag"
    assert result.resolved is None
    assert result.plan_risks == [RISK]  # what a person reads to judge whether it was flagged


def test_run_all_records_each_run_once_and_resumes_without_paying_twice(
    settings: MsaConfig, tmp_path: Path
) -> None:
    calls: list[str] = []
    good = scripts(GOOD)

    def counted(config: MsaConfig, environment: LiveEnvironment) -> MessagesClient:
        calls.append(Path(environment.repo).parent.name)
        return good(config, environment)

    tasks = [task("textkit-wrap-last-line")]
    first = asyncio.run(
        runner.run_all(tasks, ["B", "D"], settings, tmp_path, counted, budget_usd=5)
    )
    assert [(r.task, r.arm) for r in first] == [
        ("textkit-wrap-last-line", "B"),
        ("textkit-wrap-last-line", "D"),
    ]
    assert len(runner.read_results(tmp_path / runner.RESULTS)) == 2
    again = asyncio.run(
        runner.run_all(tasks, ["B", "D"], settings, tmp_path, counted, budget_usd=5)
    )
    assert again == []
    assert calls == [
        "textkit-wrap-last-line-B-0",
        "textkit-wrap-last-line-D-0",
    ]  # nothing ran twice


def test_a_spending_cap_stops_new_runs(settings: MsaConfig, tmp_path: Path) -> None:
    tasks = [task("textkit-wrap-last-line")]
    done = asyncio.run(
        runner.run_all(
            tasks, ["B"], settings, tmp_path, scripts(GOOD), budget_usd=5, max_total_usd=0
        )
    )
    assert done == []


@pytest.mark.parametrize("arm", ["A", "C", "E"])
def test_an_arm_that_does_not_exist_yet_is_refused_before_anything_runs(
    arm: str, settings: MsaConfig, tmp_path: Path
) -> None:
    def never(_: MsaConfig, __: LiveEnvironment) -> MessagesClient:
        raise AssertionError("no run may start")

    with pytest.raises(runner.ArmUnavailable):
        asyncio.run(
            runner.run_all(
                [task("ledger-statement")], ["B", arm], settings, tmp_path, never, budget_usd=5
            )
        )
    assert not (tmp_path / runner.RESULTS).exists()
