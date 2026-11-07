"""Executors for the agent effects of M6: the Planner and the Coder.

- ``request_plan``: the Planner reads the base tree and answers a ``Plan``. The plan is
  stored as an artifact and the event carries its digest (``PlanRef``).
- ``implement_plan``: the Coder implements the plan in the run's worktree.
- ``revise_changes`` (owner ``coder``): the Coder fixes what a failure report names.
  The worktree is snapshotted afterwards, and the event carries the new head tree.

**Re-execution after a crash** (ADR-007) resumes the invocation rather than restarting it:
paid turns replay from the response store and their tool results are reused, so the
worktree must be as the first execution left it. The worktree is therefore moved to the
effect's starting tree only when no turn of the effect was paid for yet.

Revisions owned by the Tester, and revisions that answer review findings, arrive with the
Tester (M7) and the Reviewer (M8). Until then they fail loudly instead of guessing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from msa.agents import coder, planner
from msa.agents.schemas import ImplementationSummary, Plan
from msa.agents.toolset import toolset
from msa.config import Role
from msa.core.effects import Effect, ImplementPlan, RequestPlan, ReviseChanges
from msa.core.events import (
    AnyEvent,
    ImplementationCompleted,
    PlanProduced,
    RevisionCompleted,
)
from msa.core.ids import PlanId
from msa.core.values import Owner, PlanRef
from msa.llm.gateway import InvocationResult, Tool
from msa.runtime.context import RunContext, reservation

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
            raise NotImplementedError("revisions owned by the tester arrive with the Tester (M7)")
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
        self.context.files.record(
            "summaries.jsonl",
            {"effect": effect.id, "kind": effect.command.kind, **summary.model_dump(mode="json")},
        )
