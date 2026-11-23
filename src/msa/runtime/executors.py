"""Compose executors: each effect kind goes to the executor that implements it.

Real executors arrive one milestone at a time (intake in M5, the agents in M6-M8). A
``Router`` lets a run use the real ones that exist and a fallback, such as the simulator,
for the rest, so each new executor runs inside a whole, journaled run from day one.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from msa.core.effects import Effect
from msa.core.events import AnyEvent
from msa.runtime.interpreter import Executor


class Router:
    def __init__(self, routes: Mapping[str, Executor], fallback: Executor) -> None:
        self.routes = dict(routes)
        self.fallback = fallback

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        executor = self.routes.get(effect.command.kind, self.fallback)
        return await executor.execute(effect, now=now)
