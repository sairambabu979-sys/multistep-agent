"""The health check passes only if the model really went through the tool."""

from __future__ import annotations

import asyncio
from typing import Any

from msa.llm import fake
from msa.llm.ping import HEALTH_TOKEN, TOKEN, PingReport, invocation, ping
from tests.support.llm import config


def script(*, token: str = TOKEN, use_tool: bool = True) -> fake.ScriptedClient:
    call = fake.tool_use("t1", "health_token", {"purpose": "ping"})
    first = [fake.message(fake.thinking(), call, stop_reason="tool_use")] if use_tool else []
    return fake.ScriptedClient(*first, fake.answer({"token": token}))


def run(client: fake.ScriptedClient) -> PingReport:
    return asyncio.run(ping(client, config()))


def test_a_ping_that_reports_the_tools_token_is_ok() -> None:
    client = script()
    report = run(client)
    assert report.ok
    assert report.used_the_tool
    assert report.models == [config().models.default]
    assert [t.stop_reason for t in report.result.turns] == ["tool_use", "end_turn"]
    answered: list[dict[str, Any]] = client.requests[1]["messages"][-1]["content"]
    assert answered == [{"type": "tool_result", "tool_use_id": "t1", "content": TOKEN}]


def test_a_ping_answered_without_the_tool_is_not_ok_even_with_the_right_token() -> None:
    report = run(script(use_tool=False))
    assert (report.ok, report.used_the_tool) == (False, False)


def test_a_ping_with_the_wrong_token_is_not_ok() -> None:
    assert not run(script(token="guessed")).ok


def test_the_ping_is_cheap_low_effort_and_strict() -> None:
    ping_invocation = invocation()
    assert ping_invocation.budget_usd_micros <= 100_000
    assert ping_invocation.effort is not None
    assert ping_invocation.effort.value == "low"
    definition = HEALTH_TOKEN.definition()
    assert definition["strict"] is True
    assert definition["input_schema"]["required"] == ["purpose"]
