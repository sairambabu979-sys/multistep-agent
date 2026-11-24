"""Checks on a tree of files, run the way the system runs them: in the sandbox.

The hidden tests import code an agent wrote, so they run like any agent command: in a
sandbox rooted at the tree, without network where the kernel allows it (and refused where
``isolate_network = "always"`` and it does not), under the sandbox's time and output limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from msa.config import SandboxConfig, VerifyCommand
from msa.evals.corpus import HIDDEN_DIR, Task, add_hidden
from msa.runtime.integration import isolation
from msa.runtime.testing import SuiteUnrunnable, run_pytest
from msa.runtime.verify import check_argv, passed
from msa.tools.paths import Root
from msa.tools.sandbox import LocalSandbox

# STATIC_ANALYSIS's checks, with its own command lines (msa.toml [verify] runs these).
GATE: tuple[VerifyCommand, ...] = ("ruff_check", "ruff_format_check", "mypy")


@dataclass(frozen=True, slots=True)
class Score:
    """One pytest session's result."""

    passed: int = 0
    failed: tuple[str, ...] = ()  # node ids
    error: str | None = None  # the session could not run at all
    isolated: bool = False  # ran without network
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and self.passed > 0 and not self.failed


@dataclass(frozen=True, slots=True)
class Gate:
    failures: dict[str, str] = field(default_factory=dict)  # command -> its output

    @property
    def ok(self) -> bool:
        return not self.failures


def sandbox(tree: Path, config: SandboxConfig) -> LocalSandbox:
    return LocalSandbox(Root.at(tree), config, role="eval")


async def wrapper(box: LocalSandbox) -> tuple[str, ...]:
    """The no-network wrapper for a pytest session, per ``isolate_network``."""
    mode = box.config.isolate_network
    if mode == "never":
        return ()
    found = await isolation(box)
    if found is None and mode == "always":
        raise SuiteUnrunnable(
            "[sandbox].isolate_network = always, but this machine cannot create a network "
            "namespace (unshare -rn): the tests cannot run here"
        )
    return found or ()


async def pytest_session(
    tree: Path,
    config: SandboxConfig,
    targets: list[str],
    junit: Path,
    *,
    ignore: tuple[str, ...] = (),
) -> Score:
    box = sandbox(tree, config)
    try:
        wrap = await wrapper(box)
        run = await run_pytest(box, targets, junit, ignore=ignore, wrapper=wrap)
    except SuiteUnrunnable as exc:
        return Score(error=str(exc))
    return Score(
        passed=sum(1 for c in run.cases if c.passed),
        failed=tuple(c.nodeid for c in run.failed),
        isolated=bool(wrap),
        output=run.done.render(),
    )


async def visible_tests(tree: Path, config: SandboxConfig, junit: Path) -> Score:
    """The repository's own tests, hidden tests (if already placed) left out."""
    return await pytest_session(tree, config, [], junit, ignore=(HIDDEN_DIR,))


async def hidden_tests(task: Task, tree: Path, config: SandboxConfig, junit: Path) -> Score:
    """Place the task's hidden tests in ``tree`` and run only them."""
    return await pytest_session(tree, config, add_hidden(task, tree), junit)


async def gate(tree: Path, config: SandboxConfig) -> Gate:
    """The static checks STATIC_ANALYSIS runs, over the whole tree."""
    box = sandbox(tree, config)
    failures: dict[str, str] = {}
    for command in GATE:
        argv = check_argv(command, ())
        done = await box.execute([*config.launcher, *argv], shown=" ".join(argv))
        if done.timed_out or not passed(command, done):
            failures[command] = done.render()
    return Gate(failures)
