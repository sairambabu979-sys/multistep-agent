"""I-15 (PLAN §6.2): tests that passed for an old tree can never finalize the change.

A whole journaled run through the interpreter. Every effect is simulated except the test
suite, whose executor reports a pass for the *base* tree, not the head it was asked about:
the evidence is stale. The core rejects it, the interpreter fails the run loudly (an
executor answering with stale evidence is a bug, not a result), and FINALIZING is never
reached. The same run with honest evidence completes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from msa.core import RunState
from msa.core.effects import Effect, RunTestSuite
from msa.core.events import AnyEvent, TestsPassed
from msa.core.ids import RunId
from msa.core.values import OutcomeReason, Phase
from msa.observability.report import timeline
from msa.runtime.clock import LogicalClock
from msa.runtime.executors import Router
from msa.runtime.interpreter import Interpreter
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import BASE, Profile, SimulatedEnvironment, SimulationConfig, digest
from tests.support.factory import limits

HAPPY = SimulationConfig(seed=0, profile=Profile.HAPPY)


class StaleTests:
    """Reports a pass, but for the base tree."""

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        assert isinstance(effect.command, RunTestSuite)
        return TestsPassed(at=now, effect_id=effect.id, tree=BASE)


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[SqliteJournal]:
    with SqliteJournal(tmp_path / "runs.db") as opened:
        yield opened


def run(journal: SqliteJournal, *, stale: bool) -> RunState:
    happy = SimulatedEnvironment(HAPPY)
    executor = Router({"run_test_suite": StaleTests()}, happy) if stale else happy
    interpreter = Interpreter(journal, executor, clock=LogicalClock())
    return asyncio.run(
        interpreter.start(
            run_id=RunId("run-1"),
            task_digest=digest("t"),
            limits=limits(),
            environment=HAPPY.model_dump(mode="json"),
        )
    )


def test_i15_stale_test_evidence_is_rejected_and_never_finalizes(journal: SqliteJournal) -> None:
    final = run(journal, stale=True)
    assert final.phase is Phase.FAILED
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.AGENT_FAILED
    assert "executor answer rejected" in final.outcome.detail
    assert f"test result is for tree {BASE}" in final.outcome.detail
    lines = timeline(journal.events(RunId("run-1")))
    assert not any("FINALIZING" in line for line in lines)
    assert lines[-1].strip().split("  ")[1] == "TESTING -> FAILED"
    assert final.evidence.tested is None  # the stale pass was never recorded as evidence


def test_the_same_run_with_current_evidence_completes(journal: SqliteJournal) -> None:
    final = run(journal, stale=False)
    assert final.phase is Phase.DONE
    assert final.evidence.tested == final.head_tree
