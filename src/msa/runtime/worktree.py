"""The run's git worktree: where agents work, and where tree hashes come from.

Evidence is bound to tree hashes (M1), so the harness, not an agent, decides what tree the
work is at. Agents edit files. The harness snapshots the worktree into a tree (``git add
-A`` then ``write-tree``) and moves it to a given tree when a step starts over.

- **Created at INTAKE**, from a commit of the base tree with a fixed identity and date, so
  the same base always gives the same commit and ``git_diff`` shows exactly the run's
  changes. A run resumed after a crash finds it on disk.
- **Outside the repository's working tree**: under the state directory, next to the
  journal (``.msa/worktrees/<run>``). The repository's own checkout is never touched.
- **Generated files are never part of a tree.** Caches that checks write (``.pytest_cache``,
  ``__pycache__``, ...) are excluded through ``core.excludesFile``, on top of the
  repository's ``.gitignore``, so running a check cannot change the tree it checked.
- **Checks see exactly the tree.** Before checks run, ``prune`` removes every file the
  tree leaves out, ignored ones above all. Otherwise a file that is in no tree (a
  gitignored ``local_settings.py``, say) could make the checks pass on code that fails once
  delivered (ADR-013). Only the environment ``uv`` builds and the checks' own caches stay.

Git runs with hooks off and fsmonitor off, without system or global config, and with
fixed arguments only: no agent input reaches a git command line.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from msa.core.ids import TreeHash

GENERATED = (
    ".venv/",  # the environment uv builds; uv ignores it itself, other tools may not
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".hypothesis/",
    ".coverage",
    ".coverage.*",
)
# What ``prune`` leaves in place: the environment uv builds from the lockfile, and caches.
# Sourceless bytecode is not kept: a lone ``module.pyc`` next to the sources is importable.
KEPT = (".venv/", "__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/", ".hypothesis/")
IDENTITY = {
    "GIT_AUTHOR_NAME": "msa",
    "GIT_AUTHOR_EMAIL": "msa@localhost",
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "msa",
    "GIT_COMMITTER_EMAIL": "msa@localhost",
    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00+0000",
}
_INHERITED_GIT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


class GitError(RuntimeError):
    """A harness git command failed. Not the model's doing, so the effect fails."""


