"""VERIFYING (PLAN §2): deterministic checks on the head tree. No model is involved.

``run_verification(tree)`` first proves the worktree holds ``tree`` (evidence is bound to
the tree it was produced for). Then it runs, in order:

1. each command in ``[verify].commands``, over the whole worktree, in the sandbox;
2. the machine checks of the preferences that apply to the files the run changed
   (``msa.runtime.checks``). Hard findings fail verification; soft and info findings are
   reported but do not.

Every check runs even after one fails, so a single revision can fix them all. The report
is stored as an artifact either way: ``VerificationFailed`` names it, and the Coder's
revision reads it.

A check that cannot run at all (the program is missing, a checker crashes) is not the
code's fault and no revision can fix it. It raises, and the interpreter turns that into
``EffectFailed``: the run stops and says why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from msa.config import Role
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


def _passed(command: str, done: Completed) -> bool:
    if done.exit_code == 0:
        return True
    return command == "pytest" and done.exit_code == PYTEST_NO_TESTS


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
        results: list[CheckResult] = []
        for name in ctx.config.verify.commands:
            done = await sandbox.run(name, ["."])
            results.append(CheckResult(name, _passed(name, done), done.render()))
        changed = await ctx.worktree.changed_files(await ctx.worktree.base_tree(), command.tree)
        if changed:
            findings = await run_checks(sandbox, ctx.preferences(), changed)
            results.append(_preference_result(findings, len(changed)))

        passed = all(r.passed for r in results)
        failed = [r.name for r in results if not r.passed]
        heading = f"# Verification of tree {command.tree}: " + (
            "passed" if passed else f"FAILED ({', '.join(failed)})"
        )
        report = "\n".join([heading, "", *(r.render() for r in results)])
        digest = ctx.files.put(report)
        ctx.files.record(
            "verifications.jsonl",
            {
                "effect": effect.id,
                "tree": command.tree,
                "passed": passed,
                "failed": failed,
                "report": digest,
            },
        )
        if passed:
            return VerificationPassed(at=now, effect_id=effect.id, tree=command.tree)
        return VerificationFailed(at=now, effect_id=effect.id, tree=command.tree, report=digest)
