"""Paid responses, kept so a crash never pays for the same turn twice.

ADR-007 makes effects at-least-once: an agent invocation in flight at a crash runs again
on resume. Each turn's response is stored under ``(effect id, turn)`` together with the
digest of the request that produced it. On re-execution, a turn whose request is identical
is answered from the store instead of the API. A turn whose request differs is a different
question and is asked again.

The tool results each turn produced are stored too. A replayed turn reuses them instead of
running its tools again: their effects already happened before the crash, and real tool
output (test timings, say) is never byte-identical twice, so re-running would change the
next request and end the replay there.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

from msa.llm.client import Response

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key         TEXT NOT NULL,
    turn        INTEGER NOT NULL,
    digest      TEXT NOT NULL,
    response    TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (key, turn)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tool_results (
    key         TEXT NOT NULL,
    turn        INTEGER NOT NULL,
    results     TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (key, turn)
) WITHOUT ROWID;
"""

type Results = list[dict[str, Any]]


class ResponseStore(Protocol):
    def get(self, key: str, turn: int) -> tuple[str, Response] | None: ...

    def put(self, key: str, turn: int, digest: str, response: Response) -> None: ...

    def get_results(self, key: str, turn: int) -> Results | None: ...

    def put_results(self, key: str, turn: int, results: Results) -> None: ...


class MemoryResponseStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, int], tuple[str, Response]] = {}
        self._results: dict[tuple[str, int], Results] = {}

    def get(self, key: str, turn: int) -> tuple[str, Response] | None:
        return self._rows.get((key, turn))

    def put(self, key: str, turn: int, digest: str, response: Response) -> None:
        self._rows[(key, turn)] = (digest, json.loads(json.dumps(response)))

    def get_results(self, key: str, turn: int) -> Results | None:
        stored = self._results.get((key, turn))
        return None if stored is None else json.loads(json.dumps(stored))

    def put_results(self, key: str, turn: int, results: Results) -> None:
        self._results[(key, turn)] = json.loads(json.dumps(results))


class SqliteResponseStore:
    """Durable before the gateway acts on a response (WAL, ``synchronous=FULL``)."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
        except BaseException:
            self._db.close()
            raise

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

    def get(self, key: str, turn: int) -> tuple[str, Response] | None:
        row = self._db.execute(
            "SELECT digest, response FROM responses WHERE key = ? AND turn = ?", (key, turn)
        ).fetchone()
        if row is None:
            return None
        response: Response = json.loads(row[1])
        return str(row[0]), response

    def put(self, key: str, turn: int, digest: str, response: Response) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO responses (key, turn, digest, response, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, turn, digest, json.dumps(response), datetime.now(UTC).isoformat()),
        )

    def get_results(self, key: str, turn: int) -> Results | None:
        row = self._db.execute(
            "SELECT results FROM tool_results WHERE key = ? AND turn = ?", (key, turn)
        ).fetchone()
        if row is None:
            return None
        results: Results = json.loads(row[0])
        return results

    def put_results(self, key: str, turn: int, results: Results) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO tool_results (key, turn, results, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (key, turn, json.dumps(results), datetime.now(UTC).isoformat()),
        )
