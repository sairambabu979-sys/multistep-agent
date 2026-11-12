"""The tool loop's behavior, against a scripted client."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from msa.config import Effort, Role
from msa.llm import fake
from msa.llm.accounting import affordable_output_tokens, estimate_input_tokens
from msa.llm.client import LlmError, Request
from msa.llm.gateway import (
    Gateway,
    InvocationFailed,
    InvocationRefused,
    Tool,
    ToolOutput,
)
from msa.llm.store import MemoryResponseStore
from tests.support.llm import READ, VERDICT, FilePath, Verdict, config, invocation


def run(client: fake.ScriptedClient, **overrides: Any) -> Any:
    gateway = Gateway(client, config(), store=overrides.pop("store", None))
    return asyncio.run(gateway.invoke(invocation(**overrides)))


def tool_turn(*calls: dict[str, Any]) -> dict[str, Any]:
    return fake.message(fake.thinking(), *calls, stop_reason="tool_use")


# ---------- answers and spend


def test_a_structured_answer_comes_back_with_its_spend() -> None:
    client = fake.ScriptedClient(fake.answer(VERDICT))
    result = run(client)
    assert result.output == Verdict(status="ok", notes="fine")
    # 100 input tokens at $4/MTok + 50 output tokens at $20/MTok.
    assert (result.spend.usd_micros, result.spend.input_tokens) == (100 * 4 + 50 * 20, 100)
    assert [(t.index, t.stop_reason, t.replayed) for t in result.turns] == [(0, "end_turn", False)]


def test_an_answer_that_does_not_match_the_schema_fails_with_its_spend() -> None:
    client = fake.ScriptedClient(fake.answer({"status": "maybe", "notes": "?"}))
    with pytest.raises(InvocationFailed, match="did not match Verdict: 1 problem") as info:
        run(client)
    assert info.value.spend.usd_micros == 1_400


def test_a_malformed_message_is_a_failure_not_a_crash() -> None:
    client = fake.ScriptedClient({"id": "x", "type": "message"})
    with pytest.raises(InvocationFailed, match="malformed message"):
        run(client)


# ---------- tools


def test_tool_results_return_together_in_call_order() -> None:
    client = fake.ScriptedClient(
        tool_turn(
            fake.tool_use("a", "read_file", {"path": "x.py"}),
            fake.tool_use("b", "read_file", {"path": "y.py"}),
        ),
        fake.answer(VERDICT),
    )
    run(client)
    results = client.requests[1]["messages"][-1]
    assert results["role"] == "user"
    assert results["content"] == [
        {"type": "tool_result", "tool_use_id": "a", "content": "contents of x.py"},
        {"type": "tool_result", "tool_use_id": "b", "content": "contents of y.py"},
    ]


def test_parallel_safe_tools_run_concurrently() -> None:
    both_started = asyncio.Event()
    started: list[str] = []

    async def rendezvous(arguments: FilePath) -> ToolOutput:
        started.append(arguments.path)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)  # deadlocks if run serially
        return ToolOutput("ok")

    tool = Tool("read_file", "Read.", FilePath, rendezvous, parallel_safe=True)
    client = fake.ScriptedClient(
        tool_turn(
            fake.tool_use("a", "read_file", {"path": "1"}),
            fake.tool_use("b", "read_file", {"path": "2"}),
        ),
        fake.answer(VERDICT),
    )
    run(client, tools=(tool,))
    assert sorted(started) == ["1", "2"]


def test_a_writing_tool_forces_the_whole_turn_to_run_in_order() -> None:
    running = 0
    overlaps: list[int] = []

    async def write(arguments: FilePath) -> ToolOutput:
        nonlocal running
        running += 1
        overlaps.append(running)
        await asyncio.sleep(0)
        running -= 1
        return ToolOutput(f"wrote {arguments.path}")

    writer = Tool("write_file", "Write.", FilePath, write)
    client = fake.ScriptedClient(
        tool_turn(
            fake.tool_use("a", "read_file", {"path": "1"}),
            fake.tool_use("b", "write_file", {"path": "2"}),
            fake.tool_use("c", "write_file", {"path": "3"}),
        ),
        fake.answer(VERDICT),
    )
    run(client, tools=(READ, writer))
    assert overlaps == [1, 1]


class Boom(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


async def explode(_: Boom) -> ToolOutput:
    raise OSError("disk on fire")


async def soft_error(_: Boom) -> ToolOutput:
    return ToolOutput("no such file", is_error=True)


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (fake.tool_use("a", "nope", {}), "unknown tool 'nope'"),
        (
            fake.tool_use("a", "read_file", {"wrong": 1}),
            'invalid input: [{"at": ["path"], "problem": "Field required"}, '
            '{"at": ["wrong"], "problem": "Extra inputs are not permitted"}]',
        ),
        (fake.tool_use("a", "explode", {"path": "x"}), "OSError: disk on fire"),
        (fake.tool_use("a", "soft", {"path": "x"}), "no such file"),
    ],
)
def test_tool_problems_are_reported_to_the_model_as_errors(
    call: dict[str, Any], expected: str
) -> None:
    tools = (
        READ,
        Tool("explode", "Explodes.", Boom, explode),
        Tool("soft", "Fails softly.", Boom, soft_error),
    )
    client = fake.ScriptedClient(tool_turn(call), fake.answer(VERDICT))
    run(client, tools=tools)
    (result,) = client.requests[1]["messages"][-1]["content"]
    assert result == {
        "type": "tool_result",
        "tool_use_id": "a",
        "content": expected,
        "is_error": True,
    }


def test_a_tool_use_stop_without_a_call_fails() -> None:
    client = fake.ScriptedClient(fake.message(fake.thinking(), stop_reason="tool_use"))
    with pytest.raises(InvocationFailed, match="without a call to make"):
        run(client)


# ---------- stop reasons


def test_pause_turn_appends_the_partial_turn_and_continues() -> None:
    partial = fake.message(fake.thinking(), fake.text("partial"), stop_reason="pause_turn")
    client = fake.ScriptedClient(partial, fake.answer(VERDICT))
    result = run(client)
    assert result.output.status == "ok"
    assert client.requests[1]["messages"][-1] == {
        "role": "assistant",
        "content": [fake.thinking(), fake.text("partial")],
    }


def test_a_refusal_raises_with_its_category_and_spend() -> None:
    client = fake.ScriptedClient(fake.refusal("bio"))
    with pytest.raises(InvocationRefused, match=r"declined \(bio\) on turn 0") as info:
        run(client)
    assert info.value.category == "bio"
    assert info.value.spend.usd_micros == 1_400


def test_a_refusal_without_a_category_is_still_reported() -> None:
    client = fake.ScriptedClient(fake.refusal(None))
    with pytest.raises(InvocationRefused, match="uncategorized") as info:
        run(client)
    assert info.value.category is None


@pytest.mark.parametrize(
    ("stop_reason", "message"),
    [
        ("max_tokens", r"turn 0 hit max_tokens \(64000\) before finishing"),
        ("model_context_window_exceeded", "unsupported reason: model_context_window_exceeded"),
    ],
)
def test_other_stops_fail_with_the_spend_so_far(stop_reason: str, message: str) -> None:
    client = fake.ScriptedClient(fake.message(fake.text("..."), stop_reason=stop_reason))
    with pytest.raises(InvocationFailed, match=message) as info:
        run(client)
    assert info.value.spend.usd_micros == 1_400
    assert [t.stop_reason for t in info.value.turns] == [stop_reason]


def test_the_turn_limit_is_enforced() -> None:
    loop = tool_turn(fake.tool_use("a", "read_file", {"path": "x"}))
    client = fake.ScriptedClient(loop, loop)
    with pytest.raises(InvocationFailed, match="no answer within 2 turns"):
        run(client, max_turns=2)


def test_an_api_error_fails_the_invocation_and_keeps_earlier_spend() -> None:
    client = fake.ScriptedClient(
        tool_turn(fake.tool_use("a", "read_file", {"path": "x"})),
        LlmError("overloaded", "busy", status=529),
    )
    with pytest.raises(InvocationFailed, match="turn 1 failed: overloaded: busy") as info:
        run(client)
    assert info.value.spend.usd_micros == 1_400


def test_thinking_the_served_model_could_not_read_is_counted_per_turn() -> None:
    dropped = {"type": "thinking_dropped", "path": "messages.1.content.0"}
    turn = fake.answer(VERDICT)
    turn["input_transformations"] = [
        {**dropped, "reason": "model_binding_mismatch"},
        {**dropped, "type": "thinking_mismatch_allowed", "reason": "prefix_binding_mismatch"},
    ]
    result = run(fake.ScriptedClient(turn))
    assert [t.thinking_dropped for t in result.turns] == [1]


# ---------- retries: only what the SDK could not retry


def mid_stream(kind: Any = "overloaded") -> LlmError:
    return LlmError(kind, "cut off", status=200, mid_stream=True)


def run_recording_sleeps(
    client: fake.ScriptedClient, *, max_retries: int = 2
) -> tuple[list[float], Any]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    gateway = Gateway(client, config(max_retries=max_retries), sleep=sleep)
    try:
        return sleeps, asyncio.run(gateway.invoke(invocation()))
    except InvocationFailed as exc:
        return sleeps, exc


def test_mid_stream_failures_are_retried_with_doubling_backoff() -> None:
    client = fake.ScriptedClient(mid_stream(), mid_stream("connection"), fake.answer(VERDICT))
    sleeps, result = run_recording_sleeps(client)
    assert result.output == Verdict(status="ok", notes="fine")
    assert sleeps == [2.0, 4.0]
    assert [t.retries for t in result.turns] == [2]
    assert client.requests[0] == client.requests[1] == client.requests[2]


def test_mid_stream_retries_are_bounded_by_max_retries() -> None:
    client = fake.ScriptedClient(mid_stream(), mid_stream(), mid_stream())
    sleeps, error = run_recording_sleeps(client)
    assert isinstance(error, InvocationFailed)
    assert str(error) == "turn 0 failed after 3 attempts: overloaded: cut off"
    assert sleeps == [2.0, 4.0]


@pytest.mark.parametrize(
    ("error", "max_retries"),
    [
        (LlmError("rate_limited", "the SDK already retried this", status=429), 2),
        (LlmError("bad_request", "retrying cannot help", status=200, mid_stream=True), 2),
        (mid_stream(), 0),
    ],
    ids=["retried-by-the-sdk", "not-retryable", "retries-disabled"],
)
def test_other_failures_are_not_retried(error: LlmError, max_retries: int) -> None:
    sleeps, failed = run_recording_sleeps(fake.ScriptedClient(error), max_retries=max_retries)
    assert isinstance(failed, InvocationFailed)
    assert str(failed) == f"turn 0 failed: {error}"
    assert sleeps == []


# ---------- refusal fallbacks


def test_fallback_echo_rules_drop_pre_boundary_internals_and_their_tool_calls() -> None:
    turn = fake.message(
        fake.thinking("declined model's thoughts", "sig1"),
        fake.text("partial text"),
        fake.tool_use("early", "read_file", {"path": "never.py"}),
        fake.fallback("claude-opus-5-5", "claude-opus-4-8"),
        fake.thinking("", "sig2"),
        fake.tool_use("late", "read_file", {"path": "yes.py"}),
        stop_reason="tool_use",
        model="claude-opus-4-8",
    )
    client = fake.ScriptedClient(turn, fake.answer(VERDICT, model="claude-opus-4-8"))
    result = run(client)
    echoed = client.requests[1]["messages"][-2]["content"]
    assert [b["type"] for b in echoed] == ["text", "fallback", "thinking", "tool_use"]
    assert echoed[1] == fake.fallback("claude-opus-5-5", "claude-opus-4-8")
    executed = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in executed] == ["late"]
    assert {t.model for t in result.turns} == {"claude-opus-4-8"}


def test_only_text_after_the_last_fallback_is_the_answer() -> None:
    final = fake.message(
        fake.text('{"status": "not ok", "notes": "half'),
        fake.fallback("claude-opus-5-5", "claude-opus-4-8"),
        fake.text(VERDICT),
        model="claude-opus-4-8",
    )
    result = run(fake.ScriptedClient(final))
    assert result.output.status == "ok"


def test_every_attempt_is_priced_at_its_own_models_rates() -> None:
    attempts = [
        fake.attempt("claude-opus-5-5", 1_000, 200),  # declined mid-output, still billed
        fake.attempt("claude-opus-4-8", 1_000, 300, fallback=True),
    ]
    served = fake.answer(
        VERDICT, model="claude-opus-4-8", usage_=fake.usage(1_000, 300, iterations=attempts)
    )
    result = run(fake.ScriptedClient(served))
    opus_5_5 = 1_000 * 4 + 200 * 20
    opus_4_8 = 1_000 * 5 + 300 * 25
    assert result.spend.usd_micros == opus_5_5 + opus_4_8
    assert result.spend.output_tokens == 500


# ---------- budget


def test_a_tight_budget_is_planned_with_an_exact_count() -> None:
    client = fake.ScriptedClient(fake.answer(VERDICT), count_tokens=lambda _: 1_000)
    run(client, budget_usd_micros=104_000)
    assert len(client.counted) == 1
    assert "max_tokens" not in client.counted[0]
    # (104,000 - 1,000 input tokens x $8 worst-case input rate) / $20 output = 4,800 tokens.
    assert client.requests[0]["max_tokens"] == 4_800


def test_an_ample_budget_needs_no_count() -> None:
    client = fake.ScriptedClient(fake.answer(VERDICT))
    run(client, budget_usd_micros=2_000_000)
    assert client.counted == []
    assert client.requests[0]["max_tokens"] == config().api.max_tokens


def test_a_failed_count_falls_back_to_the_conservative_estimate() -> None:
    def broken(_: Request) -> int:
        raise LlmError("server", "count unavailable")

    client = fake.ScriptedClient(fake.answer(VERDICT), count_tokens=broken)
    run(client, budget_usd_micros=104_000)
    sent = client.requests[0]
    unplanned = {k: v for k, v in sent.items() if k != "max_tokens"}
    expected = affordable_output_tokens(
        remaining_usd_micros=104_000,
        input_tokens=estimate_input_tokens(unplanned),
        pricing=config().pricing_for(Role.REVIEWER),
        cap=config().api.max_tokens,
    )
    assert sent["max_tokens"] == expected


def test_a_budget_that_cannot_fund_a_turn_fails_before_spending_anything() -> None:
    client = fake.ScriptedClient()
    with pytest.raises(InvocationFailed, match="cannot fund a turn of 1024 output tokens") as info:
        run(client, budget_usd_micros=10_000)
    assert client.requests == []
    assert info.value.spend.usd_micros == 0


def test_spend_beyond_the_reservation_stops_the_invocation() -> None:
    loop = tool_turn(fake.tool_use("a", "read_file", {"path": "x"}))
    pricey = {**loop, "usage": fake.usage(1_000, 3_000)}  # 64,000 micro-USD
    client = fake.ScriptedClient(pricey, count_tokens=lambda _: 10)
    with pytest.raises(InvocationFailed, match="spent 64000 of a 50000 micro-USD reservation"):
        run(client, budget_usd_micros=50_000)


def test_later_turns_are_planned_at_the_price_of_whoever_served_last() -> None:
    attempts = [fake.attempt("claude-opus-4-8", 100, 50, fallback=True)]
    first = {
        **tool_turn(fake.tool_use("a", "read_file", {"path": "x"})),
        "model": "claude-opus-4-8",
        "usage": fake.usage(100, 50, iterations=attempts),
    }
    client = fake.ScriptedClient(first, fake.answer(VERDICT), count_tokens=lambda _: 1_000)
    run(client, budget_usd_micros=106_750)
    # Turn 0 at Opus 5.5 rates: (106,750 - 8,000) / 20. Turn 1 at Opus 4.8 rates, after
    # spending 1,750: (105,000 - 1,000 x 10 worst-case input) / 25.
    assert [r["max_tokens"] for r in client.requests] == [4_937, 3_800]


def test_task_budget_is_sent_only_when_large_enough() -> None:
    big, small = (
        fake.ScriptedClient(fake.answer(VERDICT)),
        fake.ScriptedClient(fake.answer(VERDICT)),
    )
    run(big, budget_usd_micros=2_000_000)
    run(small, budget_usd_micros=104_000)
    assert big.requests[0]["output_config"]["task_budget"] == {"type": "tokens", "total": 100_000}
    assert "task-budgets-2026-03-13" in big.requests[0]["betas"]
    assert "task_budget" not in small.requests[0]["output_config"]
    assert small.requests[0]["betas"] == [
        "server-side-fallback-2026-07-01",
        "thinking-binding-controls-2026-08-01",
    ]


# ---------- configuration


def test_the_roles_model_and_effort_come_from_msa_toml_and_can_be_overridden() -> None:
    client = fake.ScriptedClient(fake.answer(VERDICT), fake.answer(VERDICT))
    run(client, role=Role.CODER)
    run(client, role=Role.CODER, effort=Effort.LOW)
    assert [r["output_config"]["effort"] for r in client.requests] == ["xhigh", "low"]
    assert {r["model"] for r in client.requests} == {"claude-opus-5-5"}


# ---------- idempotency across crashes


def test_re_running_an_invocation_replays_paid_turns_instead_of_paying_again() -> None:
    store = MemoryResponseStore()
    script = (tool_turn(fake.tool_use("a", "read_file", {"path": "x"})), fake.answer(VERDICT))
    first = run(fake.ScriptedClient(*script), store=store)

    again = fake.ScriptedClient()  # no scripted steps: any API call would fail the test
    second = run(again, store=store)
    assert again.requests == []
    assert second.output == first.output
    assert second.spend == first.spend  # the money was spent once; it is reported once
    assert [t.replayed for t in second.turns] == [True, True]


class CrashedDuringTools(MemoryResponseStore):
    """Responses were recorded; the crash came before the turn's tool results were."""

    def put_results(self, key: str, turn: int, results: list[dict[str, Any]]) -> None:
        pass