async def git(
    cwd: Path,
    *args: str,
    excludes: Path | None = None,
    identity: bool = False,
    env: Mapping[str, str] | None = None,
) -> str:
    """A harness git command. ``identity`` fixes author, committer and dates (reproducible
    commits); ``env`` sets other variables, such as a real author for a release."""
    config = ["-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}"]
    config += ["-c", "core.quotePath=false"]
    if excludes is not None:
        config += ["-c", f"core.excludesFile={excludes}"]
    variables = {k: v for k, v in os.environ.items() if k not in _INHERITED_GIT}
    variables |= {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    if identity:
        variables |= IDENTITY
    if env:
        variables |= env
    process = await asyncio.create_subprocess_exec(
        "git",
        *config,
        *args,
        cwd=cwd,
        env=variables,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        detail = err.decode("utf-8", "replace").strip() or f"exit {process.returncode}"
        raise GitError(f"git {args[0]} failed: {detail}")
    return out.decode("utf-8", "replace")


@dataclass(frozen=True, slots=True)
class RunWorktree:
    repo: Path  # the user's repository
    path: Path  # the worktree
    excludes: Path  # generated-file patterns, for core.excludesFile

    @classmethod
    def for_run(cls, repo: Path, state_dir: Path, run_id: str, name: str = "work") -> RunWorktree:
        """A run's worktrees: ``work`` holds the build (the Coder's, then the head), ``tests``
        the Tester's blind authoring, ``base`` the fail-before checks."""
        home = state_dir / "worktrees" / run_id
        return cls(repo=repo, path=home / name, excludes=home / "exclude")

    async def _git(self, *args: str, identity: bool = False) -> str:
        return await git(self.path, *args, excludes=self.excludes, identity=identity)

    @property
    def exists(self) -> bool:
        return (self.path / ".git").is_file()

    async def ensure(self, base: TreeHash) -> None:
        """Create the worktree at ``base`` unless it exists (idempotent: INTAKE may run
        again after a crash)."""
        if self.exists:
            return
        self.excludes.parent.mkdir(parents=True, exist_ok=True)
        self.excludes.write_text("\n".join(GENERATED) + "\n", encoding="utf-8")
        if self.path.exists():  # a creation interrupted before git registered it
            shutil.rmtree(self.path)
        await git(self.repo, "worktree", "prune")
        commit = (
            await git(self.repo, "commit-tree", base, "-m", "msa: run base", identity=True)
        ).strip()
        await git(self.repo, "worktree", "add", "--detach", str(self.path), commit)

    async def base_tree(self) -> TreeHash:
        return TreeHash((await self._git("rev-parse", "HEAD^{tree}")).strip())

    async def snapshot(self) -> TreeHash:
        """The tree of everything in the worktree now, minus ignored and generated files."""
        await self._git("add", "--all")
        return TreeHash((await self._git("write-tree")).strip())

    async def prune(self) -> list[str]:
        """Remove what the next snapshot would leave out, so the worktree holds exactly its
        tree plus ``KEPT``. The tree itself does not change. Returns the removed paths."""
        await self._git("add", "--all")  # now everything not ignored is in the index
        out = await self._git("clean", "-ffdx", *(f"--exclude={p}" for p in KEPT))
        return sorted(line.removeprefix("Removing ") for line in out.splitlines() if line)

    async def read(self, tree: TreeHash, path: str) -> str | None:
        """The text of ``path`` in ``tree``, or None if the tree has no such file."""
        listed = await self._git("ls-tree", "-z", tree, "--", f":(literal){path}")
        entry = listed.split("\0")[0]
        if not entry:
            return None
        meta, _, _ = entry.partition("\t")
        _, kind, oid = meta.split()
        if kind != "blob":
            return None
        return await self._git("cat-file", "blob", oid)

    async def reset(self, tree: TreeHash) -> None:
        """Make the worktree hold exactly ``tree`` (ignored files are left alone)."""
        await self._git("add", "--all")
        await self._git("read-tree", "--reset", "-u", tree)
        await self._git("clean", "-fdq")

    async def files(self, tree: TreeHash) -> list[str]:
        out = await self._git("ls-tree", "-r", "-z", "--name-only", tree)
        return sorted(p for p in out.split("\0") if p)

    async def changed_files(
        self,
        base: TreeHash,
        head: TreeHash,
        directories: Sequence[str] = (),
        *,
        deleted: bool = False,
    ) -> list[str]:
        """Paths added or modified from ``base`` to ``head``, optionally only those inside
        ``directories`` (a file path names just that file). Deleted paths are left out
        unless ``deleted``."""
        out = await self._git(
            "diff",
            "--no-ext-diff",
            "--name-only",
            "-z",
            "--no-renames",
            *(() if deleted else ("--diff-filter=d",)),
            base,
            head,
            "--",
            *_pathspecs(directories),
        )
        return sorted(p for p in out.split("\0") if p)

    async def overlay(self, tree: TreeHash, base: TreeHash, directories: Sequence[str]) -> None:
        """Bring the changes ``tree`` made to ``directories`` since ``base`` into this
        worktree. Path ownership guarantees nobody else changed them, so this never
        conflicts: it is how the Tester's tests join the Coder's tree."""
        if not directories:
            return
        out = await self._git(
            "diff",
            "--no-ext-diff",
            "--name-status",
            "-z",
            "--no-renames",
            base,
            tree,
            "--",
            *_pathspecs(directories),
        )
        fields = [f for f in out.split("\0") if f]
        changes = list(zip(fields[::2], fields[1::2], strict=True))
        if kept := [path for status, path in changes if status != "D"]:
            await self._git("checkout", tree, "--", *kept)
        for status, path in changes:
            if status == "D":
                (self.path / path).unlink(missing_ok=True)

    async def changes(self, base: TreeHash, head: TreeHash) -> str:
        """A zero-context patch from ``base`` to ``head``: which lines changed, for the
        review's scope rule (``msa.runtime.negotiation.changed_lines``)."""
        return await self._git(
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--no-textconv",
            "--no-renames",
            "-U0",
            base,
            head,
        )

    async def diff(self, base: TreeHash, head: TreeHash) -> str:
        """A patch from ``base`` to ``head`` that ``git apply`` accepts."""
        return await self._git(
            "diff",
            "--no-ext-diff",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-textconv",
            base,
            head,
        )


def _pathspecs(directories: Sequence[str]) -> list[str]:
    """Literal directory pathspecs: a directory name is never read as a glob."""
    return [f":(literal){d}" for d in directories] if directories else []
