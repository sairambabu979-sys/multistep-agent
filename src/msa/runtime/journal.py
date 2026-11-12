"""The journal: an append-only, per-run event log in SQLite.

Guarantees:

* **Durability.** WAL mode with ``synchronous=FULL``: once ``append`` returns, the event
  survives a process kill and a power loss.
* **Single writer per run.** ``append`` is a compare-and-set on the run's head sequence.
  A second writer (another interpreter, or ``msa cancel``) gets ``JournalConflict``
  instead of silently interleaving events.
* **Integrity on load.** Sequence numbers must be contiguous and end at the recorded head.
  Anything else is ``JournalCorrupt``, never a quietly truncated history.
* **Snapshots are caches.** Loading uses the newest compatible snapshot plus the events
  after it. ``verify`` re-folds the whole history and checks every snapshot against it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

from msa.core import ReplayError, RunState, fold, replay
from msa.core.events import AnyEvent, EventBase, RunStarted
from msa.core.ids import RunId
from msa.runtime.codec import (
    CURRENT_EVENT_VERSION,
    STATE_VERSION,
    CodecError,
    decode_event,
    decode_state,
    encode_event,
    encode_state,
    event_kind,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE runs (
    run_id      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    environment TEXT NOT NULL,           -- JSON: how to rebuild executors on resume
    head_seq    INTEGER NOT NULL,
    phase       TEXT NOT NULL,
    outcome     TEXT                     -- JSON, NULL while the run is live
) WITHOUT ROWID;

CREATE TABLE events (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    kind        TEXT NOT NULL,
    v           INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;

CREATE TABLE snapshots (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    seq         INTEGER NOT NULL,
    v           INTEGER NOT NULL,
    state       TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;
"""


class JournalError(Exception):
    pass


class UnknownRun(JournalError):
    pass


class DuplicateRun(JournalError):
    pass


class JournalConflict(JournalError):
    """Another writer appended to this run first."""


class JournalCorrupt(JournalError):
    pass


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: RunId
    created_at: datetime
    head_seq: int
    phase: str
    outcome: dict[str, Any] | None
    environment: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Verification:
    events: int
    snapshots_checked: int
    divergences: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.divergences


class Journal(Protocol):
    def create(self, environment: dict[str, Any], first: RunStarted, state: RunState) -> None: ...

    def append(self, event: EventBase, state: RunState, *, snapshot: bool) -> None: ...

    def load(self, run_id: RunId) -> RunState: ...

    def environment(self, run_id: RunId) -> dict[str, Any]: ...


