"""VERIFYING (PLAN §2): deterministic checks on the head tree. No model is involved.

``run_verification(tree)`` first proves the worktree holds ``tree`` (evidence is bound to
the tree it was produced for). Then it runs, in order:

1. each command in ``[verify].commands``, over the whole worktree, in the sandbox;
2. the machine checks of the preferences that apply to the files the run changed
   (``msa.runtime.checks``). Hard findings fail verification; soft and info findings are
   reported but do not.

Both leave out the Tester's directories (``tests/acceptance``, ``tests/repro``). A failure
here is the Coder's to fix, and the Coder may not edit those paths: TESTING checks them
instead, and sends their failures to the Tester. The acceptance tests themselves run in
TESTING too, where fail-before/pass-after and flakes are judged.

Every check runs even after one fails, so a single revision can fix them all. The report
is stored as an artifact either way: ``VerificationFailed`` names it, and the Coder's
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
from msa.core.effects import Effect, RunVerification
from msa.core.events import AnyEvent, VerificationFailed, VerificationPassed
from msa.runtime.checks import CheckFinding, run_checks
from msa.runtime.context import RunContext
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


class VerifyExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, RunVerification):
            raise TypeError(f"verification cannot execute {command.kind}")
        ctx = self.context
        current = await ctx.worktree.snapshot()
        if current != command.tree:
            raise RuntimeError(
                f"the worktree holds tree {current}, but verification was asked for {command.tree}"
            )
        sandbox = ctx.sandbox(Role.CODER)
        # The Tester's directories are checked in TESTING, where a failure goes to the
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

        ok = all(r.passed for r in results)
        failed = [r.name for r in results if not r.passed]
        heading = f"# Verification of tree {command.tree}: " + (
            "passed" if ok else f"FAILED ({', '.join(failed)})"
        )
        report = "\n".join([heading, "", *(r.render() for r in results)])
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
            return VerificationPassed(at=now, effect_id=effect.id, tree=command.tree)
        return VerificationFailed(at=now, effect_id=effect.id, tree=command.tree, report=digest)
