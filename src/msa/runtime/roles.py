"""Executors for the agent effects: the Planner (M6), the Coder (M6) and the Tester (M7).

- ``request_plan``: the Planner reads the base tree and answers a ``Plan``. The plan is
  stored as an artifact and the event carries its digest (``PlanRef``).
- ``implement_plan``: the Coder implements the plan in the build worktree.
- ``author_acceptance_tests``: the Tester writes acceptance tests in its own worktree, at
  the base tree, in parallel with the Coder and blind to the implementation.
- ``revise_changes``: the owner named by the command fixes what a failure report names.
  The worktree is snapshotted afterwards, and the event carries the new head tree. A Tester
  revision that removes a test or a check fails (``msa.runtime.guard``).

**Re-execution after a crash** (ADR-007) resumes the invocation rather than restarting it:
paid turns replay from the response store and their tool results are reused, so the
worktree must be as the first execution left it. The worktree is therefore moved to the
effect's starting tree only when no turn of the effect was paid for yet.

Revisions that answer review findings arrive with the Reviewer (M8). Until then they fail
loudly instead of guessing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from msa.agents import coder, planner, tester
from msa.agents.schemas import ImplementationSummary, Plan
from msa.agents.toolset import toolset
from msa.config import Role
from msa.core.effects import (
    AuthorAcceptanceTests,
    Effect,
    ImplementPlan,
    RequestPlan,
    ReviseChanges,
)
from msa.core.events import (
    AcceptanceTestsAuthored,
    AgentFailed,
    AnyEvent,
    ImplementationCompleted,
    PlanProduced,
    RevisionCompleted,
)
from msa.core.ids import PlanId, TreeHash
from msa.core.values import Owner, PlanRef
from msa.llm.gateway import InvocationResult, Tool
from msa.runtime.context import RunContext, reservation
from msa.runtime.guard import weakened
from msa.runtime.interpreter import Executor

PLAN_FILE = "plan.json"


class PlannerExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, RequestPlan):
            raise TypeError(f"the planner cannot execute {command.kind}")
        ctx = self.context
        role = Role.PLANNER
        invocation = planner.invocation(
            task=ctx.task,
            files=await ctx.worktree.files(await ctx.worktree.base_tree()),
            preferences=await ctx.preference_block(),
            feedback=command.feedback,
            tools=toolset(ctx.workspace(role), ctx.sandbox(role)),
            budget_usd_micros=reservation(effect),
            key=effect.id,
            max_files=ctx.config.agents.repo_map_max_paths,
            max_turns=ctx.config.agents.planner_max_turns,
        )
        answer = await ctx.invoke(effect, invocation, now=now)
        if not isinstance(answer, InvocationResult):
            return answer
        plan: Plan = answer.output
        digest = ctx.files.put(plan.model_dump_json(indent=2))
        ref = PlanRef(id=PlanId(f"plan-{command.attempt}"), digest=digest)
        return PlanProduced(at=now, effect_id=effect.id, plan=ref, spend=answer.spend)


class CoderExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        match effect.command:
            case ImplementPlan() as command:
                return await self._implement(effect, command, now)
            case ReviseChanges() as command:
                return await self._revise(effect, command, now)
            case other:
                raise TypeError(f"the coder cannot execute {other.kind}")

    async def _implement(self, effect: Effect, command: ImplementPlan, now: datetime) -> AnyEvent:
        ctx = self.context
        plan = Plan.model_validate_json(ctx.files.get(command.plan.digest))
        ctx.files.write(PLAN_FILE, plan.model_dump_json(indent=2))  # what revisions follow
        if not ctx.has_paid_turns(effect):
            await ctx.worktree.reset(command.base_tree)
        invocation = coder.implement(
            task=ctx.task,
            plan=plan,
            preferences=await ctx.preference_block(),
            tools=self._tools(),
            budget_usd_micros=reservation(effect),
            key=effect.id,
            max_turns=ctx.config.agents.coder_max_turns,
        )
        answer = await ctx.invoke(effect, invocation, now=now)
        if not isinstance(answer, InvocationResult):
            return answer
        self._keep(effect, answer.output)
        return ImplementationCompleted(at=now, effect_id=effect.id, spend=answer.spend)

    async def _revise(self, effect: Effect, command: ReviseChanges, now: datetime) -> AnyEvent:
        if command.owner is not Owner.CODER:
            raise TypeError(f"the coder cannot revise what the {command.owner.value} owns")
        if command.findings:
            raise NotImplementedError("revisions for review findings arrive with the Reviewer (M8)")
        if command.failure_report is None:
            raise ValueError("a coder revision without findings needs a failure report")
        ctx = self.context
        stored = ctx.files.read(PLAN_FILE)
        if stored is None:
            raise FileNotFoundError(f"{PLAN_FILE} is missing: no plan was implemented in this run")
        if not ctx.has_paid_turns(effect):
            await ctx.worktree.reset(command.tree)
        invocation = coder.revise(
            task=ctx.task,
            plan=Plan.model_validate_json(stored),
            preferences=await ctx.preference_block(),
            failure_report=ctx.files.text(command.failure_report),
            tools=self._tools(),
            budget_usd_micros=reservation(effect),
            key=effect.id,
            max_turns=ctx.config.agents.coder_max_turns,
        )
        answer = await ctx.invoke(effect, invocation, now=now)
        if not isinstance(answer, InvocationResult):
            return answer
        self._keep(effect, answer.output)
        head = await ctx.worktree.snapshot()
        return RevisionCompleted(at=now, effect_id=effect.id, head_tree=head, spend=answer.spend)

    def _tools(self) -> tuple[Tool[Any], ...]:
        ctx = self.context
        return toolset(ctx.workspace(Role.CODER), ctx.sandbox(Role.CODER))

    def _keep(self, effect: Effect, summary: ImplementationSummary) -> None:
        _keep(self.context, effect, summary)


class TesterExecutor:
    """The Tester's two effects: blind authoring (``author_acceptance_tests``, in its own
    worktree at the base tree) and revisions of its own tests (``revise_changes`` with
    owner ``tester``, in the build worktree, behind the weakened-test guard)."""

    __test__ = False  # not a pytest test class, whatever its name

    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        match effect.command:
            case AuthorAcceptanceTests() as command:
                return await self._author(effect, command, now)
            case ReviseChanges(owner=Owner.TESTER) as command:
                return await self._revise(effect, command, now)
            case other:
                raise TypeError(f"the tester cannot execute {other.kind}")

    async def _author(
        self, effect: Effect, command: AuthorAcceptanceTests, now: datetime
    ) -> AnyEvent:
        ctx = self.context
        mine = ctx.tester_worktree
        await mine.ensure(command.base_tree)
        if not ctx.has_paid_turns(effect):
            await mine.reset(command.base_tree)
        role = Role.TESTER
        invocation = tester.author(
            task=ctx.task,
            plan=Plan.model_validate_json(ctx.files.get(command.plan.digest)),
            preferences=await ctx.preference_block(),
            tools=toolset(ctx.workspace(role, worktree=mine), ctx.sandbox(role, worktree=mine)),
            budget_usd_micros=reservation(effect),
            key=effect.id,
            max_turns=ctx.config.agents.tester_max_turns,
        )
        answer = await ctx.invoke(effect, invocation, now=now)
        if not isinstance(answer, InvocationResult):
            return answer
        _keep(ctx, effect, answer.output)
        return AcceptanceTestsAuthored(at=now, effect_id=effect.id, spend=answer.spend)

    async def _revise(self, effect: Effect, command: ReviseChanges, now: datetime) -> AnyEvent:
        if command.findings:
            raise NotImplementedError("revisions for review findings arrive with the Reviewer (M8)")
        if command.failure_report is None:
            raise ValueError("a tester revision needs a failure report")
        ctx = self.context
        stored = ctx.files.read(PLAN_FILE)
        if stored is None:
            raise FileNotFoundError(f"{PLAN_FILE} is missing: no plan was implemented in this run")
        if not ctx.has_paid_turns(effect):
            await ctx.worktree.reset(command.tree)
        owned = ctx.config.capabilities.owned_directories(Role.TESTER)
        before = await self._sources(command.tree, owned)
        role = Role.TESTER
        invocation = tester.revise(
            task=ctx.task,
            plan=Plan.model_validate_json(stored),
            preferences=await ctx.preference_block(),
            failure_report=ctx.files.text(command.failure_report),
            tools=toolset(ctx.workspace(role), ctx.sandbox(role)),
            budget_usd_micros=reservation(effect),
            key=effect.id,
            max_turns=ctx.config.agents.tester_max_turns,
        )
        answer = await ctx.invoke(effect, invocation, now=now)
        if not isinstance(answer, InvocationResult):
            return answer
        _keep(ctx, effect, answer.output)
        head = await ctx.worktree.snapshot()
        changed = await ctx.worktree.changed_files(command.tree, head, owned)
        after = {p: _read(ctx.worktree.path / p) for p in {*before, *changed}}
        rejected = {
            test
            for record in ctx.files.records("tests.jsonl")
            if record.get("report") == command.failure_report
            for test in record.get("rejected", [])
        }
        if problems := weakened(before, after, rejected):
            error = "the tester weakened its tests without a reviewer's approval: " + "; ".join(
                problems
            )
            return AgentFailed(at=now, effect_id=effect.id, error=error, spend=answer.spend)
        return RevisionCompleted(at=now, effect_id=effect.id, head_tree=head, spend=answer.spend)

    async def _sources(self, tree: TreeHash, owned: Sequence[str]) -> dict[str, str]:
        """The Tester's Python files as of ``tree`` (the worktree holds it right now)."""
        ctx = self.context
        files = [
            p
            for p in await ctx.worktree.files(tree)
            if p.endswith(".py") and any(p.startswith(f"{d}/") for d in owned)
        ]
        return {p: text for p in files if (text := _read(ctx.worktree.path / p)) is not None}


class Revisions:
    """``revise_changes`` goes to whichever role owns it."""

    def __init__(self, coder: Executor, tester: Executor) -> None:
        self.coder = coder
        self.tester = tester

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, ReviseChanges):
            raise TypeError(f"revisions cannot execute {command.kind}")
        owner = self.tester if command.owner is Owner.TESTER else self.coder
        return await owner.execute(effect, now=now)


def _keep(ctx: RunContext, effect: Effect, summary: BaseModel) -> None:
    ctx.files.record(
        "summaries.jsonl",
        {"effect": effect.id, "kind": effect.command.kind, **summary.model_dump(mode="json")},
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return None