class SqliteJournal:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None)  # explicit transactions
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except BaseException:
            self._db.close()  # never leak the connection when the journal is unusable
            raise

    # ---------- lifecycle

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.close()

    def _migrate(self) -> None:
        (version,) = self._db.execute("PRAGMA user_version").fetchone()
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise JournalCorrupt(
                f"journal schema v{version} is not supported (this msa uses v{SCHEMA_VERSION})"
            )
        with self._transaction():
            # Not executescript(): it commits implicitly, which would break atomicity.
            for statement in filter(str.strip, _SCHEMA.split(";")):
                self._db.execute(statement)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _transaction(self) -> _Transaction:
        return _Transaction(self._db)

    # ---------- writes

    def create(self, environment: dict[str, Any], first: RunStarted, state: RunState) -> None:
        with self._transaction():
            try:
                self._db.execute(
                    "INSERT INTO runs (run_id, created_at, environment, head_seq, phase) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        state.run_id,
                        _now(),
                        json.dumps(environment, sort_keys=True),
                        state.seq,
                        state.phase,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateRun(f"run {state.run_id} already exists") from exc
            self._insert_event(first, state)

    def append(self, event: EventBase, state: RunState, *, snapshot: bool) -> None:
        """Durably record ``event``, which produced ``state`` (so ``state.seq`` is its seq)."""
        outcome = state.outcome.model_dump_json() if state.outcome else None
        with self._transaction():
            moved = self._db.execute(
                "UPDATE runs SET head_seq = ?, phase = ?, outcome = ? "
                "WHERE run_id = ? AND head_seq = ?",
                (state.seq, state.phase, outcome, state.run_id, state.seq - 1),
            ).rowcount
            if moved != 1:
                head = self._head(state.run_id)
                raise JournalConflict(
                    f"run {state.run_id}: expected head {state.seq - 1}, found {head}"
                )
            self._insert_event(event, state)
            if snapshot:
                self._db.execute(
                    "INSERT INTO snapshots (run_id, seq, v, state) VALUES (?, ?, ?, ?)",
                    (state.run_id, state.seq, STATE_VERSION, encode_state(state)),
                )

    def _insert_event(self, event: EventBase, state: RunState) -> None:
        self._db.execute(
            "INSERT INTO events (run_id, seq, kind, v, payload, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                state.run_id,
                state.seq,
                event_kind(event),
                CURRENT_EVENT_VERSION,
                encode_event(event),
                _now(),
            ),
        )

    # ---------- reads

    def _head(self, run_id: RunId) -> int:
        row = self._db.execute("SELECT head_seq FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownRun(f"no run {run_id}")
        return int(row[0])

    def environment(self, run_id: RunId) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT environment FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise UnknownRun(f"no run {run_id}")
        environment: dict[str, Any] = json.loads(row[0])
        return environment

    def events(self, run_id: RunId, *, after: int = 0, upto: int | None = None) -> list[AnyEvent]:
        head = self._head(run_id)
        last = head if upto is None else min(upto, head)
        rows = self._db.execute(
            "SELECT seq, kind, v, payload FROM events "
            "WHERE run_id = ? AND seq > ? AND seq <= ? ORDER BY seq",
            (run_id, after, last),
        ).fetchall()
        expected = list(range(after + 1, last + 1))
        if [r[0] for r in rows] != expected:
            raise JournalCorrupt(f"run {run_id}: events {after + 1}..{last} are not contiguous")
        try:
            return [decode_event(kind, v, payload) for _, kind, v, payload in rows]
        except (CodecError, ValueError) as exc:
            raise JournalCorrupt(f"run {run_id}: {exc}") from exc

    def snapshots(self, run_id: RunId) -> Iterator[tuple[int, RunState]]:
        rows = self._db.execute(
            "SELECT seq, state FROM snapshots WHERE run_id = ? AND v = ? ORDER BY seq",
            (run_id, STATE_VERSION),
        )
        for seq, payload in rows:
            yield int(seq), decode_state(payload)

    def load(self, run_id: RunId, *, upto: int | None = None) -> RunState:
        """The state after event ``upto`` (default: the head), via the nearest snapshot."""
        head = self._head(run_id)
        target = head if upto is None else min(upto, head)
        if target < 1:
            raise JournalError("sequence numbers start at 1")
        row = self._db.execute(
            "SELECT seq, state FROM snapshots WHERE run_id = ? AND v = ? AND seq <= ? "
            "ORDER BY seq DESC LIMIT 1",
            (run_id, STATE_VERSION, target),
        ).fetchone()
        try:
            if row is None:
                state = replay(self.events(run_id, upto=target))
            else:
                base = decode_state(row[1])
                if base.seq != row[0]:
                    raise JournalCorrupt(
                        f"run {run_id}: snapshot row {row[0]} holds the state for seq {base.seq}"
                    )
                state = fold(base, self.events(run_id, after=int(row[0]), upto=target))
        except ReplayError as exc:
            raise JournalCorrupt(f"run {run_id}: {exc}") from exc
        return state

    def verify(self, run_id: RunId) -> Verification:
        """Re-fold the entire history from scratch and check every snapshot against it."""
        events = self.events(run_id)
        snapshots = dict(self.snapshots(run_id))
        divergences: list[str] = []
        try:
            state = replay(events[:1])
            for event in events[1:]:
                state = fold(state, [event])
                cached = snapshots.get(state.seq)
                if cached is not None and cached != state:
                    divergences.append(f"snapshot at seq {state.seq} differs from replay")
        except ReplayError as exc:
            divergences.append(str(exc))
        return Verification(len(events), len(snapshots), tuple(divergences))

    def runs(self) -> list[RunSummary]:
        rows = self._db.execute(
            "SELECT run_id, created_at, head_seq, phase, outcome, environment FROM runs "
            "ORDER BY created_at DESC, run_id"
        ).fetchall()
        return [
            RunSummary(
                run_id=RunId(run_id),
                created_at=datetime.fromisoformat(created),
                head_seq=int(head),
                phase=phase,
                outcome=json.loads(outcome) if outcome else None,
                environment=json.loads(environment),
            )
            for run_id, created, head, phase, outcome, environment in rows
        ]


class _Transaction:
    """``BEGIN IMMEDIATE`` takes the write lock up front, so a conflicting writer fails at
    the start of its transaction rather than deadlocking halfway through."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def __enter__(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _value: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self._db.execute("ROLLBACK" if exc_type else "COMMIT")


def _now() -> str:
    return datetime.now(UTC).isoformat()
