"""The live executors' edges: the stand-in, effects with no executor, refusals to guess,
and the harness's own command lines and test reports."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from msa.config import MsaConfig
from msa.core.effects import (
    AwaitPlanApproval,
    Effect,
    Finalize,
    RequestReview,
    ReviseChanges,
    RunTestSuite,
    RunVerification,
)
from msa.core.events import ReviewProduced, TestsFailed
from msa.core.ids import EffectId, FindingId, RunId, TreeHash
from msa.core.values import FailureKind, Owner
from msa.llm import fake
from msa.llm.gateway import Gateway
from msa.llm.store import MemoryResponseStore
from msa.runtime import live
from msa.runtime.context import RunContext, reservation
from msa.runtime.intake import IntakeExecutor
from msa.runtime.roles import CoderExecutor, Revisions, TesterExecutor
from msa.runtime.testing import TestSuiteExecutor, parse_junit
from msa.runtime.verify import VerifyExecutor, check_argv, outside
from msa.tools.errors import CommandUnavailable
from msa.tools.paths import Root
from msa.tools.sandbox import LocalSandbox
from tests.support.factory import BASE, T0, digest, plan_ref
from tests.support.llm import config as base_config
from tests.support.repos import git, make_repo


def effect(command: object, budget: int | None = None) -> Effect:
    return Effect.model_validate(
        {"id": "run-1:5:0", "budget_usd_micros": budget, "command": command}
    )


def test_the_review_stand_in_passes_through_and_names_itself() -> None:
    stand_ins = live.StandIns()
    tree = TreeHash("a" * 40)
    answer = asyncio.run(stand_ins.execute(effect(RequestReview(tree=tree, cycle=0), 1), now=T0))
    assert isinstance(answer, ReviewProduced)
    assert (answer.effect_id, answer.findings) == (EffectId("run-1:5:0"), ())
    assert set(live.STAND_INS) == {"request_review"}
    with pytest.raises(TypeError, match="no stand-in for finalize"):
        asyncio.run(stand_ins.execute(effect(Finalize(tree=tree)), now=T0))


def test_an_effect_without_an_executor_fails_with_a_reason() -> None:
    with pytest.raises(NotImplementedError, match="await_plan_approval has no executor yet"):
        asyncio.run(live.Unavailable().execute(effect(AwaitPlanApproval(plan=plan_ref())), now=T0))


def test_a_spending_effect_without_a_reservation_is_refused() -> None:
    with pytest.raises(ValueError, match="carries no budget reservation"):
        reservation(effect(RequestReview(tree=TreeHash("a" * 40), cycle=0)))


# ---------- the harness's command lines


def test_verification_commands_leave_the_testers_directories_alone() -> None:
    skip = ("tests/acceptance", "tests/repro")
    assert check_argv("ruff_check", skip) == [
        "ruff", "check", "--no-fix", "--no-cache", "--extend-exclude",
        "tests/acceptance,tests/repro", ".",
    ]  # fmt: skip
    assert check_argv("mypy", skip) == [
        "mypy",
        "--exclude",
        r"^(tests/acceptance|tests/repro)/",
        ".",
    ]
    assert check_argv("pytest", skip)[-2:] == ["--ignore=tests/acceptance", "--ignore=tests/repro"]
    assert check_argv("ruff_format_check", ()) == [
        "ruff", "format", "--check", "--diff", "--no-cache", ".",
    ]  # fmt: skip
    assert outside(["src/a.py", "tests/acceptance/t.py", "tests/acceptances.py"], skip) == [
        "src/a.py",
        "tests/acceptances.py",
    ]


def test_junit_cases_become_node_ids_with_their_first_message_line() -> None:
    xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.acceptance.test_x" name="test_ok" file="tests/acceptance/test_x.py"/>
  <testcase classname="tests.acceptance.test_x.TestThing" name="test_bad[1]"
            file="tests/acceptance/test_x.py">
    <failure message="assert 1 == 2&#10;where ...">trace</failure>
  </testcase>
  <testcase classname="tests.acceptance.test_x" name="test_skip" file="tests/acceptance/test_x.py">
    <skipped message="later"/>
  </testcase>
  <testcase classname="" name="tests.acceptance.test_y"><error message="collection failure"/>
  </testcase>
</testsuite></testsuites>"""
    cases = {c.nodeid: (c.passed, c.message) for c in parse_junit(xml)}
    assert cases == {
        "tests/acceptance/test_x.py::test_ok": (True, ""),
        "tests/acceptance/test_x.py::TestThing::test_bad[1]": (False, "assert 1 == 2"),
        "tests/acceptance/test_x.py::test_skip": (True, ""),
        "tests.acceptance.test_y": (False, "collection failure"),
    }


