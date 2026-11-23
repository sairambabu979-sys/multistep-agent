"""Record once, replay offline: a cassette answers exactly the requests it saw."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from msa.llm import fake
from msa.llm.cassette import CASSETTE_VERSION, CassetteClient, CassetteMiss
from msa.llm.client import count_tokens_request, digest
from msa.llm.gateway import Gateway
from tests.support.llm import VERDICT, config, invocation

REQUEST = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 9}


def test_recording_needs_a_client_to_record_from(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs a client"):
        CassetteClient(tmp_path / "c.json", mode="record")


def test_a_recording_writes_every_exchange_as_it_happens(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "c.json"
    inner = fake.ScriptedClient(fake.answer(VERDICT), count_tokens=lambda _: 42)
    recorder = CassetteClient(path, mode="record", inner=inner)

    assert asyncio.run(recorder.count_tokens(REQUEST)) == 42
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == CASSETTE_VERSION
    assert [i["kind"] for i in saved["interactions"]] == ["count_tokens"]

    response = asyncio.run(recorder.create(REQUEST))
    saved = json.loads(path.read_text(encoding="utf-8"))
    counted, created = saved["interactions"]
    assert counted == {
        "kind": "count_tokens",
        "digest": digest(count_tokens_request(REQUEST)),
        "request": count_tokens_request(REQUEST),
        "response": 42,
    }
    assert created == {
        "kind": "create",
        "digest": digest(REQUEST),
        "request": REQUEST,
        "response": response,
    }
    assert [p.name for p in path.parent.iterdir()] == ["c.json"]  # no temp files left behind


def test_replay_answers_recorded_requests_in_order_without_a_client(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    inner = fake.ScriptedClient(fake.answer({"n": 1}), fake.answer({"n": 2}))
    recorder = CassetteClient(path, mode="record", inner=inner)
    first, second = asyncio.run(recorder.create(REQUEST)), asyncio.run(recorder.create(REQUEST))

    player = CassetteClient(path, mode="replay")
    assert asyncio.run(player.create(REQUEST)) == first
    assert asyncio.run(player.create({**REQUEST})) == second  # key order and identity do not matter
    with pytest.raises(CassetteMiss, match="no recorded create"):
        asyncio.run(player.create(REQUEST))  # each recording answers once


def test_a_changed_request_misses_and_says_to_re_record(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    recorder = CassetteClient(path, mode="record", inner=fake.ScriptedClient(fake.answer(VERDICT)))
    asyncio.run(recorder.create(REQUEST))
    player = CassetteClient(path, mode="replay")
    with pytest.raises(CassetteMiss, match=r"c\.json: no recorded create for sha256:.*re-record"):
        asyncio.run(player.create({**REQUEST, "max_tokens": 10}))
    with pytest.raises(CassetteMiss, match="no recorded count_tokens"):
        asyncio.run(player.count_tokens(REQUEST))


def test_an_unknown_cassette_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"version": 99, "interactions": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="cassette version 99 is unsupported"):
        CassetteClient(path, mode="replay")


def test_a_gateway_run_replays_from_its_recording_identically(tmp_path: Path) -> None:
    """The whole loop, recorded against one client and replayed with none."""
    path = tmp_path / "review.json"
    script = fake.ScriptedClient(
        fake.message(
            fake.thinking(),
            fake.tool_use("t1", "read_file", {"path": "a.py"}),
            stop_reason="tool_use",
        ),
        fake.answer(VERDICT),
    )
    live = asyncio.run(
        Gateway(CassetteClient(path, mode="record", inner=script), config()).invoke(
            invocation(budget_usd_micros=50_000)  # tight: the plan counts tokens, and is recorded
        )
    )
    replayed = asyncio.run(
        Gateway(CassetteClient(path, mode="replay"), config()).invoke(
            invocation(budget_usd_micros=50_000)
        )
    )
    assert script.counted, "the tight budget should have been planned with a count"
    assert replayed.output == live.output
    assert replayed.spend == live.spend
    assert replayed.messages == live.messages
    assert [t.max_tokens for t in replayed.turns] == [t.max_tokens for t in live.turns]
