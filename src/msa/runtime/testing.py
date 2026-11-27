"""UNIT_TESTING (PLAN §6.1, ADR-015): is the change proven by tests that mean something?

``run_unit_tests(tree, base_tree)`` first removes every file the tree leaves out and proves
the build worktree holds ``tree``. Then:

1. **There are acceptance tests.** The Tester's new test modules (``test_*.py`` or
   ``*_test.py`` under its own directories) must exist, and each must define tests: a
   plan's acceptance criteria with no tests prove nothing.
2. **They are clean.** ``ruff check`` and ``ruff format --check`` on the Tester's files, and
   the preference checks: STATIC_ANALYSIS left these to this stage, where a failure goes
   to their owner.
3. **Everything passes after the change (pass-after).** The repository's own tests run on
   ``tree``, except the integration suite (``[integration].paths``, the next stage's); the
   acceptance tests run on it in a pytest process of their own, so nothing else in the
   suite (another module, another directory's conftest.py) shares their session.
   STATIC_ANALYSIS has already refused any change to the conftest.py files and pytest
   configuration above them.
4. **Every acceptance test ran and passed.** Each test the Tester wrote (found in its
   source, as pytest finds tests) must appear in that run as *passed*: skipped, xfailed and
   never-collected tests are not evidence, whatever made them so.
5. **Flakes are told apart, never skipped.** Each failing test is rerun alone, up to
   ``[testing].flake_reruns`` times. A test with mixed results is ``flaky``: it is reported
   and goes back to the Tester. It is never quarantined to get a green run.
6. **Every acceptance test fails before the change (fail-before).** The acceptance tests
   run against the base tree in a scratch worktree. A test that passes there would pass
   whatever the Coder did, so it is rejected (I-03); so is one that is skipped there or does
   not run. A collection error counts as failing, the usual way a test of new code fails.
7. **Consistent failures are triaged** by the Tester role, read-only: ``impl_bug`` goes to
   the Coder, ``test_bug`` to the Tester, ``environment`` blocks the stage for a person to
   fix (no agent can). Flaky, rejected and non-running tests are the Tester's without
   asking a model.

Problems that need no model are reported first, so a model is paid only to diagnose what a
rule cannot. The report (a test matrix of before/after per acceptance test, plus every
problem) is stored as an artifact; ``UnitTestsFailed`` names it and the revision reads it.

Test outcomes come from pytest's JUnit XML, written outside the worktree, so nothing the
run produces lands in a tree. A suite that cannot run at all (pytest's internal and usage
errors, a timeout) is an environment failure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Literal
from xml.etree import ElementTree

from msa.agents import tester
from msa.agents.schemas import Plan, TriageKind
from msa.config import Role
from msa.core.effects import Effect, RunUnitTests
from msa.core.events import AnyEvent, UnitTestsFailed, UnitTestsPassed
from msa.core.ids import Digest, TreeHash
from msa.core.values import FailureKind, Owner, Triage
from msa.llm.gateway import InvocationResult
from msa.runtime.artifacts import PLAN_FILE
from msa.runtime.checks import run_checks
from msa.runtime.context import RunContext, reservation
from msa.runtime.guard import find_tests
from msa.runtime.parallel import gather_bounded
from msa.runtime.verify import PYTEST_NO_TESTS, removed_section
from msa.runtime.worktree import RunWorktree
from msa.tools.sandbox import Completed, LocalSandbox

PYTEST_FAILED = 1
PYTEST_COLLECTION_ERRORS = 2
TEST_MODULES = ("test_*.py", "*_test.py")  # pytest's default python_files

type Outcome = Literal["passed", "failed", "skipped"]  # skipped: pytest.skip or xfail

TRIAGE_OWNERS: dict[TriageKind, tuple[FailureKind, Owner | None]] = {
    TriageKind.IMPL_BUG: (FailureKind.IMPL_BUG, Owner.CODER),
    TriageKind.TEST_BUG: (FailureKind.TEST_BUG, Owner.TESTER),
    TriageKind.ENVIRONMENT: (FailureKind.ENVIRONMENT, None),
}


class SuiteUnrunnable(RuntimeError):
    """pytest could not run the suite at all: no agent can fix that."""


@dataclass(frozen=True, slots=True)
class Case:
    """One test's outcome in one pytest run. A module that fails to import (or is skipped
    whole) is one case whose nodeid is the module's path, without ``::``."""

    nodeid: str
    outcome: Outcome
    message: str = ""
    error: bool = False  # failed by an error (a fixture, the import), not by an assertion

    @property
    def passed(self) -> bool:
        """Nothing to fix: passed, skipped or xfailed. Enough for the repository's own
        tests; an acceptance test must have outcome ``passed`` (see ``_evidence``)."""
        return self.outcome != "failed"


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
    """``path::Class::name`` from an xunit1 testcase (its ``file`` attribute). A module-level
    problem (a collection error, a module skipped whole) has an empty classname and the
    dotted module as its name: its nodeid is the module's path, which is what pytest
    accepts to rerun it."""
    name, classname, file = case.get("name", ""), case.get("classname", ""), case.get("file")
    if not file:
        return f"{classname}::{name}" if classname else name
    if not classname:
        return file
    module = str(PurePosixPath(file).with_suffix("")).replace("/", ".")
    classes = classname.removeprefix(module).strip(".")
    return "::".join(p for p in (file, *classes.split("."), name) if p)