def counting_read() -> tuple[Tool[FilePath], list[str]]:
    calls: list[str] = []

    async def read(arguments: FilePath) -> ToolOutput:
        calls.append(arguments.path)
        return ToolOutput(f"contents of {arguments.path}, read at call {len(calls)}")

    return Tool("read_file", "Read a file.", FilePath, read, parallel_safe=True), calls


def test_a_replayed_turn_reuses_its_tool_results_instead_of_running_them_again() -> None:
    """Real tool output (timings, say) differs every run. Re-running would change the next
    request and end the replay; reusing the recorded results keeps it going."""
    store = MemoryResponseStore()
    tool, calls = counting_read()
    crashed = fake.ScriptedClient(
        tool_turn(fake.tool_use("a", "read_file", {"path": "x"})),
        LlmError("connection", "the process died here", mid_stream=False),
    )
    with pytest.raises(InvocationFailed):
        run(crashed, store=store, tools=(tool,))
    resumed = fake.ScriptedClient(fake.answer(VERDICT))
    result = run(resumed, store=store, tools=(tool,))
    assert calls == ["x"]  # the tool ran once, before the crash
    assert resumed.requests == [crashed.requests[1]]  # turn 1 asked exactly as before
    assert [t.replayed for t in result.turns] == [True, False]


