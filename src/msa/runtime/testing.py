"""TESTING (PLAN §6.1): is the change proven by tests that mean something?

``run_test_suite(tree, base_tree)`` first proves the build worktree holds ``tree``. Then:

1. **There are acceptance tests.** The Tester's new test files (under its own directories)
   must exist: a plan's acceptance criteria with no tests prove nothing.
2. **They are clean.** ``ruff check`` and ``ruff format --check`` on the Tester's files, and
   the preference checks: VERIFYING left these to TESTING, where a failure goes to their
   owner.
3. **Everything passes after the change (pass-after).** The whole suite runs on ``tree``.
4. **Flakes are told apart, never skipped.** Each failing test is rerun alone, up to
   ``[testing].flake_reruns`` times. A test with mixed results is ``flaky``: it is reported
   and goes back to the Tester. It is never quarantined to get a green run.
5. **Every acceptance test fails before the change (fail-before).** The acceptance tests
   run against the base tree in a scratch worktree. A test that passes there would pass
   whatever the Coder did, so it is rejected (I-03).
6. **Consistent failures are triaged** by the Tester role, read-only: ``impl_bug`` goes to
   the Coder, ``test_bug`` to the Tester, ``environment`` fails the run (no agent can fix
   it). Flaky and rejected tests are the Tester's without asking a model.

Problems that need no model are reported first, so a model is paid only to diagnose what a
rule cannot. The report (a test matrix of before/after per acceptance test, plus every
problem) is stored as an artifact; ``TestsFailed`` names it and the revision reads it.

Test outcomes come from pytest's JUnit XML, written outside the worktree, so nothing the
run produces lands in a tree. A suite that cannot run at all (pytest's internal and usage
errors, a timeout) is an environment failure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from msa.agents import tester
from msa.agents.schemas import Plan, TriageKind
from msa.agents.toolset import toolset
from msa.config import Role
from msa.core.effects import Effect, RunTestSuite
from msa.core.events import AnyEvent, TestsFailed, TestsPassed
from msa.core.ids import Digest, TreeHash
from msa.core.values import FailureKind, Owner, Triage
from msa.llm.gateway import InvocationResult
from msa.runtime.checks import run_checks
from msa.runtime.context import RunContext, reservation
from msa.runtime.roles import PLAN_FILE
from msa.runtime.verify import PYTEST_NO_TESTS
from msa.runtime.worktree import RunWorktree
from msa.tools.sandbox import Completed, LocalSandbox

PYTEST_FAILED = 1
PYTEST_COLLECTION_ERRORS = 2

TRIAGE_OWNERS: dict[TriageKind, tuple[FailureKind, Owner | None]] = {
    TriageKind.IMPL_BUG: (FailureKind.IMPL_BUG, Owner.CODER),
    TriageKind.TEST_BUG: (FailureKind.TEST_BUG, Owner.TESTER),
    TriageKind.ENVIRONMENT: (FailureKind.ENVIRONMENT, None),
}


class SuiteUnrunnable(RuntimeError):
    """pytest could not run the suite at all: no agent can fix that."""


@dataclass(frozen=True, slots=True)
class Case:
    """One test's outcome in one pytest run."""

    nodeid: str
    passed: bool  # passed, skipped or xfailed: nothing to fix
    message: str = ""


@dataclass(frozen=True, slots=True)
class Run:
    done: Completed
    cases: tuple[Case, ...]

    @property
    def failed(self) -> list[Case]:
        return [c for c in self.cases if not c.passed]


@dataclass
class Findings:
    """What TESTING found, in the order it is resolved: the Tester's own problems first."""

    flaky: dict[str, str] = field(default_factory=dict)  # nodeid -> rerun results
    tester: list[str] = field(default_factory=list)  # rule-based test_bug problems
    rejected: list[str] = field(default_factory=list)  # tests that pass before the change
    failures: list[Case] = field(default_factory=list)  # consistent failures, to triage
    matrix: list[tuple[str, str, str]] = field(default_factory=list)  # (test, before, after)
    sections: list[str] = field(default_factory=list)  # report sections, in order


