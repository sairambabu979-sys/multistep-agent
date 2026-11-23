"""CREATED, VALIDATING and CONTEXT (ADR-015): everything decided before a model is paid.

- **Admission** (``admit_run``): the run's limits must be within ``[admission]``, and the
  repository may not already have ``max_live_runs`` live runs (parked ones count: each
  holds worktrees and may be waiting on a person). Denied runs fail with the reason.
- **Validation** (``validate_request``): the task holds no secret (it is journaled and
  sent to the model); the preferences resolve, with no two hard preferences in conflict
  (I-16); the root is a git repository with a commit to start from. A rejection names
  what to fix.
- **Context** (``load_context``): the base commit and tree, the run's worktrees at that
  tree, and the repository map, stored as ``context.json``. Evidence is bound to tree
  hashes, so a run starts from a committed state: ``HEAD^{tree}``.

Each is idempotent: run again after a crash, it decides the same way from the same inputs,
except admission, which counts the runs live *now*. That is the point of admission.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from msa.config import AdmissionConfig
from msa.core.effects import AdmitRun, Effect, LoadContext, ValidateRequest
from msa.core.events import (
    AnyEvent,
    ContextLoaded,
    RequestRejected,
    RequestValidated,
    RunAdmitted,
    RunDenied,
)
from msa.core.ids import TREE_HASH_PATTERN, RunId, TreeHash
from msa.core.values import Limits
from msa.runtime.artifacts import RunFiles
from msa.runtime.worktree import RunWorktree
from msa.skills.preferences import PreferenceError, Resolved, store
from msa.tools.sandbox import LocalSandbox
from msa.tools.secrets import scan

_TREE = re.compile(TREE_HASH_PATTERN)
_GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
CONTEXT_FILE = "context.json"


def secrets_in_task(task: str) -> str | None:
    """Why ``task`` may not be used, or None. Shared with ``msa run``, which refuses such a
    task before anything is journaled."""
    found = scan(task)
    if not found:
        return None
    listed = "; ".join(m.render() for m in found)
    return (
        f"the task contains what looks like a secret ({listed}). Tasks are journaled and "
        "sent to the model: refer to credentials by name, never by value."
    )


# ---------- CREATED


def admission_problems(limits: Limits, policy: AdmissionConfig, live_runs: int) -> list[str]:
    problems = []
    ceiling = round(policy.max_budget_usd * 1_000_000)
    if limits.budget_usd_micros > ceiling:
        problems.append(
            f"budget {limits.budget_usd_micros} micro-USD is over the admission limit "
            f"{ceiling} ([admission].max_budget_usd)"
        )
    longest = timedelta(minutes=policy.max_wall_clock_minutes)
    if limits.wall_clock > longest:
        problems.append(
            f"wall clock {limits.wall_clock} is over the admission limit {longest} "
            "([admission].max_wall_clock_minutes)"
        )
    if live_runs >= policy.max_live_runs:
        problems.append(
            f"{live_runs} other run(s) of this repository are live, the most allowed "
            f"([admission].max_live_runs = {policy.max_live_runs}): finish, answer or cancel "
            "one first (`msa runs`)"
        )
    return problems


class AdmissionExecutor:
    def __init__(
        self, policy: AdmissionConfig, limits: Callable[[], Limits], live_runs: Callable[[], int]
    ) -> None:
        self.policy = policy
        self._limits = limits
        self._live_runs = live_runs

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        if not isinstance(effect.command, AdmitRun):
            raise TypeError(f"admission cannot execute {effect.command.kind}")
        problems = admission_problems(self._limits(), self.policy, self._live_runs())
        if problems:
            return RunDenied(at=now, effect_id=effect.id, reason="; ".join(problems))
        return RunAdmitted(at=now, effect_id=effect.id)


# ---------- VALIDATING


class ValidationExecutor:
    def __init__(
        self,
        root: Path,
        sandbox: LocalSandbox,
        *,
        task: str,
        overrides: Sequence[str] = (),
        load: Callable[[Path, Sequence[str]], Resolved] | None = None,
    ) -> None:
        self.root = root
        self.sandbox = sandbox
        self.task = task
        self.overrides = tuple(overrides)
        self._load = load or store.load
        self.resolved: Resolved | None = None  # the run's preferences, once validated

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        if not isinstance(effect.command, ValidateRequest):
            raise TypeError(f"validation cannot execute {effect.command.kind}")
        if not self.task.strip():
            return RequestRejected(at=now, effect_id=effect.id, reason="the task is empty")
        if problem := secrets_in_task(self.task):
            return RequestRejected(at=now, effect_id=effect.id, reason=problem)
        try:
            resolved = self._load(self.root, self.overrides)
        except PreferenceError as exc:
            return RequestRejected(at=now, effect_id=effect.id, reason=f"preferences: {exc}")
        if await base_tree(self.sandbox) is None:
            reason = "the root is not a git repository with a commit to start from"
            return RequestRejected(at=now, effect_id=effect.id, reason=reason)
        self.resolved = resolved
        return RequestValidated(at=now, effect_id=effect.id)


async def _git(sandbox: LocalSandbox, *args: str) -> str | None:
    done = await sandbox.execute([*_GIT, *args], shown="git", merge=False)
    return done.stdout.strip() if done.exit_code == 0 else None


async def base_tree(sandbox: LocalSandbox) -> TreeHash | None:
    tree = await _git(sandbox, "rev-parse", "--verify", "--quiet", "HEAD^{tree}")
    return TreeHash(tree) if tree and _TREE.match(tree) else None


# ---------- CONTEXT


class ContextExecutor:
    """The base tree and commit, the worktrees at it, and the repository map. The worktrees
    are created before the event is journaled: once the context is loaded, every later
    executor (also after a resume) finds them, and the two IMPLEMENTING regions never race
    to create theirs."""

    def __init__(
        self,
        sandbox: LocalSandbox,
        files: RunFiles,
        run_id: RunId,
        worktrees: Sequence[RunWorktree],
    ) -> None:
        self.sandbox = sandbox
        self.files = files
        self.run_id = run_id
        self.worktrees = tuple(worktrees)

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        if not isinstance(effect.command, LoadContext):
            raise TypeError(f"context loading cannot execute {effect.command.kind}")
        tree = await base_tree(self.sandbox)
        commit = await _git(self.sandbox, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        if tree is None or commit is None:
            raise RuntimeError("the repository has no commit to start from any more")
        for worktree in self.worktrees:
            await worktree.ensure(tree)
        paths = await self.worktrees[0].files(tree) if self.worktrees else []
        context = {
            "run": self.run_id,
            "base_commit": commit,
            "base_tree": tree,
            "files": paths,
        }
        text = json.dumps(context, indent=2, sort_keys=True)
        self.files.write(CONTEXT_FILE, text)
        digest = self.files.put(text)
        return ContextLoaded(at=now, effect_id=effect.id, base_tree=tree, context=digest)


def base_commit(files: RunFiles) -> str:
    """The commit the run started from, as CONTEXT recorded it."""
    stored = files.read(CONTEXT_FILE)
    if stored is None:
        raise FileNotFoundError(f"{CONTEXT_FILE} is missing: the context was never loaded")
    commit = json.loads(stored)["base_commit"]
    if not isinstance(commit, str):
        raise TypeError(f"{CONTEXT_FILE} names no base commit")
    return commit
