"""Crash recovery under real SIGKILLs (PLAN §6.2 "Chaos", the M2 exit criterion).

Each trial runs a simulated agent run in a forked child that kills itself with SIGKILL at
a chosen boundary. SIGKILL gives the process no chance to clean up, flush, or roll back, so
it behaves like a real crash. Each run is crashed one to three times in a row and then
resumed to completion. Every trial must satisfy:

1. **Same outcome.** The recovered run ends in *exactly* the state an uninterrupted run with
   the same seed ends in, including timestamps, ledger, effect ids, and findings.
2. **No duplicate work.** No effect whose completion was journaled before a crash executes
   again afterwards. Only effects that were in flight may repeat.
3. **Intact journal.** The journal re-folds from scratch and matches every snapshot.

The number of trials is ``MSA_CHAOS_TRIALS`` (25 by default; the nightly job runs 100).
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import random
import signal
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from msa.core import RunState
from msa.core.effects import Effect
from msa.core.events import AnyEvent, Completion
from msa.core.ids import EffectId, RunId
from msa.core.table import Step
from msa.runtime.clock import LogicalClock
from msa.runtime.interpreter import Interpreter, Observer
from msa.runtime.journal import SqliteJournal, UnknownRun
from msa.runtime.simulation import SimulatedEnvironment, SimulationConfig, digest
from tests.support.factory import limits

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork() to crash a child"),
]

RUN = RunId("chaos")
POINTS = ("effect_started", "before_commit", "after_commit")
TRIALS = int(os.environ.get("MSA_CHAOS_TRIALS", "25"))
MIN_EVENTS = 12  # only crash runs long enough to have interesting kill points
LIMITS = limits(wall_clock=LogicalClock().tick * 10_000)


class _Probe(Observer):
    """Counts boundary crossings, and SIGKILLs the process at the chosen one."""

    def __init__(self, kill: tuple[str, int] | None = None) -> None:
        self.kill = kill
        self.counts: Counter[str] = Counter()

    def _cross(self, point: str) -> None:
        self.counts[point] += 1
        if self.kill == (point, self.counts[point]):
            os.kill(os.getpid(), signal.SIGKILL)

    def effect_started(self, effect: Effect) -> None:
        self._cross("effect_started")

    def before_commit(self, event: AnyEvent, step: Step) -> None:
        self._cross("before_commit")

    def after_commit(self, event: AnyEvent, step: Step) -> None:
        self._cross("after_commit")


def _record_execution(log: Path, effect: Effect) -> None:
    # O_APPEND writes reach the page cache immediately, so they survive SIGKILL.
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, f"{effect.id}\n".encode())
    finally:
        os.close(fd)


def _drive(db: Path, log: Path, seed: int, probe: _Probe) -> RunState:
    config = SimulationConfig(seed=seed)
    environment = SimulatedEnvironment(config, on_execute=lambda e: _record_execution(log, e))
    with SqliteJournal(db) as journal:
        interpreter = Interpreter(
            journal, environment, clock=LogicalClock(), observer=probe, snapshot_every=3
        )
        try:
            journal.environment(RUN)
        except UnknownRun:  # never created (or crashed before RunStarted was durable)
            coroutine = interpreter.start(
                run_id=RUN,
                task_digest=digest("chaos"),
                limits=LIMITS,
                environment=config.model_dump(mode="json"),
            )
        else:
            coroutine = interpreter.resume(RUN)
        return asyncio.run(coroutine)


def _child(db: Path, log: Path, seed: int, kill: tuple[str, int]) -> None:
    _drive(db, log, seed, _Probe(kill))


def _crash(db: Path, log: Path, seed: int, kill: tuple[str, int]) -> int:
    process = multiprocessing.get_context("fork").Process(target=_child, args=(db, log, seed, kill))
    process.start()
    process.join(timeout=60)
    assert process.exitcode is not None, "child hung"
    return process.exitcode


def _completed(db: Path) -> set[EffectId]:
    if not db.exists():
        return set()
    with SqliteJournal(db) as journal:
        try:
            events = journal.events(RUN)
        except UnknownRun:
            return set()
    return {e.effect_id for e in events if isinstance(e, Completion)}


def _executions(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


@dataclass(frozen=True)
class Baseline:
    seed: int
    state: RunState
    counts: Counter[str]


def _baseline(tmp: Path, start_seed: int) -> Baseline:
    """The first seed at or after ``start_seed`` whose uninterrupted run is long enough."""
    for seed in range(start_seed, start_seed + 1_000):
        probe = _Probe()
        db = tmp / f"baseline-{seed}.db"
        state = _drive(db, tmp / f"baseline-{seed}.log", seed, probe)
        if state.seq >= MIN_EVENTS:
            return Baseline(seed, state, probe.counts)
    raise AssertionError("no seed produced a long enough run")  # pragma: no cover


@pytest.mark.parametrize("trial", range(TRIALS))
def test_a_crashed_run_resumes_to_the_uninterrupted_outcome(trial: int, tmp_path: Path) -> None:
    rng = random.Random(trial)
    base = _baseline(tmp_path, start_seed=trial * 1_000)
    db, log = tmp_path / "crashed.db", tmp_path / "crashed.log"

    # First crash: within the uninterrupted run's own counts, so it is guaranteed to fire.
    point = rng.choice(POINTS)
    kills = [(point, rng.randint(1, base.counts[point]))]
    # Further crashes hit the resumed processes early in their (shorter) remaining work.
    kills += [(rng.choice(POINTS), rng.randint(1, 4)) for _ in range(rng.randint(0, 2))]

    crashes = 0
    for kill in kills:
        completed, executed = _completed(db), len(_executions(log))
        exitcode = _crash(db, log, base.seed, kill)
        rerun = set(_executions(log)[executed:]) & completed
        assert not rerun, f"journaled effects executed again after resume: {sorted(rerun)}"
        if exitcode == 0:
            break  # the run finished before reaching this kill point
        assert exitcode == -signal.SIGKILL, f"child exited with {exitcode}"
        crashes += 1
    assert crashes >= 1

    completed, executed = _completed(db), len(_executions(log))
    final = _drive(db, log, base.seed, _Probe())
    assert not set(_executions(log)[executed:]) & completed

    assert final == base.state
    with SqliteJournal(db) as journal:
        assert journal.load(RUN) == base.state
        verification = journal.verify(RUN)
    assert verification.ok, verification.divergences
