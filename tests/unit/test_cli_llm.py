"""``msa llm ping``: live (stubbed here), recording, and offline replay."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from msa import cli
from msa.config import MsaConfig
from msa.llm import fake
from msa.llm.anthropic_client import AnthropicMessages
from msa.llm.cassette import CassetteClient
from msa.llm.ping import TOKEN, ping
from tests.support.llm import config

runner = CliRunner()


def msa(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


def script(*, token: str = TOKEN, use_tool: bool = True) -> fake.ScriptedClient:
    call = fake.tool_use("t1", "health_token", {"purpose": "ping"})
    first = [fake.message(fake.thinking(), call, stop_reason="tool_use")] if use_tool else []
    return fake.ScriptedClient(*first, fake.answer({"token": token}))


def stub_live(monkeypatch: pytest.MonkeyPatch, client: fake.ScriptedClient) -> None:
    def live(_: MsaConfig) -> fake.ScriptedClient:
        return client

    monkeypatch.setattr(cli, "_live_client", live)


def recorded(tmp_path: Path) -> Path:
    path = tmp_path / "ping.json"
    asyncio.run(ping(CassetteClient(path, mode="record", inner=script()), config()))
    return path


def test_a_live_ping_reports_each_turn_and_the_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_live(monkeypatch, script())
    result = msa("llm", "ping")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == f"ok: claude-opus-5-5 answered {TOKEN!r} in 2 turns (live)"
    assert lines[1].startswith("  turn 0  tool_use  max_tokens ")
    assert lines[2].startswith("  turn 1  end_turn  max_tokens ")
    assert lines[3] == "  spent $0.0028"


def test_recording_writes_a_cassette_that_replays_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_live(monkeypatch, script())
    cassette = tmp_path / "live" / "ping.json"
    recording = msa("llm", "ping", "--record", str(cassette))
    assert recording.exit_code == 0, recording.output
    assert f"recorded {cassette}" in recording.output
    kinds = [i["kind"] for i in json.loads(cassette.read_text(encoding="utf-8"))["interactions"]]
    assert kinds.count("create") == 2

    def unreachable(_: MsaConfig) -> fake.ScriptedClient:
        raise AssertionError("replay must not build a live client")

    monkeypatch.setattr(cli, "_live_client", unreachable)
    replay = msa("llm", "ping", "--replay", str(cassette))
    assert replay.exit_code == 0, replay.output
    assert f"(replayed from {cassette})" in replay.output


def test_a_cassette_for_a_different_request_misses(tmp_path: Path) -> None:
    other = _changed_config(tmp_path)
    result = msa("--config", other, "llm", "ping", "--replay", str(recorded(tmp_path)))
    assert result.exit_code == 1
    assert "the request changed, so re-record the cassette" in result.output


def _changed_config(tmp_path: Path) -> str:
    text = (Path(__file__).resolve().parents[2] / "msa.toml").read_text(encoding="utf-8")
    old = '"server-side-fallback-2026-07-01"'  # a bumped beta changes every request
    assert old in text, "update this test: the fallback beta line moved"
    path = tmp_path / "other.toml"
    path.write_text(text.replace(old, '"server-side-fallback-2026-12-01"'), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("variant", "problem"),
    [
        ({"use_tool": False}, "the model answered without calling the tool"),
        ({"token": "guessed"}, f"expected token {TOKEN!r}"),
    ],
)
def test_a_ping_that_skipped_the_tool_fails(
    monkeypatch: pytest.MonkeyPatch, variant: dict[str, Any], problem: str
) -> None:
    stub_live(monkeypatch, script(**variant))  # built per test: a script is consumed by use
    result = msa("llm", "ping")
    assert result.exit_code == 1
    assert result.output.startswith("FAILED: ")
    assert f"error: {problem}" in result.output


def test_a_failed_invocation_reports_what_it_spent(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_live(monkeypatch, fake.ScriptedClient(fake.refusal("cyber")))
    result = msa("llm", "ping")
    assert result.exit_code == 1
    assert "error: declined (cyber) on turn 0 (spent $0.0014)" in result.output


def test_record_and_replay_are_exclusive(tmp_path: Path) -> None:
    cassette = recorded(tmp_path)
    result = msa("llm", "ping", "--record", str(tmp_path / "x.json"), "--replay", str(cassette))
    assert result.exit_code == 1
    assert "cannot be combined" in result.output


def test_ping_needs_the_settings_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no msa.toml here
    result = msa("llm", "ping")
    assert result.exit_code == 1
    assert "needs msa.toml" in result.output


def test_a_corrupt_cassette_is_reported(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 0, "interactions": []}), encoding="utf-8")
    result = msa("llm", "ping", "--replay", str(bad))
    assert result.exit_code == 1
    assert "cassette version 0 is unsupported" in result.output


def test_the_live_client_is_built_from_the_settings() -> None:
    live = cli._live_client(config())  # the seam the other tests stub
    try:
        assert isinstance(live, AnthropicMessages)
    finally:
        asyncio.run(live.aclose())
