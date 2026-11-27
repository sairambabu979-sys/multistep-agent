"""Independent commands, run at once: at most ``limit`` at a time, results in input order.

Static analysis and the Tester's lint run several checks that only read the worktree
(ruff, ruff format, mypy, ripgrep). Run one after another, the stage takes as long as all of
them together. Run together, as long as the slowest. Each keeps its own timeout, since each
is still its own sandbox command.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence


async def gather_bounded[T](jobs: Sequence[Callable[[], Awaitable[T]]], limit: int) -> list[T]:
    """Run every job, at most ``limit`` at once, and return their results in ``jobs``'
    order, whatever order they finish in. Every job runs to its end even if another raises
    (a cancelled check would leave its process behind). Then the first exception, in
    ``jobs``' order, is raised, so the same failure is reported however the timing falls."""
    if limit < 1:
        raise ValueError(f"limit must be at least 1, not {limit}")
    gate = asyncio.Semaphore(limit)

    async def one(job: Callable[[], Awaitable[T]]) -> T:
        async with gate:
            return await job()

    outcomes = await asyncio.gather(*(one(job) for job in jobs), return_exceptions=True)
    results: list[T] = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
        results.append(outcome)
    return results