# ---------- executors on a real worktree


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunContext:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    repo = make_repo("greeter", tmp_path / "repo")
    base = base_config()
    config: MsaConfig = base.model_copy(
        update={"sandbox": base.sandbox.model_copy(update={"launcher": (sys.executable, "-m")})}
    )
    environment = live.LiveEnvironment(repo=str(repo), state_dir=str(tmp_path / "s"), task="t")
    intake = IntakeExecutor(repo, LocalSandbox(Root.at(repo), config.sandbox))
    context = live.context(
        environment,
        config,
        Gateway(fake.ScriptedClient(), config, store=MemoryResponseStore()),
        run_id=RunId("run-1"),
        intake=intake,
    )
    asyncio.run(context.worktree.ensure(TreeHash(git(repo, "rev-parse", "HEAD^{tree}"))))
    return context


def test_verification_refuses_a_tree_the_worktree_does_not_hold(run: RunContext) -> None:
    other = TreeHash("b" * 40)
    with pytest.raises(RuntimeError, match=f"verification was asked for {other}"):
        asyncio.run(VerifyExecutor(run).execute(effect(RunVerification(tree=other)), now=T0))


def test_testing_refuses_a_tree_the_worktree_does_not_hold(run: RunContext) -> None:
    other = TreeHash("b" * 40)
    command = RunTestSuite(tree=other, base_tree=BASE)
    with pytest.raises(RuntimeError, match=f"tests were asked for {other}"):
        asyncio.run(TestSuiteExecutor(run).execute(effect(command, 1_000), now=T0))


def test_a_check_that_cannot_run_fails_the_effect_not_the_code(run: RunContext) -> None:
    run.config = run.config.model_copy(
        update={"sandbox": run.config.sandbox.model_copy(update={"launcher": ("no-such-tool",)})}
    )
    tree = asyncio.run(run.worktree.snapshot())
    with pytest.raises(CommandUnavailable, match="no-such-tool is not installed"):
        asyncio.run(VerifyExecutor(run).execute(effect(RunVerification(tree=tree)), now=T0))


def test_no_acceptance_tests_is_the_testers_problem_and_needs_no_model(run: RunContext) -> None:
    tree = asyncio.run(run.worktree.snapshot())
    command = RunTestSuite(tree=tree, base_tree=tree)
    answer = asyncio.run(TestSuiteExecutor(run).execute(effect(command, 1_000), now=T0))
    assert isinstance(answer, TestsFailed)
    assert (answer.triage.kind, answer.triage.owner) == (FailureKind.TEST_BUG, Owner.TESTER)
    report = run.files.text(answer.report)
    assert "No acceptance tests were written" in report
    assert "tests/acceptance/" in report


def test_revisions_go_to_their_owner_and_review_findings_wait_for_m8(run: RunContext) -> None:
    router = Revisions(CoderExecutor(run), TesterExecutor(run))
    for owner in Owner:
        command = ReviseChanges(owner=owner, tree=BASE, findings=(FindingId("f1"),))
        with pytest.raises(NotImplementedError, match="arrive with the Reviewer"):
            asyncio.run(router.execute(effect(command, 1_000), now=T0))
    tester_revision = ReviseChanges(owner=Owner.TESTER, tree=BASE, failure_report=digest("r"))
    with pytest.raises(TypeError, match="the coder cannot revise what the tester owns"):
        asyncio.run(CoderExecutor(run).execute(effect(tester_revision, 1_000), now=T0))
