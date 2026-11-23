"""Journals on each backend, for tests that must hold on both.

SQLite always runs. PostgreSQL runs when ``MSA_TEST_POSTGRES_URL`` names a server (CI starts
one); each test gets a schema of its own, dropped afterwards. With
``MSA_REQUIRE_POSTGRES=1`` (set in CI) a missing server fails the test instead of skipping
it, so the PostgreSQL backend is never silently untested.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from msa.runtime.journal import PostgresJournal, SqliteJournal, SqlJournal

POSTGRES_URL = os.environ.get("MSA_TEST_POSTGRES_URL", "")
REQUIRED = os.environ.get("MSA_REQUIRE_POSTGRES") == "1"
BACKENDS = ("sqlite", "postgres")

type Opener = Callable[[], SqlJournal]


def needs_postgres() -> None:
    if POSTGRES_URL:
        return
    if REQUIRED:
        pytest.fail("MSA_REQUIRE_POSTGRES=1 but MSA_TEST_POSTGRES_URL is not set")
    pytest.skip("set MSA_TEST_POSTGRES_URL to a PostgreSQL server to test that backend")


def drop_schema(schema: str) -> None:
    with psycopg.connect(POSTGRES_URL, autocommit=True) as db:
        db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def opener_for(backend: str, directory: Path) -> Iterator[Opener]:
    """Opens journals on one store: each call is a new connection to the same journal."""
    if backend == "sqlite":
        yield lambda: SqliteJournal(directory / "runs.db")
        return
    needs_postgres()
    schema = f"msa_test_{uuid.uuid4().hex[:12]}"
    try:
        yield lambda: PostgresJournal(POSTGRES_URL, schema=schema)
    finally:
        drop_schema(schema)
