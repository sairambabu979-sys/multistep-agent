"""Driving runs inside the service: started from the API, or resumed after an answer.

At most one driver per run in this process; a second ``drive`` while one is going does
nothing, and the run's journal (compare-and-set appends) guards against drivers in other
processes, such as ``msa resume``. A driver that fails keeps its error for the API to
report: the run itself stays journaled and resumable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from msa.core import RunState
from msa.core.ids import Digest, RunId
from msa.core.values import Limits
from msa.runtime.interpreter import Interpreter
from msa.runtime.journal import SqliteJournal

# (run, its stored environment, the journal) -> an interpreter for it, open while in use
type Factory = Callable[
    [RunId, dict[str, Any], SqliteJournal], AbstractAsyncContextManager[Interpreter]
]


class Runner:
    def __init__(self, journal_path: Path, factory: Factory) -> None:
        self.journal_path = journal_path
        self.factory = factory
        self._tasks: dict[RunId, asyncio.Task[RunState]] = {}
        self.errors: dict[RunId, str] = {}

    def driving(self, run_id: RunId) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def drive(self, run_id: RunId) -> bool:
        """Resume ``run_id`` in the background. False if it is already being driven."""
        if self.driving(run_id):
            return False
        self._launch(run_id, self._resume(run_id))
        return True

    def start(
        self,
        run_id: RunId,
        *,
        task_digest: Digest,
        limits: Limits,
        environment: dict[str, Any],
    ) -> None:
        self._launch(run_id, self._start(run_id, task_digest, limits, environment))

    def _launch(self, run_id: RunId, work: Any) -> None:
        self.errors.pop(run_id, None)
        task: asyncio.Task[RunState] = asyncio.create_task(work, name=f"drive {run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda t: self._finished(run_id, t))

    def _finished(self, run_id: RunId, task: asyncio.Task[RunState]) -> None:
        if task.cancelled():
            return
        if (error := task.exception()) is not None:
            self.errors[run_id] = f"{type(error).__name__}: {error}"

    async def _resume(self, run_id: RunId) -> RunState:
        with SqliteJournal(self.journal_path) as journal:
            environment = journal.environment(run_id)
            async with self.factory(run_id, environment, journal) as interpreter:
                return await interpreter.resume(run_id)

    async def _start(
        self, run_id: RunId, task_digest: Digest, limits: Limits, environment: dict[str, Any]
    ) -> RunState:
        with SqliteJournal(self.journal_path) as journal:
            async with self.factory(run_id, environment, journal) as interpreter:
                return await interpreter.start(
                    run_id=run_id, task_digest=task_digest, limits=limits, environment=environment
                )

    async def settle(self) -> None:
        """Wait for every driver to stop (tests, and shutdown)."""
        while pending := [t for t in self._tasks.values() if not t.done()]:
            await asyncio.wait(pending)

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
