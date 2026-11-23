"""STATIC_ANALYSIS (ADR-015): deterministic checks on the head tree. No model is involved.

``run_static_analysis(tree)`` first removes every file the tree leaves out (gitignored ones,
chiefly: they would not be delivered, so they must not make a check pass), then proves the
worktree holds ``tree`` (evidence is bound to the tree it was produced for). Then it runs,
in order:

1. each command in ``[verify].commands``, over the whole worktree, in the sandbox;
2. the machine checks of the preferences that apply to the files the run changed
   (``msa.runtime.checks``). Hard findings fail verification; soft and info findings are
   reported but do not;
3. the pytest-configuration guard (``msa.runtime.guard.reconfigured``): the change may not
   alter the conftest.py files or pytest configuration above the Tester's directories,
   which decide whether and how the Tester's tests run (ADR-013).

Both leave out the Tester's directories (``tests/acceptance``, ``tests/repro``). A failure
here is the Coder's to fix, and the Coder may not edit those paths: UNIT_TESTING checks
them instead, and sends their failures to the Tester. No test runs here: UNIT_TESTING and
INTEGRATION_TESTING run them, where fail-before/pass-after and flakes are judged.

Every check runs even after one fails, so a single revision can fix them all. The report
is stored as an artifact either way: ``StaticAnalysisFailed`` names it, and the Coder's
revision reads it.

A check that cannot run at all (the program is missing, a checker crashes) is not the
code's fault and no revision can fix it. It raises, and the interpreter turns that into
``EffectFailed``: the run stops and says why.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from msa.config import Role, VerifyCommand
from msa.core.effects import Effect, RunStaticAnalysis
from msa.core.events import AnyEvent, StaticAnalysisFailed, StaticAnalysisPassed
from msa.core.ids import TreeHash
from msa.runtime.checks import CheckFinding, run_checks
from msa.runtime.context import RunContext
from msa.runtime.guard import pytest_controls, reconfigured
from msa.runtime.worktree import RunWorktree
from msa.tools.sandbox import Completed

PYTEST_NO_TESTS = 5  # pytest's exit code when it collected nothing: not a failure


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    output: str

    def render(self) -> str:
        return f"## {self.name}: {'passed' if self.passed else 'FAILED'}\n\n{self.output.strip()}\n"


def passed(command: str, done: Completed) -> bool:
    if done.exit_code == 0:
        return True
    return command == "pytest" and done.exit_code == PYTEST_NO_TESTS


def check_argv(command: VerifyCommand, excluded: Sequence[str]) -> list[str]:
    """The harness's own command line for a check over the worktree, minus ``excluded``
    directories. Fixed arguments only: nothing here comes from a model."""
    match command:
        case "ruff_check":
            skip = ["--extend-exclude", ",".join(excluded)] if excluded else []
            return ["ruff", "check", "--no-fix", "--no-cache", *skip, "."]
        case "ruff_format_check":
            skip = ["--extend-exclude", ",".join(excluded)] if excluded else []
            return ["ruff", "format", "--check", "--diff", "--no-cache", *skip, "."]
        case "mypy":
            pattern = "^(" + "|".join(re.escape(d) for d in excluded) + ")/"
            return ["mypy", *(["--exclude", pattern] if excluded else []), "."]
        case "pytest":
            return ["pytest", "-q", "-p", "no:cacheprovider", *(f"--ignore={d}" for d in excluded)]
        case _:  # pragma: no cover - exhaustive over VerifyCommand
            assert_never(command)


def outside(paths: Sequence[str], directories: Sequence[str]) -> list[str]:
    return [p for p in paths if not any(p == d or p.startswith(f"{d}/") for d in directories)]


def _preference_result(findings: list[CheckFinding], checked: int) -> CheckResult:
    hard = [f for f in findings if f.enforcement == "hard"]
    lines = [f.render() for f in findings] or ["no findings"]
    summary = f"{len(findings)} finding(s), {len(hard)} hard, in {checked} changed file(s)"
    return CheckResult("preferences", not hard, "\n".join([summary, *lines]))


def removed_section(removed: Sequence[str]) -> str:
    listed = "\n".join(f"- `{p}`" for p in removed)
    return (
        "## Files outside the tree\n\nThese are ignored by git, so they are in no tree and "
        "would not be delivered. They were removed before checking: code that needs them "
        f"fails once delivered.\n\n{listed}\n"
    )


async def pytest_configuration(
    worktree: RunWorktree, base: TreeHash, head: TreeHash, tester: Sequence[str]
) -> CheckResult | None:
    """The guard on what decides how the Tester's tests run, or None if nothing it
    watches changed."""
    controls = pytest_controls(tester)
    changed = await worktree.changed_files(base, head, controls, deleted=True)
    problems = []
    for path in changed:
        before, after = await worktree.read(base, path), await worktree.read(head, path)
        if problem := reconfigured(path, before, after):
            problems.append(problem)
    if not problems:
        return None
    where = ", ".join(f"{d}/" for d in tester)
    return CheckResult(
        "pytest configuration",
        False,
        "The Tester's tests run under the base tree's pytest configuration: no role may "
        f"change what decides which of the tests in {where} run, or how.\n\n"
        + "\n".join(f"- {p}" for p in problems)
        + "\n\nRevert these changes. A fixture your own tests need belongs in their module, "
        "or in a conftest.py of a directory that is not above the Tester's.",
    )


class VerifyExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, RunStaticAnalysis):
            raise TypeError(f"static analysis cannot execute {command.kind}")
        ctx = self.context
        removed = await ctx.worktree.prune()
        current = await ctx.worktree.snapshot()
        if current != command.tree:
            raise RuntimeError(
                f"the worktree holds tree {current}, but static analysis was asked for "
                f"{command.tree}"
            )
        sandbox = ctx.sandbox(Role.CODER)
        # The Tester's directories are checked in UNIT_TESTING, where a failure goes to the
        # Tester: here it would go to the Coder, who may not edit them.
        tester = ctx.config.capabilities.owned_directories(Role.TESTER)
        results: list[CheckResult] = []
        for name in ctx.config.verify.commands:
            argv = check_argv(name, tester)
            done = await sandbox.execute([*sandbox.config.launcher, *argv], shown=" ".join(argv))
            results.append(CheckResult(name, passed(name, done), done.render()))
        base = await ctx.worktree.base_tree()
        changed = outside(await ctx.worktree.changed_files(base, command.tree), tester)
        if changed:
            findings = await run_checks(sandbox, ctx.preferences(), changed)
            results.append(_preference_result(findings, len(changed)))
        if guard := await pytest_configuration(ctx.worktree, base, command.tree, tester):
            results.append(guard)

        ok = all(r.passed for r in results)
        failed = [r.name for r in results if not r.passed]
        heading = f"# Static analysis of tree {command.tree}: " + (
            "passed" if ok else f"FAILED ({', '.join(failed)})"
        )
        notes = [removed_section(removed)] if removed else []
        report = "\n".join([heading, "", *(r.render() for r in results), *notes])
        digest = ctx.files.put(report)
        ctx.files.record(
            "verifications.jsonl",
            {
                "effect": effect.id,
                "tree": command.tree,
                "passed": ok,
                "failed": failed,
                "report": digest,
            },
        )
        if ok:
            return StaticAnalysisPassed(at=now, effect_id=effect.id, tree=command.tree)
        return StaticAnalysisFailed(at=now, effect_id=effect.id, tree=command.tree, report=digest)
