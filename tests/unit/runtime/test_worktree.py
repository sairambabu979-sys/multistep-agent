"""The run's worktree: created once at the base tree, snapshotted into trees, reset, diffed."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from msa.core.ids import TreeHash
from msa.runtime.worktree import GitError, RunWorktree, git
from tests.support.repos import git as sync_git
from tests.support.repos import make_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo("greeter", tmp_path / "repo")


def base_of(repo: Path) -> TreeHash:
    return TreeHash(sync_git(repo, "rev-parse", "HEAD^{tree}"))


def worktree(repo: Path, tmp_path: Path) -> RunWorktree:
    made = RunWorktree.for_run(repo, tmp_path / "state", "run-1")
    asyncio.run(made.ensure(base_of(repo)))
    return made


def test_it_starts_at_the_base_tree_outside_the_checkout(repo: Path, tmp_path: Path) -> None:
    made = worktree(repo, tmp_path)
    assert made.path == tmp_path / "state" / "worktrees" / "run-1" / "work"
    assert asyncio.run(made.base_tree()) == base_of(repo)
    assert asyncio.run(made.snapshot()) == base_of(repo)
    assert (made.path / "src" / "greeter.py").read_text() == (
        repo / "src" / "greeter.py"
    ).read_text()
    assert sync_git(repo, "status", "--porcelain") == ""  # the checkout is untouched


def test_ensure_is_idempotent_and_the_base_commit_deterministic(repo: Path, tmp_path: Path) -> None:
    made = worktree(repo, tmp_path)
    (made.path / "new.txt").write_text("work in progress\n")
    asyncio.run(made.ensure(base_of(repo)))  # a re-run INTAKE keeps the work
    assert (made.path / "new.txt").exists()
    other = RunWorktree.for_run(repo, tmp_path / "state", "run-2")
    asyncio.run(other.ensure(base_of(repo)))
    head = asyncio.run(git(made.path, "rev-parse", "HEAD")).strip()
    assert asyncio.run(git(other.path, "rev-parse", "HEAD")).strip() == head


def test_snapshot_includes_new_files_but_never_generated_ones(repo: Path, tmp_path: Path) -> None:
    made = worktree(repo, tmp_path)
    (made.path / "src" / "extra.py").write_text("x = 1\n")
    for generated in (".pytest_cache/v/x", "src/__pycache__/m.cpython-313.pyc", ".ruff_cache/a"):
        (made.path / generated).parent.mkdir(parents=True, exist_ok=True)
        (made.path / generated).write_text("cache")
    tree = asyncio.run(made.snapshot())
    assert asyncio.run(made.files(tree)) == [
        "pyproject.toml",
        "src/extra.py",
        "src/greeter.py",
        "tests/test_greeter.py",
    ]
    assert asyncio.run(made.changed_files(base_of(repo), tree)) == ["src/extra.py"]


def test_reset_restores_a_tree_exactly(repo: Path, tmp_path: Path) -> None:
    made = worktree(repo, tmp_path)
    base = base_of(repo)
    (made.path / "src" / "greeter.py").write_text("changed\n")
    (made.path / "src" / "extra.py").write_text("x = 1\n")
    changed = asyncio.run(made.snapshot())
    (made.path / "untracked.py").write_text("left over\n")
    (made.path / "tests" / "test_greeter.py").unlink()
    asyncio.run(made.reset(base))
    assert asyncio.run(made.snapshot()) == base
    assert not (made.path / "untracked.py").exists()
    assert not (made.path / "src" / "extra.py").exists()
    asyncio.run(made.reset(changed))
    assert (made.path / "src" / "greeter.py").read_text() == "changed\n"


def test_diff_is_a_patch_the_repository_accepts(repo: Path, tmp_path: Path) -> None:
    made = worktree(repo, tmp_path)
    (made.path / "src" / "extra.py").write_text("x = 1\n")
    (made.path / "tests" / "test_greeter.py").unlink()
    head = asyncio.run(made.snapshot())
    patch = asyncio.run(made.diff(base_of(repo), head))
    assert "+x = 1" in patch
    assert asyncio.run(made.changed_files(base_of(repo), head)) == ["src/extra.py"]
    (tmp_path / "patch.diff").write_bytes(patch.encode())
    sync_git(repo, "apply", "--check", str(tmp_path / "patch.diff"))


def test_a_failing_git_command_raises_git_error(repo: Path, tmp_path: Path) -> None:
    made = worktree(repo, tmp_path)
    with pytest.raises(GitError, match="git read-tree failed"):
        asyncio.run(made.reset(TreeHash("0" * 40)))


def test_prune_leaves_exactly_the_tree_plus_the_environment_and_caches(
    repo: Path, tmp_path: Path
) -> None:
    """A file the tree leaves out must not take part in checks of the tree (ADR-013)."""
    made = worktree(repo, tmp_path)
    (made.path / ".gitignore").write_text("local_settings.py\n.env\n")
    (made.path / "src" / "local_settings.py").write_text('FAREWELL = "goodbye"\n')
    (made.path / ".env").write_text("SECRET=1\n")
    (made.path / "src" / "greeter.pyc").write_text("sourceless bytecode imports")
    (made.path / "src" / "new.py").write_text("x = 1\n")  # untracked, not ignored: kept
    kept = [".venv/lib/site.py", "src/__pycache__/m.cpython-313.pyc", ".mypy_cache/a/b.json"]
    for path in kept:
        (made.path / path).parent.mkdir(parents=True, exist_ok=True)
        (made.path / path).write_text("environment or cache")
    before = asyncio.run(made.snapshot())

    removed = asyncio.run(made.prune())

    assert removed == [".env", "src/greeter.pyc", "src/local_settings.py"]
    assert asyncio.run(made.snapshot()) == before  # the tree did not change
    on_disk = sorted(
        str(p.relative_to(made.path))
        for p in made.path.rglob("*")
        if p.is_file() and p.name != ".git"
    )
    assert on_disk == sorted([*asyncio.run(made.files(before)), *kept])
    assert asyncio.run(made.prune()) == []


def test_read_gives_a_file_as_of_a_tree(repo: Path, tmp_path: Path) -> None:
    made = worktree(repo, tmp_path)
    base = base_of(repo)
    (made.path / "src" / "greeter.py").write_text("changed\n")
    head = asyncio.run(made.snapshot())
    assert asyncio.run(made.read(head, "src/greeter.py")) == "changed\n"
    assert (
        asyncio.run(made.read(base, "src/greeter.py")) == (repo / "src" / "greeter.py").read_text()
    )
    assert asyncio.run(made.read(base, "src/missing.py")) is None
    assert asyncio.run(made.read(base, "src")) is None  # a directory is not a file
    assert asyncio.run(made.read(base, "*.toml")) is None  # paths are literal, never globs


def test_changed_files_can_include_deletions(repo: Path, tmp_path: Path) -> None:
    made = worktree(repo, tmp_path)
    base = base_of(repo)
    (made.path / "pyproject.toml").unlink()
    (made.path / "conftest.py").write_text("")
    head = asyncio.run(made.snapshot())
    controls = ["conftest.py", "pyproject.toml", "pytest.ini"]
    assert asyncio.run(made.changed_files(base, head, controls)) == ["conftest.py"]
    assert asyncio.run(made.changed_files(base, head, controls, deleted=True)) == [
        "conftest.py",
        "pyproject.toml",
    ]
