"""The eval corpus (PLAN §11) and the check that every task is fit to score.

A resolve rate means something only if every task was a real task: hidden tests that fail
on the base repository, and a solution the system's own gates accept that passes them.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from msa.config import SandboxConfig
from msa.evals import corpus, validity
from tests.support.scripts import ROOT

pytestmark = pytest.mark.slow

EVALS = ROOT / "evals"
SANDBOX = SandboxConfig(launcher=(sys.executable, "-m"))


def test_the_corpus_has_the_shape_the_plan_registered() -> None:
    tasks = corpus.load(EVALS)
    assert len(tasks) == 20
    assert Counter(t.kind for t in tasks) == {"bugfix": 8, "feature": 6, "refactor": 4, "trap": 2}
    for task in tasks:
        if task.kind == "trap":
            assert task.expect == "flag"
            assert not task.reference
        else:
            assert task.expect == "resolve"
            assert task.hidden
            assert task.reference
    assert {t.repo for t in tasks} == {"configlib", "ledger", "schedule", "textkit"}


def test_every_task_is_fit_to_score(tmp_path: Path) -> None:
    tasks = corpus.load(EVALS)
    found = asyncio.run(validity.check_all(tasks, tmp_path, SANDBOX, parallel=os.cpu_count() or 2))
    assert {task: problems for task, problems in found.items() if problems} == {}


def _copy_task(tmp_path: Path, task_id: str) -> tuple[Path, corpus.Task]:
    root = tmp_path / "evals"
    (root / "tasks").mkdir(parents=True)
    shutil.copytree(EVALS / "tasks" / task_id, root / "tasks" / task_id)
    shutil.copytree(EVALS / "repos", root / "repos")
    (task,) = corpus.load(root)
    return root, task


def test_a_task_already_done_on_its_base_is_unfit(tmp_path: Path) -> None:
    root, task = _copy_task(tmp_path, "textkit-wrap-last-line")
    for path in task.reference:  # the fix is already in the base repository
        shutil.copyfile(
            path, root / "repos" / task.repo / path.relative_to(task.home / "reference")
        )
    found = asyncio.run(validity.problems(task, tmp_path / "work", SANDBOX))
    assert found == ["base: every hidden test already passes: there is nothing to do"]


def test_a_reference_that_resolves_nothing_is_unfit(tmp_path: Path) -> None:
    root, task = _copy_task(tmp_path, "textkit-wrap-last-line")
    for path in task.reference:  # the reference is the unchanged base file
        shutil.copyfile(
            root / "repos" / task.repo / path.relative_to(task.home / "reference"), path
        )
    found = asyncio.run(validity.problems(task, tmp_path / "work", SANDBOX))
    assert len(found) == 1
    assert found[0].startswith("reference: hidden tests fail: tests/hidden/")


def test_a_reference_the_gate_rejects_is_unfit(tmp_path: Path) -> None:
    _, task = _copy_task(tmp_path, "textkit-wrap-last-line")
    (reference,) = task.reference
    planted = '\n\nx: int = "not an int"\n'  # format-clean, so only mypy objects
    reference.write_text(reference.read_text(encoding="utf-8") + planted, encoding="utf-8")
    found = asyncio.run(validity.problems(task, tmp_path / "work", SANDBOX))
    assert found == ["reference: mypy fails"]


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ('kind = "chore"\nrepo = "ledger"\nexpect = "resolve"\ntask = "x"', "kind must be"),
        ('kind = "trap"\nrepo = "ledger"\nexpect = "resolve"\ntask = "x"', "expects flag"),
        ('kind = "bugfix"\nrepo = "nowhere"\nexpect = "resolve"\ntask = "x"', "no base repository"),
        ('kind = "bugfix"\nrepo = "ledger"\nexpect = "resolve"\ntask = " "', "task text is empty"),
        ('kind = "bugfix"\nrepo = "ledger"\nexpect = "resolve"\ntask = "x"', "needs hidden tests"),
        ('kind = "trap"\nrepo = "ledger"\nexpect = "flag"\ntask = "x"\nseed = 1', "unknown keys"),
    ],
)
def test_a_malformed_task_is_refused(tmp_path: Path, spec: str, message: str) -> None:
    root = tmp_path / "evals"
    (root / "tasks" / "broken").mkdir(parents=True)
    (root / "tasks" / "broken" / "task.toml").write_text(spec, encoding="utf-8")
    shutil.copytree(EVALS / "repos", root / "repos")
    with pytest.raises(corpus.CorpusError, match=message):
        corpus.load(root)


def test_only_the_named_tasks_load_and_an_unknown_name_is_refused() -> None:
    assert [t.id for t in corpus.load(EVALS, frozenset({"ledger-statement"}))] == [
        "ledger-statement"
    ]
    with pytest.raises(corpus.CorpusError, match="no such task: nope"):
        corpus.load(EVALS, frozenset({"nope"}))


def test_a_delivered_tree_is_exported_whole(tmp_path: Path) -> None:
    (task,) = corpus.load(EVALS, frozenset({"ledger-statement"}))
    repo = corpus.materialize(task, tmp_path / "repo", reference=True)
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    out = corpus.export_tree(repo, tree, tmp_path / "out")
    assert (out / "src" / "ledger" / "accounts.py").read_text(encoding="utf-8") == (
        task.home / "reference" / "src" / "ledger" / "accounts.py"
    ).read_text(encoding="utf-8")
    assert not (out / ".git").exists()