def test_tools_that_did_not_finish_before_a_crash_run_again() -> None:
    store = CrashedDuringTools()
    tool, calls = counting_read()
    script = (tool_turn(fake.tool_use("a", "read_file", {"path": "x"})), fake.answer(VERDICT))
    run(fake.ScriptedClient(*script), store=store, tools=(tool,))
    client = fake.ScriptedClient(fake.answer(VERDICT))
    result = run(client, store=store, tools=(tool,))
    assert calls == ["x", "x"]
    assert len(client.requests) == 1  # new tool output: turn 1 is a new question
    assert [t.replayed for t in result.turns] == [True, False]


def test_a_turn_asked_afresh_records_fresh_tool_results() -> None:
    store = MemoryResponseStore()
    tool, calls = counting_read()
    script = (tool_turn(fake.tool_use("a", "read_file", {"path": "x"})), fake.answer(VERDICT))
    run(fake.ScriptedClient(*script), store=store, tools=(tool,))
    run(fake.ScriptedClient(*script), store=store, tools=(tool,), prompt="A different task.")
    assert calls == ["x", "x"]
    assert store.get_results("run-1:9:0", 0) == [
        {"type": "tool_result", "tool_use_id": "a", "content": "contents of x, read at call 2"}
    ]


def test_an_invocation_without_tools_sends_no_tool_list() -> None:
    client = fake.ScriptedClient(fake.answer(VERDICT))
    run(client, tools=())
    assert "tools" not in client.requests[0]


def test_the_fake_refuses_requests_nobody_scripted() -> None:
    with pytest.raises(AssertionError, match="unscripted request #1"):
        asyncio.run(fake.ScriptedClient().create({"model": "m"}))
