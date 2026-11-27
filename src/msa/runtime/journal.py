"""The journal: an append-only, per-run event log in SQL, on SQLite or PostgreSQL.

``SqlJournal`` holds every rule; a backend supplies the connection, its transactions and
its dialect, so both backends pass the same contract tests (``tests/unit/runtime``).
``open_journal`` picks one: a ``postgresql://`` URL, or else a SQLite file.

Guarantees, on either backend:

* **Durability.** SQLite: WAL mode with ``synchronous=FULL``. PostgreSQL:
  ``synchronous_commit = on`` for every connection. Once ``append`` returns, the event
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
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

import psycopg
from psycopg import sql

from msa.config import postgres_url_problem
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

SCHEMA_VERSION = 2  # v2: the runs table keeps the rollups /api/metrics reads (ADR-023)

# One schema for both backends. SQLite stores these tables WITHOUT ROWID (they are keyed
# by their primary key anyway); PostgreSQL has no such clause. Payloads are TEXT on both,
# so an event's bytes are identical whichever database holds them.
_TABLES = (
    """CREATE TABLE runs (
    run_id      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    environment TEXT NOT NULL,           -- JSON: how to rebuild executors on resume
    head_seq    INTEGER NOT NULL,
    phase       TEXT NOT NULL,
    outcome     TEXT,                    -- JSON, NULL while the run is live
    {rollups}
)""",
    """CREATE TABLE events (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    kind        TEXT NOT NULL,
    v           INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
)""",
    """CREATE TABLE snapshots (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    seq         INTEGER NOT NULL,
    v           INTEGER NOT NULL,
    state       TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
)""",
)


# Rollups of each run's state at its head, written with every append in the same transaction
# as the event, so they are never behind the journal. /api/metrics reads these instead of
# folding every run (ADR-023). Schema v1 journals gain them on open.
_ROLLUPS = (
    ("spent_usd_micros", "INTEGER NOT NULL DEFAULT 0"),
    ("paused_seconds", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
    ("waiting", "INTEGER NOT NULL DEFAULT 0"),  # 1 while it waits on a person
    ("revisions", "INTEGER NOT NULL DEFAULT 0"),
    ("working_seconds", "DOUBLE PRECISION NOT NULL DEFAULT 0"),  # started to now, less paused
)
ROLLUP_COLUMNS = tuple(name for name, _ in _ROLLUPS)
_SCHEMA: tuple[str, ...] = tuple(
    table.replace("{rollups}", ",\n    ".join(f"{name} {kind}" for name, kind in _ROLLUPS))
    for table in _TABLES
)


_SET_ROLLUPS = ", ".join(f"{name} = ?" for name, _ in _ROLLUPS)


def rollups(state: RunState) -> tuple[int, float, int, int, float]:
    """The run's rollups, in ``_ROLLUPS`` order."""
    working = state.updated_at - state.started_at - state.paused
    return (
        state.ledger.spent.usd_micros,
        state.paused.total_seconds(),
        int(state.waiting_since is not None),
        state.revisions,
        working.total_seconds(),
    )


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
class RunRollup:
    """What /api/metrics needs of a run, from the runs table alone."""

    run_id: RunId
    phase: str
    outcome_reason: str | None
    spent_usd_micros: int
    paused_seconds: float
    waiting: bool
    revisions: int
    working_seconds: float


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


class _Rows(Protocol):
    """What both drivers' cursors share."""

    @property
    def rowcount(self) -> int: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def __iter__(self) -> Iterator[Any]: ...


