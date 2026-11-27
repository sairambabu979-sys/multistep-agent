"""What the live executors of one run share: the task, the worktree, the gateway, the files.

Executors get only an effect, and an executor rebuilt on resume must answer it exactly as
the original would. So nothing here is per-effect state: everything is either fixed for the
run (the task, the configuration) or read back from disk (the worktree, the run files).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from msa.agents.toolset import toolset
from msa.config import MsaConfig, Role
from msa.core.budget import NO_SPEND, Spend
from msa.core.effects import Effect
from msa.core.events import AgentFailed, AgentRefused, AnyEvent
from msa.core.ids import RunId, TreeHash
from msa.llm.gateway import (
    Gateway,
    Invocation,
    InvocationFailed,
    InvocationRefused,
    InvocationResult,
    Tool,
    Turn,
)
from msa.runtime.artifacts import RunFiles
from msa.runtime.worktree import RunWorktree
from msa.skills.policy import Policy
from msa.skills.preferences import Resolved
from msa.skills.preferences.render import render
from msa.skills.registry import Registry, allowed_tools
from msa.skills.registry import render as render_skills
from msa.tools.audit import Violation, log_violation
from msa.tools.capabilities import WritePolicy
from msa.tools.paths import Root
from msa.tools.sandbox import LocalSandbox, make_sandbox
from msa.tools.workspace import Workspace


@dataclass
class RunContext:
    run_id: RunId
    task: str
    config: MsaConfig
    gateway: Gateway
    worktree: RunWorktree  # the build: the Coder's while BUILDING, the head tree after
    tester_worktree: RunWorktree  # the Tester's, at the base tree while BUILDING (blind)
    base_worktree: RunWorktree  # scratch, for running acceptance tests against the base
    repro_worktree: RunWorktree  # scratch, where the Tester writes repro tests at the head
    files: RunFiles
    preferences: Callable[[], Resolved]  # resolved at VALIDATING, or again on resume
    on_violation: Callable[[Violation], None] = log_violation
    # Read from the user's repository and user layer, never from a worktree: no agent can
    # add a skill or loosen the policy (ADR-016).
    skills: Callable[[], Registry] = Registry.empty
    policy: Callable[[], Policy] = Policy
    _block: dict[TreeHash, str] = field(default_factory=dict)
    _guidance: dict[Role, str] = field(default_factory=dict)

    # ---------- the tool layer, bound to one worktree for one role

    def sandbox(self, role: Role, *, worktree: RunWorktree | None = None) -> LocalSandbox:
        root = Root.at((worktree or self.worktree).path)
        return make_sandbox(
            root, self.config.sandbox, role=role.value, on_violation=self.on_violation
        )

    def workspace(self, role: Role, *, worktree: RunWorktree | None = None) -> Workspace:
        root = Root.at((worktree or self.worktree).path)
        policy = WritePolicy(role, self.config.capabilities, root.case_insensitive)
        return Workspace(root, policy, self.config.sandbox, on_violation=self.on_violation)

    def tools(self, role: Role, *, worktree: RunWorktree | None = None) -> tuple[Tool[Any], ...]:
        """The role's tools: narrowed by its skills, and never one the policy forbids it."""
        tools = toolset(
            self.workspace(role, worktree=worktree), self.sandbox(role, worktree=worktree)
        )
        allowed = allowed_tools(self.skills().for_role(role))
        forbidden = set(self.policy().forbids(role))
        return tuple(
            t for t in tools if (allowed is None or t.name in allowed) and t.name not in forbidden
        )

    # ---------- prompt inputs that must be byte-stable for the run

    async def guidance(self, role: Role) -> str:
        """The system prompt's tail for ``role``: the preference block, then the role's
        skills. Byte-stable for the run, like each part."""
        preferences = await self.preference_block()
        if role not in self._guidance:
            self._guidance[role] = render_skills(self.skills().for_role(role))
        skills = self._guidance[role]
        return "\n".join(part for part in (preferences, skills) if part)

    async def preference_block(self) -> str:
        """The preferences that apply to the base tree's files, rendered once per run."""
        base = await self.worktree.base_tree()
        if base not in self._block:
            files = await self.worktree.files(base)
            self._block[base] = render(self.preferences().select(files))
        return self._block[base]

    # ---------- invoking an agent

    def has_paid_turns(self, effect: Effect, key: str | None = None) -> bool:
        """Whether an earlier execution of ``effect`` already paid for a turn, so this one
        is a resumption that will replay it (and must find the worktree as it left it).
        ``key`` names one of several invocations an effect makes (its idempotency key)."""
        store = self.gateway.store
        return store is not None and store.get(key or effect.id, 0) is not None

    async def invoke[O: BaseModel](
        self, effect: Effect, invocation: Invocation[O], *, now: datetime
    ) -> InvocationResult[O] | AnyEvent:
        """The invocation's result, or the failure event that answers ``effect``. Either
        way the invocation is recorded for the run report."""
        try:
            result = await self.gateway.invoke(invocation)
        except InvocationRefused as exc:
            self._record(effect, invocation, exc.turns, exc.spend.usd_micros, f"refused: {exc}")
            return AgentRefused(at=now, effect_id=effect.id, category=exc.category, spend=exc.spend)
        except InvocationFailed as exc:
            self._record(effect, invocation, exc.turns, exc.spend.usd_micros, f"failed: {exc}")
            return AgentFailed(at=now, effect_id=effect.id, error=str(exc), spend=exc.spend)
        self._record(effect, invocation, result.turns, result.spend.usd_micros, "ok")
        return result

    def _record(
        self,
        effect: Effect,
        invocation: Invocation[Any],
        turns: tuple[Turn, ...],
        usd_micros: int,
        status: str,
    ) -> None:
        self.files.record(
            "invocations.jsonl",
            {
                "effect": effect.id,
                "kind": effect.command.kind,
                "role": invocation.role.value,
                "model": self.config.models.model_for(invocation.role),
                "effort": (
                    invocation.effort or self.config.models.effort_for(invocation.role)
                ).value,
                "served_by": sorted({t.model for t in turns}),
                "status": status,
                "turns": len(turns),
                "replayed": sum(t.replayed for t in turns),
                "usd_micros": usd_micros,
                "input_tokens": sum(t.spend.input_tokens for t in turns),
                "output_tokens": sum(t.spend.output_tokens for t in turns),
                "cache_read_tokens": sum(t.cache_read_tokens for t in turns),
            },
        )


def reservation(effect: Effect) -> int:
    """A spending effect's budget slice. The core always funds one (or fails the run)."""
    if effect.budget_usd_micros is None:
        raise ValueError(f"{effect.command.kind} {effect.id} carries no budget reservation")
    return effect.budget_usd_micros


@dataclass
class Purse:
    """One effect's reservation, shared by the invocations it makes in turn. Each gets
    what the earlier ones left, so together they never exceed the reservation."""

    reservation: int
    spent: Spend = NO_SPEND

    @property
    def left(self) -> int:
        return max(1, self.reservation - self.spent.usd_micros)

    def pay(self, spend: Spend) -> None:
        self.spent = self.spent + spend

    def charge[E: AnyEvent](self, failure: E) -> E:
        """A failure event that also carries what this effect spent before it failed."""
        if isinstance(failure, AgentFailed | AgentRefused):
            return failure.model_copy(update={"spend": self.spent + failure.spend})  # type: ignore[return-value]
        return failure
