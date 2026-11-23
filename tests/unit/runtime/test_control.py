"""Actions on a journaled run (``msa.runtime.control``), and the ``Deferred`` ledger.

A run that a Temporal workflow drives must not be journaled from outside: the workflow
journals what it folds, and a parked workflow would never notice a write behind its back.
``Deferred`` lets the same actions run against such a run: the core validates the event
exactly as before, nothing is written, and the event is held to be sent as a signal.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from msa.core.events import ReleaseApproved, ReleaseRejected
from msa.core.ids import RunId
from msa.runtime import control
from msa.runtime.clock import LogicalClock
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import Profile, SimulationConfig
from tests.support.driver import Driver
from tests.support.factory import limits

RUN = RunId("run-1")
YEARS = limits(wall_clock=timedelta(days=3650))


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[SqliteJournal]:
    """A run parked on its release approval."""
    d = Driver(YEARS)
    d.to_review()
    d.review()
    d.verify_release()
    environment = SimulationConfig(seed=0, profile=Profile.HAPPY).model_dump(mode="json")
    with SqliteJournal(tmp_path / "runs.db") as opened:
        opened.create(environment, d.events[0], d.steps[0].state)  # type: ignore[arg-type]
        for event, step in zip(d.events[1:], d.steps[1:], strict=True):
            opened.append(event, step.state, snapshot=False)
        yield opened


def test_a_deferred_action_is_validated_and_held_but_never_journaled(
    journal: SqliteJournal,
) -> None:
    before = journal.load(RUN)
    deferred = control.Deferred(journal)
    done = control.decide(deferred, RUN, LogicalClock(), by="ada", decision="approve")
    assert isinstance(done.event, ReleaseApproved)
    assert done.event.by == "ada"
    assert deferred.events == [done.event]
    assert done.state.approved_by == "ada"  # what the workflow will reach once it folds it
    assert journal.load(RUN) == before  # the journal is untouched: the workflow writes it


def test_a_deferred_action_the_core_refuses_holds_nothing(journal: SqliteJournal) -> None:
    deferred = control.Deferred(journal)
    with pytest.raises(control.NotWaiting, match="not waiting for await_unblock"):
        control.unblock(deferred, RUN, LogicalClock(), by="ada")
    with pytest.raises(ValueError, match="no agent may approve"):  # refused as it is built
        control.decide(deferred, RUN, LogicalClock(), by="agent:coder", decision="approve")
    assert deferred.events == []


def test_the_same_action_on_the_journal_is_written(journal: SqliteJournal) -> None:
    done = control.decide(
        journal, RUN, LogicalClock(), by="ada", decision="reject", feedback="not yet"
    )
    assert isinstance(done.event, ReleaseRejected)
    assert journal.load(RUN) == done.state