class SqlJournal(ABC):
    """The journal's rules, over any SQL database. Statements use ``?`` placeholders."""

    location: str  # where the journal is, for messages; never a password

    # ---------- what a backend supplies

    @abstractmethod
    def _run(self, sql: str, params: Sequence[object] = ()) -> _Rows:
        """Execute one statement."""

    @abstractmethod
    def _transaction(self) -> AbstractContextManager[None]:
        """A transaction that takes the write lock before it reads anything it will check."""

    @abstractmethod
    def _is_duplicate(self, error: Exception) -> bool:
        """Whether ``error`` is a primary-key violation."""

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.close()

    # ---------- writes

    def create(self, environment: dict[str, Any], first: RunStarted, state: RunState) -> None:
        with self._transaction():
            try:
                columns = ", ".join(("run_id", "created_at", "environment", "head_seq", "phase"))
                rolled = ", ".join(ROLLUP_COLUMNS)
                self._run(
                    f"INSERT INTO runs ({columns}, {rolled}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        state.run_id,
                        _now(),
                        json.dumps(environment, sort_keys=True),
                        state.seq,
                        state.phase,
                        *rollups(state),
                    ),
                )
            except Exception as exc:
                if self._is_duplicate(exc):
                    raise DuplicateRun(f"run {state.run_id} already exists") from exc
                raise
            self._insert_event(first, state)

    def append(self, event: EventBase, state: RunState, *, snapshot: bool) -> None:
        """Durably record ``event``, which produced ``state`` (so ``state.seq`` is its seq)."""
        outcome = state.outcome.model_dump_json() if state.outcome else None
        with self._transaction():
            moved = self._run(
                f"UPDATE runs SET head_seq = ?, phase = ?, outcome = ?, {_SET_ROLLUPS} "
                "WHERE run_id = ? AND head_seq = ?",
                (state.seq, state.phase, outcome, *rollups(state), state.run_id, state.seq - 1),
            ).rowcount
            if moved != 1:
                head = self._head(state.run_id)
                raise JournalConflict(
                    f"run {state.run_id}: expected head {state.seq - 1}, found {head}"
                )
            self._insert_event(event, state)
            if snapshot:
                self._run(
                    "INSERT INTO snapshots (run_id, seq, v, state) VALUES (?, ?, ?, ?)",
                    (state.run_id, state.seq, STATE_VERSION, encode_state(state)),
                )

    def _insert_event(self, event: EventBase, state: RunState) -> None:
        self._run(
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
        row = self._run("SELECT head_seq FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownRun(f"no run {run_id}")
        return int(row[0])

    def environment(self, run_id: RunId) -> dict[str, Any]:
        row = self._run("SELECT environment FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownRun(f"no run {run_id}")
        environment: dict[str, Any] = json.loads(row[0])
        return environment

    def events(self, run_id: RunId, *, after: int = 0, upto: int | None = None) -> list[AnyEvent]:
        head = self._head(run_id)
        last = head if upto is None else min(upto, head)
        rows = self._run(
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
        rows = self._run(
            "SELECT seq, state FROM snapshots WHERE run_id = ? AND v = ? ORDER BY seq",
            (run_id, STATE_VERSION),
        ).fetchall()
        for seq, payload in rows:
            yield int(seq), decode_state(payload)

    def load(self, run_id: RunId, *, upto: int | None = None) -> RunState:
        """The state after event ``upto`` (default: the head), via the nearest snapshot."""
        head = self._head(run_id)
        target = head if upto is None else min(upto, head)
        if target < 1:
            raise JournalError("sequence numbers start at 1")
        row = self._run(
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

    def rollups(self) -> list[RunRollup]:
        """Every run's rollups: one query, and no state is loaded (ADR-023)."""
        columns = ", ".join(name for name, _ in _ROLLUPS)
        rows = self._run(f"SELECT run_id, phase, outcome, {columns} FROM runs").fetchall()
        return [
            RunRollup(
                run_id=RunId(run_id),
                phase=phase,
                outcome_reason=json.loads(outcome)["reason"] if outcome else None,
                spent_usd_micros=int(spent),
                paused_seconds=float(paused),
                waiting=bool(waiting),
                revisions=int(revisions),
                working_seconds=float(working),
            )
            for run_id, phase, outcome, spent, paused, waiting, revisions, working in rows
        ]

    def _backfill_rollups(self) -> None:
        """Compute the rollups of every run a v1 journal held, from its state, once."""
        for (run_id,) in self._run("SELECT run_id FROM runs").fetchall():
            state = self.load(RunId(run_id))
            self._run(f"UPDATE runs SET {_SET_ROLLUPS} WHERE run_id = ?", (*rollups(state), run_id))

    def runs(self) -> list[RunSummary]:
        rows = self._run(
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


class SqliteJournal(SqlJournal):
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self.location = self.path
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

    def close(self) -> None:
        self._db.close()

    def _run(self, sql: str, params: Sequence[object] = ()) -> _Rows:
        return self._db.execute(sql, params)

    def _transaction(self) -> AbstractContextManager[None]:
        return _Transaction(self._db)

    def _is_duplicate(self, error: Exception) -> bool:
        return isinstance(error, sqlite3.IntegrityError)

    def _migrate(self) -> None:
        (version,) = self._db.execute("PRAGMA user_version").fetchone()
        if version == SCHEMA_VERSION:
            return
        if version not in {0, 1}:
            raise JournalCorrupt(
                f"journal schema v{version} is not supported (this msa uses v{SCHEMA_VERSION})"
            )
        with self._transaction():
            # Not executescript(): it commits implicitly, which would break atomicity.
            if version == 0:
                for statement in _SCHEMA:
                    self._db.execute(f"{statement} WITHOUT ROWID")
            else:  # v1 -> v2: the rollup columns, filled from every run's state
                for name, kind in _ROLLUPS:
                    self._db.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
                self._backfill_rollups()
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


_MIGRATION_LOCK = 0x6D_73_61_4A  # "msaJ": one migration at a time, across processes


class PostgresJournal(SqlJournal):
    """The journal in PostgreSQL, in its own schema (``msa`` by default).

    ``url`` is a libpq connection URL without a password: libpq reads it from
    ``PGPASSWORD`` or ``~/.pgpass``, so no secret is ever written to a settings file.
    Concurrent writers are serialized by the row lock the compare-and-set ``UPDATE`` takes
    on the run, exactly as SQLite's write lock does.
    """

    def __init__(self, url: str, *, schema: str = "msa") -> None:
        if problem := postgres_url_problem(url):
            raise JournalError(problem)
        self.location = url  # holds no password: postgres_url_problem refuses one
        self.schema = schema
        try:
            self._db: psycopg.Connection[Any] = psycopg.connect(url, autocommit=True)
        except psycopg.Error as exc:
            raise JournalError(f"cannot connect to the journal at {url}: {exc}") from exc
        try:
            self._db.execute("SET synchronous_commit = on")
            self._db.execute(self._sql("SET search_path TO {}"))
            self._migrate()
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def _run(self, statement: str, params: Sequence[object] = ()) -> _Rows:
        return self._db.execute(statement.replace("?", "%s"), params)

    def _transaction(self) -> AbstractContextManager[None]:
        return _PgTransaction(self._db)

    def _is_duplicate(self, error: Exception) -> bool:
        return isinstance(error, psycopg.errors.UniqueViolation)

    def _sql(self, template: str) -> sql.Composed:
        """``template`` with its ``{}`` filled by this journal's schema, quoted."""
        return sql.SQL(template).format(sql.Identifier(self.schema))

    def _migrate(self) -> None:
        with self._db.transaction():
            self._db.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
            self._db.execute(self._sql("CREATE SCHEMA IF NOT EXISTS {}"))
            self._db.execute("CREATE TABLE IF NOT EXISTS msa_schema (version INTEGER NOT NULL)")
            row = self._db.execute("SELECT version FROM msa_schema").fetchone()
            if row is None:
                for statement in _SCHEMA:
                    self._db.execute(statement)
                self._db.execute("INSERT INTO msa_schema (version) VALUES (%s)", (SCHEMA_VERSION,))
            elif row[0] == 1:  # v1 -> v2: the rollup columns, filled from every run's state
                for name, kind in _ROLLUPS:
                    self._db.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
                self._backfill_rollups()
                self._db.execute("UPDATE msa_schema SET version = %s", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                raise JournalCorrupt(
                    f"journal schema v{row[0]} is not supported (this msa uses v{SCHEMA_VERSION})"
                )


class _PgTransaction:
    def __init__(self, db: psycopg.Connection[Any]) -> None:
        self._block = db.transaction()

    def __enter__(self) -> None:
        self._block.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._block.__exit__(exc_type, value, tb)


def open_journal(path: Path | str, url: str | None = None, *, schema: str = "msa") -> SqlJournal:
    """The run journal: PostgreSQL (in ``schema``) when ``url`` is given, else the SQLite
    file at ``path``."""
    if url:
        return PostgresJournal(url, schema=schema)
    return SqliteJournal(path)


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
