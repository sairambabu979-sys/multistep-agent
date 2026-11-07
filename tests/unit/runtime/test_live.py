"""The live executors' edges: stand-ins, effects with no executor, and refusals to guess."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from msa.config import MsaConfig
from msa.core.effects import (
    AuthorAcceptanceTests,
    AwaitPlanApproval,
    Effect,
    Finalize,
    RequestReview,
    ReviseChanges,
    RunTestSuite,
    RunVerification,
)
from msa.core.events import AcceptanceTestsAuthored, Completion, ReviewProduced, TestsPassed
from msa.core.ids import EffectId, FindingId, RunId, TreeHash
from msa.core.values import Owner
from msa.llm import fake
from msa.llm.gateway import Gateway
from msa.llm.store import MemoryResponseStore
from msa.runtime import live
from msa.runtime.context import RunContext, reservation
from msa.runtime.intake import IntakeExecutor
from msa.runtime.roles import CoderExecutor
from msa.runtime.verify import VerifyExecutor
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


def test_stand_ins_pass_through_and_name_themselves() -> None:
    stand_ins = live.StandIns()
    tree = TreeHash("a" * 40)
    answers = [
        asyncio.run(stand_ins.execute(effect(command, 1), now=T0))
        for command in (
            AuthorAcceptanceTests(plan=plan_ref(), base_tree=BASE),
            RunTestSuite(tree=tree, base_tree=BASE),
            RequestReview(tree=tree, cycle=0),
        )
    ]
    assert [type(a) for a in answers] == [AcceptanceTestsAuthored, TestsPassed, ReviewProduced]
    assert all(isinstance(a, Completion) and a.effect_id == EffectId("run-1:5:0") for a in answers)
    assert isinstance(answers[2], ReviewProduced)
    assert answers[2].findings == ()
    assert set(live.STAND_INS) == {"author_acceptance_tests", "run_test_suite", "request_review"}
    with pytest.raises(TypeError, match="no stand-in for finalize"):
        asyncio.run(stand_ins.execute(effect(Finalize(tree=tree)), now=T0))


def test_an_effect_without_an_executor_fails_with_a_reason() -> None:
    with pytest.raises(NotImplementedError, match="await_plan_approval has no executor yet"):
        asyncio.run(live.Unavailable().execute(effect(AwaitPlanApproval(plan=plan_ref())), now=T0))


def test_a_spending_effect_without_a_reservation_is_refused() -> None:
    with pytest.raises(ValueError, match="carries no budget reservation"):
        reservation(effect(RequestReview(tree=TreeHash("a" * 40), cycle=0)))


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


def test_a_check_that_cannot_run_fails_the_effect_not_the_code(run: RunContext) -> None:
    run.config = run.config.model_copy(
        update={"sandbox": run.config.sandbox.model_copy(update={"launcher": ("no-such-tool",)})}
    )
    tree = asyncio.run(run.worktree.snapshot())
    with pytest.raises(CommandUnavailable, match="no-such-tool is not installed"):
        asyncio.run(VerifyExecutor(run).execute(effect(RunVerification(tree=tree)), now=T0))


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (
            ReviseChanges(owner=Owner.TESTER, tree=BASE, failure_report=digest("r")),
            "arrive with the Tester",
        ),
        (
            ReviseChanges(owner=Owner.CODER, tree=BASE, findings=(FindingId("f1"),)),
            "arrive with the Reviewer",
        ),
    ],
)
def test_revisions_m6_cannot_do_are_refused_not_guessed(
    run: RunContext, command: ReviseChanges, message: str
) -> None:
    with pytest.raises(NotImplementedError, match=message):
        asyncio.run(CoderExecutor(run).execute(effect(command, 1_000), now=T0))
