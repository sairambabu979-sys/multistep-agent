from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from msa.core import RunState
from msa.core.events import CancelRequested, IntakeCompleted
from msa.core.ids import EffectId, RunId
from msa.runtime.clock import LogicalClock
from msa.runtime.codec import CodecError
from msa.runtime.interpreter import Interpreter
from msa.runtime.journal import (
    DuplicateRun,
    JournalConflict,
    JournalCorrupt,
    JournalError,
    SqliteJournal,
    UnknownRun,
)
from msa.runtime.simulation import BASE, Profile, SimulatedEnvironment, SimulationConfig, digest
from tests.support.driver import Driver
from tests.support.factory import limits, run_started

RUN = RunId("run-1")


def simulate(journal: SqliteJournal, *, seed: int = 0, snapshot_every: int = 4) -> RunState:
    config = SimulationConfig(seed=seed, profile=Profile.HAPPY)
    interpreter = Interpreter(
        journal, SimulatedEnvironment(config), clock=LogicalClock(), snapshot_every=snapshot_every
    )
    return asyncio.run(
        interpreter.start(
            run_id=RUN,
            task_digest=digest("t"),
            limits=limits(),
            environment=config.model_dump(mode="json"),
        )
    )


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[SqliteJournal]:
    with SqliteJournal(tmp_path / "nested" / "runs.db") as opened:
        yield opened


def test_a_journaled_run_loads_back_to_its_exact_state(journal: SqliteJournal) -> None:
    final = simulate(journal)
    assert journal.load(RUN) == final
    assert journal.verify(RUN).ok


def test_the_journal_is_durable_wal(journal: SqliteJournal) -> None:
    assert journal._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert journal._db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_time_travel_loads_any_past_state(journal: SqliteJournal) -> None:
    final = simulate(journal, snapshot_every=3)
    events = journal.events(RUN)
    for seq in range(1, final.seq + 1):
        assert journal.load(RUN, upto=seq).seq == seq
    assert journal.load(RUN, upto=10_000) == final
    assert len(events) == final.seq
    with pytest.raises(JournalError, match="start at 1"):
        journal.load(RUN, upto=0)


def test_snapshots_are_written_periodically_and_at_the_end(journal: SqliteJournal) -> None:
    final = simulate(journal, snapshot_every=4)
    expected = sorted({*range(4, final.seq + 1, 4), final.seq})
    assert [seq for seq, _ in journal.snapshots(RUN)] == expected


def test_a_run_id_can_only_be_created_once(journal: SqliteJournal) -> None:
    simulate(journal)
    with pytest.raises(DuplicateRun):
        simulate(journal)


def test_a_second_writer_gets_a_conflict_not_an_interleaving(journal: SqliteJournal) -> None:
    d = Driver()
    journal.create({"kind": "test"}, run_started(), d.state)
    step = d.intake()
    journal.append(d.events[-1], step.state, snapshot=False)
    # A second writer that loaded seq 1 and computed its own seq 2 loses the race.
    rival = Driver()
    rival_step = rival.send(CancelRequested(at=rival.tick(), reason="rival"))
    with pytest.raises(JournalConflict, match="expected head 1, found 2"):
        journal.append(rival.events[-1], rival_step.state, snapshot=False)


def test_unknown_runs_are_reported(journal: SqliteJournal) -> None:
    for read in (journal.load, journal.events, journal.environment):
        with pytest.raises(UnknownRun):
            read(RunId("nope"))


def _drop_snapshots(journal: SqliteJournal) -> None:
    """``load`` trusts snapshots (they are a cache). Corruption *behind* a snapshot is found
    by ``verify``; dropping them makes ``load`` read the damaged history itself."""
    journal._db.execute("DELETE FROM snapshots")


def test_load_trusts_snapshots_and_verify_audits_behind_them(journal: SqliteJournal) -> None:
    final = simulate(journal)
    journal._db.execute("UPDATE events SET payload = '{}' WHERE run_id = ? AND seq = 2", (RUN,))
    assert journal.load(RUN) == final  # served from the terminal snapshot
    with pytest.raises(JournalCorrupt):
        journal.verify(RUN)  # reads every event, so the damage surfaces


def test_a_gap_in_the_history_is_corruption_not_truncation(journal: SqliteJournal) -> None:
    simulate(journal)
    _drop_snapshots(journal)
    journal._db.execute("DELETE FROM events WHERE run_id = ? AND seq = 3", (RUN,))
    with pytest.raises(JournalCorrupt, match="not contiguous"):
        journal.load(RUN)