def parse_junit(text: str) -> tuple[Case, ...]:
    root = ElementTree.fromstring(text)  # pytest's own output; it declares no entities
    cases = []
    for case in root.iter("testcase"):
        outcome: Outcome = "passed"
        problem = case.find("failure")
        error = problem is None and case.find("error") is not None
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            outcome = "failed"
        elif (problem := case.find("skipped")) is not None:
            outcome = "skipped"
        message = ""
        if problem is not None:
            lines = (problem.get("message") or problem.text or "").strip().splitlines()
            message = lines[0] if lines else problem.tag
            if problem.get("type") == "pytest.xfail":
                message = f"xfail: {message}"
        cases.append(Case(_nodeid(case), outcome, message, error))
    return tuple(sorted(cases, key=lambda c: c.nodeid))


def is_test_module(path: str) -> bool:
    return any(fnmatchcase(PurePosixPath(path).name, pattern) for pattern in TEST_MODULES)


def cases_of(run: Run, module: str, test: str) -> list[Case]:
    """``test``'s cases in ``run`` (one per parametrization), or else the case of its
    whole module (a collection error, a module skipped whole), or []."""
    nodeid = f"{module}::{test}"
    mine = [c for c in run.cases if c.nodeid == nodeid or c.nodeid.startswith(f"{nodeid}[")]
    return mine or [c for c in run.cases if c.nodeid == module]


async def run_pytest(
    sandbox: LocalSandbox,
    targets: Sequence[str],
    junit: Path,
    *,
    ignore: Sequence[str] = (),
    wrapper: Sequence[str] = (),
) -> Run:
    """``wrapper`` runs before the launcher: ``unshare -rn``, for a suite without network."""
    junit.parent.mkdir(parents=True, exist_ok=True)
    junit.unlink(missing_ok=True)
    argv = ["pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1"]
    skipped = [f"--ignore={p}" for p in ignore]
    argv += [*skipped, f"--junitxml={junit}", "--", *targets]
    shown = " ".join([*wrapper, "pytest", "-q", *skipped, *targets])
    done = await sandbox.execute([*wrapper, *sandbox.config.launcher, *argv], shown=shown)
    if done.timed_out:
        raise SuiteUnrunnable(f"the test suite timed out:\n{done.render()}")
    if done.exit_code not in {0, PYTEST_FAILED, PYTEST_COLLECTION_ERRORS, PYTEST_NO_TESTS}:
        raise SuiteUnrunnable(f"pytest could not run the suite:\n{done.render()}")
    cases = parse_junit(junit.read_text(encoding="utf-8")) if junit.exists() else ()
    if done.exit_code in {PYTEST_FAILED, PYTEST_COLLECTION_ERRORS} and not any(
        not c.passed for c in cases
    ):
        # A failure pytest did not attribute to a test (a broken conftest, say).
        cases = (*cases, Case("<collection>", "failed", "pytest failed outside any test"))
    return Run(done, cases)


