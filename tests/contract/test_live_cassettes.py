"""Exchanges recorded against the real API, replayed offline (tests/cassettes/README.md)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from msa.config import load_config
from msa.llm.cassette import CassetteClient
from msa.llm.ping import ping

ROOT = Path(__file__).resolve().parents[2]
CASSETTES = ROOT / "tests" / "cassettes"
PING = CASSETTES / "live" / "ping.json"
RECORD = f"uv run msa llm ping --record {PING.relative_to(ROOT)}"
# Anthropic API keys and bearer tokens; a cassette must never hold either.
CREDENTIAL = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{16,}")


def test_the_live_ping_still_replays_against_what_the_api_accepted() -> None:
    if not PING.exists():
        pytest.skip(
            f"no live cassette recorded yet: run `{RECORD}` with ANTHROPIC_API_KEY set, "
            "or dispatch the record-cassettes workflow"
        )
    report = asyncio.run(ping(CassetteClient(PING, mode="replay"), load_config(ROOT / "msa.toml")))
    assert report.ok, report.result.output
    assert report.result.spend.usd_micros > 0


def test_no_cassette_holds_a_credential() -> None:
    leaks = [
        str(path.relative_to(ROOT))
        for path in CASSETTES.rglob("*.json")
        if CREDENTIAL.search(path.read_text(encoding="utf-8"))
    ]
    assert leaks == []


def test_the_credential_pattern_would_catch_a_leak() -> None:
    assert CREDENTIAL.search('{"key": "sk-ant-api03-' + "a" * 24 + '"}')
    assert CREDENTIAL.search("authorization: Bearer " + "b" * 40)
    assert not CREDENTIAL.search('{"signature": "' + "c" * 200 + '"}')