def test_an_undecodable_payload_is_corruption(journal: SqliteJournal) -> None:
    simulate(journal)
    _drop_snapshots(journal)
    journal._db.execute("UPDATE events SET payload = '{}' WHERE run_id = ? AND seq = 2", (RUN,))
    with pytest.raises(JournalCorrupt):
        journal.load(RUN)


def test_an_event_from_a_newer_msa_is_corruption(journal: SqliteJournal) -> None:
    simulate(journal)
    _drop_snapshots(journal)
    journal._db.execute("UPDATE events SET v = 99 WHERE run_id = ? AND seq = 2", (RUN,))
    with pytest.raises(JournalCorrupt, match="newer msa"):
        journal.load(RUN)


def test_a_journal_that_cannot_be_replayed_is_corruption(journal: SqliteJournal) -> None:
    simulate(journal)
    stale = IntakeCompleted(
        at=LogicalClock().epoch, effect_id=EffectId("run-1:1:0"), base_tree=BASE
    )
    journal._db.execute(
        "UPDATE events SET kind = ?, payload = ? WHERE run_id = ? AND seq = 3",
        (stale.kind, stale.model_dump_json(), RUN),
    )
    verification = journal.verify(RUN)
    assert not verification.ok
    assert "rejected on replay" in verification.divergences[0]
    _drop_snapshots(journal)
    with pytest.raises(JournalCorrupt, match="rejected on replay"):
        journal.load(RUN)


def test_verify_detects_a_snapshot_that_disagrees_with_history(journal: SqliteJournal) -> None:
    final = simulate(journal, snapshot_every=2)
    (seq, snap), *_ = journal.snapshots(RUN)
    tampered = snap.model_copy(update={"replans": 1})
    journal._db.execute(
        "UPDATE snapshots SET state = ? WHERE run_id = ? AND seq = ?",
        (tampered.model_dump_json(), RUN, seq),
    )
    result = journal.verify(RUN)
    assert result.divergences == (f"snapshot at seq {seq} differs from replay",)
    assert result.events == final.seq


def test_snapshots_of_another_state_version_are_ignored(journal: SqliteJournal) -> None:
    final = simulate(journal, snapshot_every=2)
    journal._db.execute("UPDATE snapshots SET v = 0")
    assert list(journal.snapshots(RUN)) == []
    assert journal.load(RUN) == final  # rebuilt from events alone


def test_an_unsupported_schema_version_refuses_to_open(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    SqliteJournal(db).close()
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(JournalCorrupt, match="schema v99"):
        SqliteJournal(db)


def test_reopening_an_existing_journal_keeps_its_runs(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    with SqliteJournal(db) as first:
        final = simulate(first)
    with SqliteJournal(db) as again:
        assert again.load(RUN) == final


def test_runs_are_listed_with_their_outcome_and_environment(journal: SqliteJournal) -> None:
    final = simulate(journal)
    (summary,) = journal.runs()
    assert summary.run_id == RUN
    assert summary.head_seq == final.seq
    assert summary.phase == "done"
    assert summary.outcome is not None
    assert summary.outcome["reason"] == "completed"
    assert summary.environment["kind"] == "simulation"


def test_an_in_memory_journal_works() -> None:
    with SqliteJournal(":memory:") as journal:
        final = simulate(journal)
        assert journal.load(RUN) == final


def test_only_journaled_event_types_can_be_appended(journal: SqliteJournal) -> None:
    class Rogue(CancelRequested):
        pass

    d = Driver()
    journal.create({"kind": "test"}, run_started(), d.state)
    step = d.send(CancelRequested(at=d.tick(), reason="x"))
    with pytest.raises(CodecError, match="not a journaled event type"):
        journal.append(Rogue(at=d.clock, reason="x"), step.state, snapshot=False)


def test_cancel_events_round_trip_through_the_journal(journal: SqliteJournal) -> None:
    d = Driver()
    journal.create({"kind": "test"}, run_started(), d.state)
    event = CancelRequested(at=d.tick(), reason="stop")
    step = d.send(event)
    journal.append(event, step.state, snapshot=True)
    assert journal.load(RunId("run-1")) == step.state


def test_a_snapshot_filed_under_the_wrong_seq_is_corruption(journal: SqliteJournal) -> None:
    final = simulate(journal, snapshot_every=1_000)
    journal._db.execute("UPDATE snapshots SET seq = seq - 1 WHERE run_id = ?", (RUN,))
    with pytest.raises(JournalCorrupt, match=f"holds the state for seq {final.seq}"):
        journal.load(RUN)
