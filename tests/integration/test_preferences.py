"""I-16 (PLAN §6.2): conflicting hard preferences fail the run in INTAKE, precisely.

A whole journaled run: the real intake executor resolves the real preference layers of a
real git repository, and the simulator answers every effect after it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from msa.core import RunState
from msa.core.effects import Effect, RequestPlan
from msa.core.events import AnyEvent
from msa.core.ids import EffectId, RunId
from msa.core.values import OutcomeReason, Phase
from msa.runtime.clock import LogicalClock
from msa.runtime.executors import Router
from msa.runtime.intake import IntakeExecutor
from msa.runtime.interpreter import Interpreter
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import Profile, SimulatedEnvironment, SimulationConfig, digest
from msa.skills.preferences import store
from msa.tools.paths import Root
from msa.tools.sandbox import LocalSandbox
from tests.support.factory import limits
from tests.support.tools import sandbox_config

HAPPY = SimulationConfig(seed=0, profile=Profile.HAPPY)

USER = """\
version: 1
preferences:
  - id: style.quotes.single
    scope: { language: python }
    rule: "Use single quotes for strings."
    enforcement: hard
    conflicts_with: [style.quotes.double]
"""
REPO = """\
version: 1
preferences:
  - id: style.quotes.double
    scope: { language: python, paths: ["src/**"] }
    rule: "Use double quotes for strings."
    enforcement: hard
"""


def git(repo: Path, *args: str) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "user.yaml"))
    return root


def write_layers(repo: Path, *, user: str = USER, repo_layer: str = REPO) -> None:
    (Path(os.environ["MSA_USER_PREFERENCES"])).write_text(user, encoding="utf-8")
    (repo / ".msa").mkdir(exist_ok=True)
    (repo / store.REPO_FILE).write_text(repo_layer, encoding="utf-8")


class Recording:
    def __init__(self) -> None:
        self.kinds: list[str] = []
        self.happy = SimulatedEnvironment(HAPPY)

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        self.kinds.append(effect.command.kind)
        return await self.happy.execute(effect, now=now)


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[SqliteJournal]:
    with SqliteJournal(tmp_path / "runs.db") as opened:
        yield opened


def run(
    journal: SqliteJournal, repo: Path, *, overrides: tuple[str, ...] = ()
) -> tuple[RunState, IntakeExecutor, Recording]:
    sandbox = LocalSandbox(Root.at(repo), sandbox_config())
    intake = IntakeExecutor(repo, sandbox, overrides=overrides)
    rest = Recording()
    interpreter = Interpreter(
        journal, Router({"prepare_workspace": intake}, rest), clock=LogicalClock()
    )
    final = asyncio.run(
        interpreter.start(
            run_id=RunId("run-1"),
            task_digest=digest("t"),
            limits=limits(),
            environment=HAPPY.model_dump(mode="json"),
        )
    )
    return final, intake, rest


def test_i16_conflicting_hard_preferences_fail_the_run_in_intake(
    journal: SqliteJournal, repo: Path
) -> None:
    write_layers(repo)
    final, intake, rest = run(journal, repo)
    assert final.phase is Phase.FAILED
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.INTAKE_REJECTED
    user_file = os.environ["MSA_USER_PREFERENCES"]
    assert final.outcome.detail == (
        "preferences: conflicting hard preferences: style.quotes.double "
        f"(from {repo / store.REPO_FILE}) conflicts with style.quotes.single (from {user_file}): "
        "both are hard and both apply to python, src/** / python, all paths. make one soft, "
        "narrow a scope, or switch one off with --pref <id>=off"
    )
    assert rest.kinds == []  # nothing after intake ran: no planner was paid for
    assert intake.resolved is None
    assert journal.load(RunId("run-1")) == final


def test_a_run_override_lets_the_same_repo_through(journal: SqliteJournal, repo: Path) -> None:
    write_layers(repo)
    final, intake, rest = run(journal, repo, overrides=("style.quotes.single=off",))
    assert final.phase is Phase.DONE
    assert rest.kinds[0] == "request_plan"
    assert intake.resolved is not None
    assert {p.id for p in intake.resolved.select(["src/app.py"])} >= {"style.quotes.double"}


def test_intake_snapshots_the_committed_base_tree(journal: SqliteJournal, repo: Path) -> None:
    final, _, _ = run(journal, repo)
    assert final.base_tree == git(repo, "rev-parse", "HEAD^{tree}")


@pytest.mark.parametrize(
    "broken", ["version: 1\nversion: 2\n", "version: 1\npreferences:\n  - id: x.y\n"]
)
def test_broken_preference_files_fail_intake_with_the_file_named(
    journal: SqliteJournal, repo: Path, broken: str
) -> None:
    write_layers(repo, repo_layer=broken)
    final, _, _ = run(journal, repo)
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.INTAKE_REJECTED
    assert final.outcome.detail is not None
    assert final.outcome.detail.startswith("preferences: ")
    assert (
        "preferences.yaml" in final.outcome.detail or "x.y: no layer sets" in final.outcome.detail
    )


def test_a_root_without_a_commit_fails_intake(journal: SqliteJournal, tmp_path: Path) -> None:
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    final, _, _ = run(journal, bare)
    assert final.outcome is not None
    assert final.outcome.detail == "the root is not a git repository with a commit to start from"


def test_intake_executes_only_its_own_effect(repo: Path) -> None:
    intake = IntakeExecutor(repo, LocalSandbox(Root.at(repo), sandbox_config()))
    effect = Effect(id=EffectId("run-1:1:0"), budget_usd_micros=1, command=RequestPlan(attempt=1))
    with pytest.raises(TypeError, match="intake cannot execute request_plan"):
        asyncio.run(intake.execute(effect, now=datetime.now().astimezone()))