# ---------- pytest's JUnit report


def _nodeid(case: ElementTree.Element) -> str:
    """``path::Class::name`` from an xunit1 testcase (its ``file`` attribute)."""
    name, classname, file = case.get("name", ""), case.get("classname", ""), case.get("file")
    if not file:
        return f"{classname}::{name}" if classname else name
    module = str(PurePosixPath(file).with_suffix("")).replace("/", ".")
    classes = classname.removeprefix(module).strip(".")
    return "::".join(p for p in (file, *classes.split("."), name) if p)


def parse_junit(text: str) -> tuple[Case, ...]:
    root = ElementTree.fromstring(text)  # pytest's own output; it declares no entities
    cases = []
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        message = ""
        if problem is not None:
            lines = (problem.get("message") or problem.text or "").strip().splitlines()
            message = lines[0] if lines else problem.tag
        cases.append(Case(_nodeid(case), problem is None, message))
    return tuple(sorted(cases, key=lambda c: c.nodeid))


async def run_pytest(sandbox: LocalSandbox, targets: Sequence[str], junit: Path) -> Run:
    junit.parent.mkdir(parents=True, exist_ok=True)
    junit.unlink(missing_ok=True)
    argv = ["pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1"]
    argv += [f"--junitxml={junit}", "--", *targets]
    shown = " ".join(["pytest", "-q", *targets])
    done = await sandbox.execute([*sandbox.config.launcher, *argv], shown=shown)
    if done.timed_out:
        raise SuiteUnrunnable(f"the test suite timed out:\n{done.render()}")
    if done.exit_code not in {0, PYTEST_FAILED, PYTEST_COLLECTION_ERRORS, PYTEST_NO_TESTS}:
        raise SuiteUnrunnable(f"pytest could not run the suite:\n{done.render()}")
    cases = parse_junit(junit.read_text(encoding="utf-8")) if junit.exists() else ()
    if done.exit_code in {PYTEST_FAILED, PYTEST_COLLECTION_ERRORS} and not any(
        not c.passed for c in cases
    ):
        # A failure pytest did not attribute to a test (a broken conftest, say).
        cases = (*cases, Case("<collection>", False, "pytest failed outside any test"))
    return Run(done, cases)


# ---------- the executor


