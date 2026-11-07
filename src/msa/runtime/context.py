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

from msa.config import MsaConfig, Role
from msa.core.effects import Effect
from msa.core.events import AgentFailed, AgentRefused, AnyEvent
from msa.core.ids import RunId, TreeHash
from msa.llm.gateway import (
    Gateway,
    Invocation,
    InvocationFailed,
    InvocationRefused,
    InvocationResult,
    Turn,
)
from msa.runtime.artifacts import RunFiles
from msa.runtime.worktree import RunWorktree
from msa.skills.preferences import Resolved
from msa.skills.preferences.render import render
from msa.tools.audit import Violation, log_violation
from msa.tools.capabilities import WritePolicy
from msa.tools.paths import Root
from msa.tools.sandbox import LocalSandbox
from msa.tools.workspace import Workspace


@dataclass
class RunContext:
    run_id: RunId
    task: str
    config: MsaConfig
    gateway: Gateway
    worktree: RunWorktree
    files: RunFiles
    preferences: Callable[[], Resolved]  # resolved at INTAKE, or again on resume
    on_violation: Callable[[Violation], None] = log_violation
    _block: dict[TreeHash, str] = field(default_factory=dict)

    # ---------- the tool layer, bound to the worktree for one role

    def root(self) -> Root:
        return Root.at(self.worktree.path)

    def sandbox(self, role: Role) -> LocalSandbox:
        return LocalSandbox(
            self.root(), self.config.sandbox, role=role.value, on_violation=self.on_violation
        )

    def workspace(self, role: Role) -> Workspace:
        root = self.root()
        policy = WritePolicy(role, self.config.capabilities, root.case_insensitive)
        return Workspace(root, policy, self.config.sandbox, on_violation=self.on_violation)

    # ---------- prompt inputs that must be byte-stable for the run

    async def preference_block(self) -> str:
        """The preferences that apply to the base tree's files, rendered once per run."""
        base = await self.worktree.base_tree()
        if base not in self._block:
            files = await self.worktree.files(base)
            self._block[base] = render(self.preferences().select(files))
        return self._block[base]

    # ---------- invoking an agent

    def has_paid_turns(self, effect: Effect) -> bool:
        """Whether an earlier execution of ``effect`` already paid for a turn, so this one
        is a resumption that will replay it (and must find the worktree as it left it)."""
        store = self.gateway.store
        return store is not None and store.get(effect.id, 0) is not None

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
