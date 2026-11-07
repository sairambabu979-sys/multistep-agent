"""INTAKE (PLAN §2): resolve the preferences and snapshot the base tree.

``PrepareWorkspace`` completes with ``IntakeCompleted(base_tree)``, or the run fails with
``IntakeRejected(reason)``. Two things reject:

- **The preferences do not resolve**: a layer file is invalid, a merged entry is
  incomplete, or two hard preferences contradict each other where both apply (I-16). The
  reason is the resolver's message, which names both preferences, the layers that defined
  them, and how to fix it.
- **There is no base tree**: the root is not a git repository with a commit to start from.

The base tree is ``HEAD^{tree}``: evidence is bound to tree hashes (M1), so a run starts
from a committed state.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from msa.core.effects import Effect, PrepareWorkspace
from msa.core.events import AnyEvent, IntakeCompleted, IntakeRejected
from msa.core.ids import TREE_HASH_PATTERN, TreeHash
from msa.skills.preferences import PreferenceError, Resolved, store
from msa.tools.sandbox import LocalSandbox

_TREE = re.compile(TREE_HASH_PATTERN)


class IntakeExecutor:
    def __init__(
        self,
        root: Path,
        sandbox: LocalSandbox,
        *,
        overrides: Sequence[str] = (),
        load: Callable[[Path, Sequence[str]], Resolved] | None = None,
    ) -> None:
        self.root = root
        self.sandbox = sandbox
        self.overrides = tuple(overrides)
        self._load = load or store.load
        self.resolved: Resolved | None = None  # the run's preferences, once intake succeeds

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        if not isinstance(effect.command, PrepareWorkspace):
            raise TypeError(f"intake cannot execute {effect.command.kind}")
        try:
            resolved = self._load(self.root, self.overrides)
        except PreferenceError as exc:
            return IntakeRejected(at=now, effect_id=effect.id, reason=f"preferences: {exc}")
        tree = await self._base_tree()
        if tree is None:
            reason = "the root is not a git repository with a commit to start from"
            return IntakeRejected(at=now, effect_id=effect.id, reason=reason)
        self.resolved = resolved
        return IntakeCompleted(at=now, effect_id=effect.id, base_tree=TreeHash(tree))

    async def _base_tree(self) -> str | None:
        git = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
        done = await self.sandbox.execute(
            [*git, "rev-parse", "--verify", "--quiet", "HEAD^{tree}"], shown="git", merge=False
        )
        tree = done.stdout.strip()
        return tree if done.exit_code == 0 and _TREE.match(tree) else None