class TestSuiteExecutor:
    __test__ = False  # not a pytest test class

    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, RunTestSuite):
            raise TypeError(f"the test suite cannot execute {command.kind}")
        ctx = self.context
        current = await ctx.worktree.snapshot()
        if current != command.tree:
            raise RuntimeError(
                f"the worktree holds tree {current}, but tests were asked for {command.tree}"
            )
        tree = command.tree
        try:
            found = await self._examine(effect, tree, command.base_tree)
        except SuiteUnrunnable as exc:
            report = f"# Tests of tree {tree}: cannot run\n\n{exc}\n"
            environment = Triage(kind=FailureKind.ENVIRONMENT, owner=None)
            return self._failed(effect, tree=tree, now=now, triage=environment, report=report)

        # The Tester's own problems need no model to diagnose: flaky first, then the rest.
        for problems, kind in (
            (found.flaky, FailureKind.FLAKY),
            (found.tester, FailureKind.TEST_BUG),
        ):
            if problems:
                return self._failed(
                    effect,
                    tree=tree,
                    now=now,
                    triage=Triage(kind=kind, owner=Owner.TESTER),
                    report=self._report(tree, found, "FAILED"),
                    rejected=found.rejected,
                )
        if found.failures:
            return await self._triage(effect, command.tree, now, found)
        report = self._report(command.tree, found, "passed")
        digest = ctx.files.put(report)
        self._record(effect, tree=command.tree, verdict="passed", kind=None, report=digest)
        return TestsPassed(at=now, effect_id=effect.id, tree=command.tree)

    # ---------- the checks

    async def _examine(self, effect: Effect, tree: TreeHash, base: TreeHash) -> Findings:
        ctx = self.context
        owned = ctx.config.capabilities.owned_directories(Role.TESTER)
        acceptance = [
            p for p in await ctx.worktree.changed_files(base, tree, owned) if p.endswith(".py")
        ]
        found = Findings()
        sandbox = ctx.sandbox(Role.TESTER)
        junit = ctx.files.directory.resolve() / "junit" / effect.id.replace(":", "-")

        if not acceptance:
            found.tester.append(
                "No acceptance tests were written: every acceptance criterion in the plan "
                f"needs a test under {', '.join(f'{d}/' for d in owned) or 'the tester paths'}."
            )
        else:
            await self._lint(sandbox, acceptance, found)

        after = await run_pytest(sandbox, [], junit / "after.xml")
        found.sections.append(f"## Full suite on the head tree\n\n{after.done.render()}\n")
        for case in after.failed:
            if "::" not in case.nodeid:  # a collection error: nothing to rerun on its own
                found.failures.append(case)
                continue
            results = await self._rerun(sandbox, case.nodeid, junit)
            if any(results):
                shown = ", ".join("pass" if r else "fail" for r in [False, *results])
                found.flaky[case.nodeid] = shown
            else:
                found.failures.append(case)
        if found.flaky:
            lines = [f"- `{t}`: {r}" for t, r in sorted(found.flaky.items())]
            found.sections.append(
                "## Flaky tests\n\nThese failed, then passed when rerun alone. They are not "
                "skipped: make them deterministic.\n\n" + "\n".join(lines) + "\n"
            )

        if acceptance:
            before = await self._before(base, tree, owned, acceptance, junit)
            # On the base tree, acceptance tests often fail at import, as one collection
            # error for the file; that is a failure for every test in it.
            passed_before = {c.nodeid for c in before.cases if c.passed and "::" in c.nodeid}
            after_by_id = {c.nodeid: c for c in after.cases}
            tests = {c.nodeid for c in after.cases if c.nodeid.split("::")[0] in acceptance}
            for test in sorted(tests | passed_before):
                state = after_by_id.get(test)
                after_text = (
                    "flaky"
                    if test in found.flaky
                    else "pass"
                    if state is not None and state.passed
                    else "fail"
                )
                before_text = "pass" if test in passed_before else "fail"
                found.matrix.append((test, before_text, after_text))
            for test in sorted(passed_before):
                found.rejected.append(test)
                found.tester.append(
                    f"`{test}` passes on the base tree, before the change: it cannot tell "
                    "whether the change works. Make it test what the plan adds."
                )
        return found

    async def _lint(self, sandbox: LocalSandbox, files: list[str], found: Findings) -> None:
        launcher = sandbox.config.launcher
        for argv in (
            ["ruff", "check", "--no-fix", "--no-cache", "--", *files],
            ["ruff", "format", "--check", "--diff", "--no-cache", "--", *files],
        ):
            done = await sandbox.execute([*launcher, *argv], shown=" ".join(argv[:3]))
            if done.exit_code != 0:
                found.tester.append(f"Your tests fail `{' '.join(argv[:3])}`:\n\n{done.render()}")
        resolved = self.context.preferences()
        hard = [f for f in await run_checks(sandbox, resolved, files) if f.enforcement == "hard"]
        if hard:
            found.tester.append(
                "Your tests break hard preferences:\n\n" + "\n".join(f.render() for f in hard)
            )

    async def _rerun(self, sandbox: LocalSandbox, nodeid: str, junit: Path) -> list[bool]:
        results = []
        for attempt in range(self.context.config.testing.flake_reruns):
            run = await run_pytest(sandbox, [nodeid], junit / f"rerun-{attempt}.xml")
            mine = [c for c in run.cases if c.nodeid == nodeid]
            results.append(bool(mine) and all(c.passed for c in mine))
            if results[-1]:
                break  # one pass after a failure is already mixed: flaky
        return results

    async def _before(
        self,
        base: TreeHash,
        tree: TreeHash,
        owned: Sequence[str],
        acceptance: list[str],
        junit: Path,
    ) -> Run:
        """The acceptance tests, run against the base tree: they must all fail there."""
        scratch: RunWorktree = self.context.base_worktree
        await scratch.ensure(base)
        await scratch.reset(base)
        await scratch.overlay(tree, base, owned)
        scratch_sandbox = self.context.sandbox(Role.TESTER, worktree=scratch)
        run = await run_pytest(scratch_sandbox, acceptance, junit / "before.xml")
        await scratch.reset(base)
        return run

    # ---------- answers

    async def _triage(
        self, effect: Effect, tree: TreeHash, now: datetime, found: Findings
    ) -> AnyEvent:
        ctx = self.context
        draft = self._report(tree, found, "FAILED")
        stored = ctx.files.read(PLAN_FILE)
        if stored is None:
            raise FileNotFoundError(f"{PLAN_FILE} is missing: no plan was implemented in this run")
        role = Role.TESTER
        invocation = tester.triage(
            task=ctx.task,
            plan=Plan.model_validate_json(stored),
            preferences=await ctx.preference_block(),
            failure_report=draft,
            tools=toolset(ctx.workspace(role), ctx.sandbox(role)),
            budget_usd_micros=reservation(effect),
            key=effect.id,
            max_turns=ctx.config.agents.triage_max_turns,
        )
        answer = await ctx.invoke(effect, invocation, now=now)
        if not isinstance(answer, InvocationResult):
            return answer
        diagnosis = answer.output
        kind, owner = TRIAGE_OWNERS[diagnosis.kind]
        report = (
            f"{draft}\n## Triage: {diagnosis.kind.value} (owner: {owner or 'none'})\n\n"
            f"`{diagnosis.test_id}`: {diagnosis.diagnosis.strip()}\n"
        )
        triage = Triage(kind=kind, owner=owner)
        event = self._failed(effect, tree=tree, now=now, triage=triage, report=report)
        return event.model_copy(update={"spend": answer.spend})

    def _failed(
        self,
        effect: Effect,
        *,
        tree: TreeHash,
        now: datetime,
        triage: Triage,
        report: str,
        rejected: Sequence[str] = (),
    ) -> TestsFailed:
        digest = self.context.files.put(report)
        self._record(
            effect, tree=tree, verdict="failed", kind=triage.kind, report=digest, rejected=rejected
        )
        return TestsFailed(
            at=now,
            effect_id=effect.id,
            tree=tree,
            triage=triage,
            report=digest,
        )

    def _report(self, tree: TreeHash, found: Findings, verdict: str) -> str:
        out = [f"# Tests of tree {tree}: {verdict}", ""]
        if found.matrix:
            out += ["## Acceptance tests: fail before, pass after", ""]
            out += ["| test | before the change | after |", "|---|---|---|"]
            out += [f"| `{t}` | {b} | {a} |" for t, b, a in found.matrix]
            out.append("")
        if found.tester:
            out += ["## Problems with the tests", ""]
            out += [f"- {problem}" for problem in found.tester]
            out.append("")
        if found.failures:
            out += ["## Consistent failures", ""]
            out += [f"- `{c.nodeid}`: {c.message or 'failed'}" for c in found.failures]
            out.append("")
        out += found.sections
        return "\n".join(out)

    def _record(
        self,
        effect: Effect,
        *,
        tree: TreeHash,
        verdict: str,
        kind: FailureKind | None,
        report: Digest,
        rejected: Sequence[str] = (),
    ) -> None:
        """``rejected``: tests that pass before the change. A Tester revision may replace
        them without tripping the weakened-test guard: they were never evidence."""
        self.context.files.record(
            "tests.jsonl",
            {
                "effect": effect.id,
                "tree": tree,
                "verdict": verdict,
                "kind": kind.value if kind else None,
                "report": report,
                "rejected": list(rejected),
            },
        )
