"""The task corpus (PLAN §11): hand-built tasks, each with hidden tests the agents never see.

Layout, under one directory (``evals`` in this repository)::

    repos/<repo>/                 a base repository: src/, tests/, pyproject.toml
    tasks/<id>/task.toml          kind, repo, expect, and the task text
    tasks/<id>/hidden/*.py        hidden tests, copied into the delivered tree only to score it
    tasks/<id>/reference/...      files that resolve the task, laid over the base repository

A task the system should resolve (``expect = "resolve"``) needs hidden tests and a
reference. A trap (``expect = "flag"``: an ambiguous or impossible task) needs neither: the
right answer is to name the problem, which a person judges (``msa.evals.report``).
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Kind = Literal["bugfix", "feature", "refactor", "trap"]
Expect = Literal["resolve", "flag"]
KINDS: tuple[Kind, ...] = ("bugfix", "feature", "refactor", "trap")
HIDDEN_DIR = "tests/hidden"  # where hidden tests land in a tree being scored


class CorpusError(ValueError):
    """The corpus on disk is not well formed."""


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    kind: Kind
    repo: str
    expect: Expect
    text: str
    home: Path  # tasks/<id>
    repo_home: Path  # repos/<repo>

    @property
    def hidden(self) -> list[Path]:
        folder = self.home / "hidden"
        return sorted(folder.glob("test_*.py")) if folder.is_dir() else []

    @property
    def reference(self) -> list[Path]:
        folder = self.home / "reference"
        return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []


def load(root: Path, only: frozenset[str] = frozenset()) -> list[Task]:
    """Every task under ``root/tasks``, sorted by id, or just those named in ``only``."""
    tasks_dir, repos_dir = root / "tasks", root / "repos"
    if not tasks_dir.is_dir():
        raise CorpusError(f"no tasks under {tasks_dir}")
    tasks = [
        _task(home, repos_dir)
        for home in sorted(tasks_dir.iterdir())
        if home.is_dir() and (not only or home.name in only)
    ]
    if missing := only - {t.id for t in tasks}:
        raise CorpusError(f"no such task: {', '.join(sorted(missing))}")
    return tasks


def _task(home: Path, repos_dir: Path) -> Task:
    spec = home / "task.toml"
    try:
        raw = tomllib.loads(spec.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CorpusError(f"{spec}: {exc}") from exc
    kind, repo, expect, text = raw.get("kind"), raw.get("repo"), raw.get("expect"), raw.get("task")
    if kind not in KINDS:
        raise CorpusError(f"{spec}: kind must be one of {', '.join(KINDS)}, not {kind!r}")
    if expect != ("flag" if kind == "trap" else "resolve"):
        raise CorpusError(
            f"{spec}: a {kind} task expects {'flag' if kind == 'trap' else 'resolve'}"
        )
    if not isinstance(text, str) or not text.strip():
        raise CorpusError(f"{spec}: the task text is empty")
    if not isinstance(repo, str) or not (repos_dir / repo / "pyproject.toml").is_file():
        raise CorpusError(f"{spec}: no base repository {repo!r} under {repos_dir}")
    if unknown := set(raw) - {"kind", "repo", "expect", "task"}:
        raise CorpusError(f"{spec}: unknown keys {sorted(unknown)}")
    task = Task(home.name, kind, repo, expect, text.strip(), home, repos_dir / repo)
    if expect == "resolve" and not (task.hidden and task.reference):
        raise CorpusError(f"{home}: a task to resolve needs hidden tests and a reference")
    return task


def materialize(task: Task, destination: Path, *, reference: bool = False) -> Path:
    """The task's base repository at ``destination`` as a git repository with one commit,
    with the reference solution laid over it if asked (then committed too)."""
    if destination.exists():
        raise FileExistsError(destination)
    shutil.copytree(task.repo_home, destination, ignore=shutil.ignore_patterns("__pycache__"))
    _git(destination, "init", "-q", "-b", "main")
    _commit(destination, "base")
    if reference:
        for path in task.reference:
            target = destination / path.relative_to(task.home / "reference")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        _commit(destination, f"reference for {task.id}")
    return destination


def add_hidden(task: Task, tree: Path) -> list[str]:
    """Copy the hidden tests into ``tree``; returns their paths relative to it."""
    folder = tree / HIDDEN_DIR
    folder.mkdir(parents=True, exist_ok=True)
    placed = []
    for test in task.hidden:
        shutil.copyfile(test, folder / test.name)
        placed.append(f"{HIDDEN_DIR}/{test.name}")
    return placed


def export_tree(repo: Path, tree: str, destination: Path) -> Path:
    """The files of git tree ``tree`` (in ``repo``'s object store) written out to
    ``destination``: what a run delivered, outside every worktree."""
    destination.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", tree],
        check=True,
        capture_output=True,
    )
    subprocess.run(["tar", "-x", "-C", str(destination)], input=archive.stdout, check=True)
    return destination


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "--all")
    _git(
        repo,
        "-c",
        "user.name=msa-eval",
        "-c",
        "user.email=msa-eval@localhost",
        "commit",
        "--allow-empty",  # a reference that changes nothing is a finding, not a crash
        "-qm",
        message,
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