# ---------- what makes an acceptance test evidence


def after_text(cases: Sequence[Case], flaky: Mapping[str, str]) -> str:
    """One acceptance test's outcome on the head tree, over its parametrizations."""
    if not cases:
        return "not run"
    if any(c.nodeid in flaky for c in cases):
        return "flaky"
    if any(c.outcome == "failed" for c in cases):
        return "fail"
    if skipped := [c for c in cases if c.outcome == "skipped"]:
        return f"skipped ({skipped[0].message or 'no reason given'})"
    return "pass"


def before_text(cases: Sequence[Case]) -> str:
    """One acceptance test's outcome on the base tree, over its parametrizations."""
    if not cases:
        return "not run"
    if any(c.outcome == "failed" for c in cases):
        return "fail"
    if any(c.outcome == "skipped" for c in cases):
        return "skipped"
    return "pass"


def judge(
    authored: Mapping[str, list[str]],
    after: Run,
    before: Run,
    found: Findings,
    *,
    repro: Collection[str] = (),
) -> None:
    """Each authored test is evidence only if it failed before the change and passed after
    it. Failing after is for triage; anything else is the Tester's to fix, without a model.

    ``repro`` modules settled a dispute: they failed on the head tree they disputed, which
    is their fail-before, so the base tree does not judge them (ADR-014)."""
    for module, names in sorted(authored.items()):
        for name in names:
            test = f"{module}::{name}"
            now = after_text(cases_of(after, module, name), found.flaky)
            then = "repro" if module in repro else before_text(cases_of(before, module, name))
            found.matrix.append((test, then, now))
            if now == "not run":
                found.tester.append(
                    f"`{test}` did not run on the head tree: pytest collected no test of that "
                    "name. An acceptance test must run and pass."
                )
                continue
            if now.startswith("skipped"):
                found.tester.append(
                    f"`{test}` was {now} on the head tree: an acceptance test must run and "
                    "pass, so it may not be skipped, xfailed or conditional on the environment."
                )
                continue
            if then in {"fail", "repro"}:
                continue
            found.rejected.append(test)
            if then == "pass":
                found.tester.append(
                    f"`{test}` passes on the base tree, before the change: it cannot tell "
                    "whether the change works. Make it test what the plan adds."
                )
            else:
                found.tester.append(
                    f"`{test}` was {then} on the base tree, so it does not show that the "
                    "change is needed: it must fail there. Make it test what the plan adds, "
                    "unconditionally."
                )


# ---------- the executor


