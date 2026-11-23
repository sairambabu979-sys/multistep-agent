"""Both response stores keep what a turn cost, keyed by effect and turn."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from msa.llm import fake
from msa.llm.store import MemoryResponseStore, ResponseStore, SqliteResponseStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ResponseStore]:
    if request.param == "memory":
        yield MemoryResponseStore()
        return
    with SqliteResponseStore(tmp_path / "nested" / "responses.db") as sqlite_store:
        yield sqlite_store


def test_a_stored_response_comes_back_with_its_digest(store: ResponseStore) -> None:
    response = fake.answer({"status": "ok"}, model="m")
    store.put("run-1:9:0", 0, "sha256:a", response)
    assert store.get("run-1:9:0", 0) == ("sha256:a", response)


def test_turns_and_effects_are_kept_apart(store: ResponseStore) -> None:
    store.put("run-1:9:0", 0, "sha256:a", {"turn": 0})
    store.put("run-1:9:0", 1, "sha256:b", {"turn": 1})
    store.put("run-1:9:1", 0, "sha256:c", {"turn": 0, "effect": 1})
    assert store.get("run-1:9:0", 1) == ("sha256:b", {"turn": 1})
    assert store.get("run-1:9:1", 0) == ("sha256:c", {"turn": 0, "effect": 1})
    assert store.get("run-1:9:1", 1) is None
    assert store.get("run-2:9:0", 0) is None


def test_a_re_asked_turn_replaces_the_old_answer(store: ResponseStore) -> None:
    store.put("k", 0, "sha256:old", {"v": 1})
    store.put("k", 0, "sha256:new", {"v": 2})
    assert store.get("k", 0) == ("sha256:new", {"v": 2})


def test_the_store_keeps_a_copy_not_the_callers_object(store: ResponseStore) -> None:
    response = {"content": [{"type": "text", "text": "a"}]}
    store.put("k", 0, "sha256:a", response)
    response["content"].append({"type": "text", "text": "mutated"})
    assert store.get("k", 0) == ("sha256:a", {"content": [{"type": "text", "text": "a"}]})


def test_sqlite_responses_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "responses.db"
    with SqliteResponseStore(path) as first:
        first.put("k", 3, "sha256:a", {"v": 1})
    with SqliteResponseStore(path) as second:
        assert second.get("k", 3) == ("sha256:a", {"v": 1})
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_an_in_memory_sqlite_store_works_without_a_directory() -> None:
    with SqliteResponseStore(":memory:") as store:
        store.put("k", 0, "sha256:a", {"v": 1})
        assert store.get("k", 0) == ("sha256:a", {"v": 1})


def test_a_store_that_cannot_be_opened_does_not_leak_its_connection(tmp_path: Path) -> None:
    path = tmp_path / "not-a-database.db"
    path.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        SqliteResponseStore(path)


def test_tool_results_are_kept_per_turn_as_copies(store: ResponseStore) -> None:
    results = [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]
    assert store.get_results("k", 0) is None
    store.put_results("k", 0, results)
    results.append({"type": "tool_result", "tool_use_id": "b", "content": "mutated"})
    assert store.get_results("k", 0) == [
        {"type": "tool_result", "tool_use_id": "a", "content": "ok"}
    ]
    assert store.get_results("k", 1) is None
    store.put_results("k", 0, [])
    assert store.get_results("k", 0) == []
