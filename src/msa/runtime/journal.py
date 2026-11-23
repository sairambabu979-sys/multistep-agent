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

SCHEMA_VERSION = 1

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
    outcome     TEXT                     -- JSON, NULL while the run is live
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
                self._run(
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
        if version != 0:
            raise JournalCorrupt(
                f"journal schema v{version} is not supported (this msa uses v{SCHEMA_VERSION})"
            )
        with self._transaction():
            # Not executescript(): it commits implicitly, which would break atomicity.
            for statement in _TABLES:
                self._db.execute(f"{statement} WITHOUT ROWID")
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
                for statement in _TABLES:
                    self._db.execute(statement)
                self._db.execute("INSERT INTO msa_schema (version) VALUES (%s)", (SCHEMA_VERSION,))
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