class TestSuiteExecutor:
    __test__ = False  # not a pytest test class

    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, RunUnitTests):
            raise TypeError(f"the unit test stage cannot execute {command.kind}")
        ctx = self.context
        removed = await ctx.worktree.prune()
        current = await ctx.worktree.snapshot()
        if current != command.tree:
            raise RuntimeError(
                f"the worktree holds tree {current}, but tests were asked for {command.tree}"
            )
        tree = command.tree
        try:
            found = await self._examine(effect, tree, command.base_tree)
            if removed:
                found.sections.append(removed_section(removed))
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
        return UnitTestsPassed(at=now, effect_id=effect.id, tree=command.tree)

    # ---------- the checks

    async def _examine(self, effect: Effect, tree: TreeHash, base: TreeHash) -> Findings:
        ctx = self.context
        owned = ctx.config.capabilities.owned_directories(Role.TESTER)
        written = [
            p for p in await ctx.worktree.changed_files(base, tree, owned) if p.endswith(".py")
        ]
        modules = [p for p in written if is_test_module(p)]
        authored = {m: find_tests(await ctx.worktree.read(tree, m) or "") for m in modules}
        found = Findings()
        sandbox = ctx.sandbox(Role.TESTER)
        junit = ctx.files.directory.resolve() / "junit" / effect.id.replace(":", "-")

        if not modules:
            where = ", ".join(f"{d}/" for d in owned) or "the tester paths"
            found.tester.append(
                "No acceptance tests were written: every acceptance criterion in the plan "
                f"needs a test in a test module (`test_*.py`) under {where}."
            )
        for module, names in authored.items():
            if not names:
                found.tester.append(
                    f"`{module}` defines no tests: pytest would collect nothing from it. "
                    "Write `test_*` functions (or methods of a `Test*` class) that check the "
                    "acceptance criteria."
                )
        if written:
            await self._lint(sandbox, written, found)

        # The integration suite is the next stage's: it runs there, isolated.
        integration = [p for p in ctx.config.integration.paths if (ctx.worktree.path / p).exists()]
        suite = await run_pytest(sandbox, [], junit / "after.xml", ignore=[*modules, *integration])
        found.sections.append(
            f"## The repository's tests on the head tree\n\n{suite.done.render()}\n"
        )
        failed = suite.failed
        accepted = Run(suite.done, ())
        if modules:
            accepted = await run_pytest(sandbox, modules, junit / "acceptance.xml")
            found.sections.append(
                "## The acceptance tests on the head tree, in a session of their own\n\n"
                f"{accepted.done.render()}\n"
            )
            failed += accepted.failed
        for case in failed:
            if "::" not in case.nodeid:  # a module that does not import: one failure
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

        if modules:
            repro_dir = f"{ctx.config.testing.repro_directory}/"
            repro = {m for m in modules if m.startswith(repro_dir)}
            judged = [m for m in modules if m not in repro]
            before = (
                await self._before(base, tree, owned, judged, junit)
                if judged
                else Run(suite.done, ())
            )
            judge(authored, accepted, before, found, repro=repro)
        return found

    async def _lint(self, sandbox: LocalSandbox, files: list[str], found: Findings) -> None:
        """ruff, ruff format and the hard preferences over the Tester's files, at once: they
        only read them. The problems are listed in that order, whatever order they end in."""
        launcher = sandbox.config.launcher

        async def lint(argv: list[str]) -> str | None:
            done = await sandbox.execute([*launcher, *argv], shown=" ".join(argv[:3]))
            if done.exit_code == 0:
                return None
            return f"Your tests fail `{' '.join(argv[:3])}`:\n\n{done.render()}"

        async def hard_preferences() -> str | None:
            checked = await run_checks(sandbox, self.context.preferences(), files)
            hard = [f for f in checked if f.enforcement == "hard"]
            if not hard:
                return None
            return "Your tests break hard preferences:\n\n" + "\n".join(f.render() for f in hard)

        jobs: list[Callable[[], Awaitable[str | None]]] = [
            partial(lint, ["ruff", "check", "--no-fix", "--no-cache", "--", *files]),
            partial(lint, ["ruff", "format", "--check", "--diff", "--no-cache", "--", *files]),
            hard_preferences,
        ]
        problems = await gather_bounded(jobs, self.context.config.verify.concurrency)
        found.tester.extend(problem for problem in problems if problem is not None)

    async def _rerun(self, sandbox: LocalSandbox, nodeid: str, junit: Path) -> list[bool]:
        results = []
        for attempt in range(self.context.config.testing.flake_reruns):
            run = await run_pytest(sandbox, [nodeid], junit / f"rerun-{attempt}.xml")
            mine = [c for c in run.cases if c.nodeid == nodeid]
            results.append(bool(mine) and all(c.outcome == "passed" for c in mine))
            if results[-1]:
                break  # one pass after a failure is already mixed: flaky
        return results

    async def _before(
        self,
        base: TreeHash,
        tree: TreeHash,
        owned: Sequence[str],
        modules: list[str],
        junit: Path,
    ) -> Run:
        """The acceptance tests, run against the base tree: they must all fail there."""
        scratch: RunWorktree = self.context.base_worktree
        await scratch.ensure(base)
        await scratch.reset(base)
        await scratch.overlay(tree, base, owned)
        await scratch.prune()
        scratch_sandbox = self.context.sandbox(Role.TESTER, worktree=scratch)
        run = await run_pytest(scratch_sandbox, modules, junit / "before.xml")
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
            preferences=await ctx.guidance(role),
            failure_report=draft,
            tools=ctx.tools(role),
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
    ) -> UnitTestsFailed:
        digest = self.context.files.put(report)
        self._record(
            effect, tree=tree, verdict="failed", kind=triage.kind, report=digest, rejected=rejected
        )
        return UnitTestsFailed(
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
