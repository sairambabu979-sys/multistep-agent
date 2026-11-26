"""INTEGRATION_TESTING (ADR-015): the repository's integration suite, run on the head tree.

``run_integration_tests(tree)`` removes every file the tree leaves out, proves the build
worktree holds ``tree``, and runs pytest on ``[integration].paths``, in a network namespace
of its own where the kernel allows it (``unshare -rn``): no test can reach any host, so a
passing suite does not depend on one. The report says whether it was isolated.

No model is needed to decide what a failure means:

- **Failing on the head only**: the change broke it. It goes to the Coder (``impl_bug``).
- **Failing on the base tree as well**: it was broken before the run, so no revision of
  this change can fix it. The stage blocks for a person (``environment``).
- **Flaky** (mixed results when rerun alone): it blocks for a person too. A suite this run
  did not write is not the Tester's to fix, and it is never skipped to get a green run.
- **Cannot run at all** (pytest's internal and usage errors, a timeout, or isolation
  required but unavailable): it blocks for a person.

A repository with no integration suite passes this stage with nothing run, and the report
and the PR say exactly that: it is not evidence, and nothing claims it is.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from msa.config import Role
from msa.core.effects import Effect, RunIntegrationTests
from msa.core.events import AnyEvent, IntegrationTestsFailed, IntegrationTestsPassed
from msa.core.ids import Digest, TreeHash
from msa.core.values import FailureKind, Owner, Triage
from msa.runtime.context import RunContext
from msa.runtime.testing import Case, Run, SuiteUnrunnable, run_pytest
from msa.runtime.verify import removed_section
from msa.tools.sandbox import NETWORK_NAMESPACE, LocalSandbox

ISOLATION = NETWORK_NAMESPACE
RECORDS = "integrations.jsonl"


async def isolation(sandbox: LocalSandbox) -> tuple[str, ...] | None:
    """What takes the network away: ``unshare -rn`` where this kernel lets an unprivileged
    user create the namespace, () in a container (it has none), None where neither."""
    return await sandbox.network_wrapper()


class IntegrationExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, RunIntegrationTests):
            raise TypeError(f"the integration stage cannot execute {command.kind}")
        ctx = self.context
        removed = await ctx.worktree.prune()
        current = await ctx.worktree.snapshot()
        if current != command.tree:
            raise RuntimeError(
                f"the worktree holds tree {current}, but integration tests were asked for "
                f"{command.tree}"
            )
        tree = command.tree
        notes = [removed_section(removed)] if removed else []
        suites = await self._suites(tree)
        if not suites:
            listed = ", ".join(f"`{p}`" for p in ctx.config.integration.paths) or "(none)"
            report = (
                f"# Integration tests of tree {tree}: nothing to run\n\n"
                f"The tree has no integration suite at {listed} ([integration].paths). "
                "Nothing ran, so this stage is no evidence either way.\n"
            )
            return self._passed(effect, tree, now, report, ran=False, isolated=False)

        sandbox = ctx.sandbox(Role.CODER)
        wrapper = await self._wrapper(sandbox)
        mode = ctx.config.integration.isolate_network
        if wrapper is None and mode == "always":
            report = (
                f"# Integration tests of tree {tree}: cannot run\n\n"
                "[integration].isolate_network = always, but this machine cannot create a "
                "network namespace (`unshare -rn`). Run on a host that can, or set it to "
                "auto.\n"
            )
            return self._blocked(effect, tree, now, report)
        isolated = wrapper is not None
        how = (
            "with the sandbox's network"
            if not isolated
            else "without network (`unshare -rn`)"
            if wrapper
            else "without network (in a container with none)"
        )
        junit = ctx.files.directory.resolve() / "junit" / effect.id.replace(":", "-")
        try:
            head = await run_pytest(sandbox, suites, junit / "head.xml", wrapper=wrapper or ())
            sections = [
                f"## The integration suite on the head tree, {how}\n\n{head.done.render()}\n"
            ]
            if not head.failed:
                report = "\n".join(
                    [f"# Integration tests of tree {tree}: passed", "", *sections, *notes]
                )
                return self._passed(effect, tree, now, report, ran=True, isolated=isolated)
            flaky, consistent = await self._rerun(sandbox, head.failed, junit, wrapper or ())
            before = await self._on_base(consistent, junit, wrapper or ()) if consistent else set()
        except SuiteUnrunnable as exc:
            report = f"# Integration tests of tree {tree}: cannot run\n\n{exc}\n"
            return self._blocked(effect, tree, now, report)

        broken = [c for c in consistent if c.nodeid not in before]
        sections += _listing("Fail on the head tree only: the change broke them", broken)
        sections += _listing(
            "Fail on the base tree too: broken before this run",
            [c for c in consistent if c.nodeid in before],
        )
        sections += _listing("Flaky: mixed results when rerun alone", flaky)
        report = "\n".join([f"# Integration tests of tree {tree}: FAILED", "", *sections, *notes])
        if broken:
            triage = Triage(kind=FailureKind.IMPL_BUG, owner=Owner.CODER)
            return self._failed(effect, tree, now, report, triage)
        return self._blocked(effect, tree, now, report)

    # ---------- the checks

    async def _suites(self, tree: TreeHash) -> list[str]:
        files = await self.context.worktree.files(tree)
        return [
            p
            for p in self.context.config.integration.paths
            if any(f == p or f.startswith(f"{p}/") for f in files)
        ]

    async def _wrapper(self, sandbox: LocalSandbox) -> tuple[str, ...] | None:
        if self.context.config.integration.isolate_network == "never":
            return None
        return await isolation(sandbox)

    async def _rerun(
        self, sandbox: LocalSandbox, failed: Sequence[Case], junit: Path, wrapper: Sequence[str]
    ) -> tuple[list[Case], list[Case]]:
        flaky, consistent = [], []
        for case in failed:
            if "::" not in case.nodeid:  # a module that does not import: not a flake
                consistent.append(case)
                continue
            for attempt in range(self.context.config.testing.flake_reruns):
                run = await run_pytest(
                    sandbox, [case.nodeid], junit / f"rerun-{attempt}.xml", wrapper=wrapper
                )
                mine = [c for c in run.cases if c.nodeid == case.nodeid]
                if mine and all(c.outcome == "passed" for c in mine):
                    flaky.append(case)
                    break
            else:
                consistent.append(case)
        return flaky, consistent

    async def _on_base(
        self, failing: Sequence[Case], junit: Path, wrapper: Sequence[str]
    ) -> set[str]:
        """Which of ``failing`` also fail on the base tree, in the scratch worktree."""
        ctx = self.context
        scratch = ctx.base_worktree
        base = await ctx.worktree.base_tree()
        await scratch.ensure(base)
        await scratch.reset(base)
        await scratch.prune()
        present = set(await scratch.files(base))
        targets = sorted({c.nodeid for c in failing if c.nodeid.split("::")[0] in present})
        if not targets:
            return set()
        sandbox = ctx.sandbox(Role.CODER, worktree=scratch)
        run: Run = await run_pytest(sandbox, targets, junit / "base.xml", wrapper=wrapper)
        return {c.nodeid for c in run.failed} & set(targets)

    # ---------- answers

    def _record(
        self, effect: Effect, tree: TreeHash, verdict: str, report: str, **extra: object
    ) -> Digest:
        digest = self.context.files.put(report)
        self.context.files.record(
            RECORDS,
            {"effect": effect.id, "tree": tree, "verdict": verdict, "report": digest, **extra},
        )
        return digest

    def _passed(
        self,
        effect: Effect,
        tree: TreeHash,
        now: datetime,
        report: str,
        *,
        ran: bool,
        isolated: bool,
    ) -> AnyEvent:
        self._record(effect, tree, "passed", report, ran=ran, isolated=isolated)
        return IntegrationTestsPassed(at=now, effect_id=effect.id, tree=tree)

    def _failed(
        self, effect: Effect, tree: TreeHash, now: datetime, report: str, triage: Triage
    ) -> AnyEvent:
        digest = self._record(effect, tree, "failed", report, kind=triage.kind.value)
        return IntegrationTestsFailed(
            at=now, effect_id=effect.id, tree=tree, triage=triage, report=digest
        )

    def _blocked(self, effect: Effect, tree: TreeHash, now: datetime, report: str) -> AnyEvent:
        environment = Triage(kind=FailureKind.ENVIRONMENT, owner=None)
        return self._failed(effect, tree, now, report, environment)


def _listing(title: str, cases: Sequence[Case]) -> list[str]:
    if not cases:
        return []
    lines = [f"- `{c.nodeid}`: {c.message or 'failed'}" for c in cases]
    return [f"## {title}\n\n" + "\n".join(lines) + "\n"]
