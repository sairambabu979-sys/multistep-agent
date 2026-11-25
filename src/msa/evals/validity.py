"""Whether a task is fit to score anything (``msa eval check``).

A task that resolves is only evidence if it was a real task: its hidden tests must fail on
the base repository, and a solution must exist that the system's own gates would accept.
For every task to resolve, on the base repository and then with the reference laid over it:

- the static gate (ruff, ruff format, mypy, as STATIC_ANALYSIS runs them) passes on both;
- the repository's visible tests pass on both;
- the hidden tests fail on the base, at least one of them;
- the hidden tests all pass with the reference.

A trap has nothing to run: it is checked to have no reference, since there is no right
change to make.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from msa.config import SandboxConfig
from msa.evals.corpus import Task, materialize
from msa.evals.scoring import gate, hidden_tests, visible_tests


async def problems(task: Task, workdir: Path, config: SandboxConfig) -> list[str]:
    """What is wrong with ``task``; [] when it is fit to score."""
    if task.expect == "flag":
        return ["a trap has a reference solution"] if task.reference else []
    found: list[str] = []
    for name, reference in (("base", False), ("reference", True)):
        tree = materialize(task, workdir / name, reference=reference)
        checked = await gate(tree, config)
        found += [f"{name}: {command} fails" for command in checked.failures]
        visible = await visible_tests(tree, config, workdir / "junit" / f"{name}-visible.xml")
        if not visible.ok:
            found.append(
                f"{name}: the visible tests do not pass ({_why(visible.failed, visible.error)})"
            )
        hidden = await hidden_tests(task, tree, config, workdir / "junit" / f"{name}-hidden.xml")
        if hidden.error is not None:
            found.append(f"{name}: the hidden tests cannot run: {hidden.error}")
        elif reference and not hidden.ok:
            found.append(f"reference: hidden tests fail: {', '.join(hidden.failed)}")
        elif not reference and not hidden.failed:
            found.append("base: every hidden test already passes: there is nothing to do")
    return found


async def check_all(
    tasks: list[Task], workdir: Path, config: SandboxConfig, *, parallel: int = 4
) -> dict[str, list[str]]:
    """Every task's problems, ``parallel`` tasks at a time."""
    limit = asyncio.Semaphore(parallel)

    async def one(task: Task) -> tuple[str, list[str]]:
        async with limit:
            return task.id, await problems(task, workdir / task.id, config)

    return dict(await asyncio.gather(*(one(t) for t in tasks)))


def _why(failed: tuple[str, ...], error: str | None) -> str:
    return error.splitlines()[0] if error else ", ".join(failed) or "no tests ran"
