"""Every request the gateway sends obeys the API's rules for this model family (ADR-008).

The scripts are generated: any mix of parallel and sequential tool calls, paused turns and
mid-output refusal fallbacks, ending in an answer. Whatever the path, each request must be
one the API accepts, and the conversation must only ever grow, so preserved thinking and
the prompt cache keep working.
"""

from __future__ import annotations

import ast
import asyncio
import itertools
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict

from msa.config import Role
from msa.llm import fake
from msa.llm.client import Request
from msa.llm.gateway import Gateway, Tool, ToolOutput
from msa.llm.schema import strictness_problems
from tests.support.llm import READ, VERDICT, config, invocation

ROOT = Path(__file__).resolve().parents[2]
SENT_FIELDS = {
    "betas",
    "cache_control",
    "fallbacks",
    "max_tokens",
    "messages",
    "model",
    "output_config",
    "system",
    "thinking",
    "tools",
}
FORBIDDEN = {"temperature", "top_p", "top_k", "tool_choice", "stop_sequences"}
FALLBACK_TO = "claude-opus-5"


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    text: str


async def write_file(arguments: Edit) -> ToolOutput:
    return ToolOutput(f"wrote {len(arguments.text)} characters to {arguments.path}")


WRITE = Tool("write_file", "Write a file.", Edit, write_file)
TOOLS = (WRITE, READ)  # deliberately unsorted: the gateway must sort them


# ---------- generated scripts


def _call(index: int, name: str) -> dict[str, Any]:
    arguments = {"path": f"f{index}.py"} | ({"text": "x" * index} if name == "write_file" else {})
    return fake.tool_use(f"call_{index}", name, arguments)


@st.composite
def turns(draw: st.DrawFn) -> list[dict[str, Any]]:
    script: list[dict[str, Any]] = []
    for position in range(draw(st.integers(min_value=0, max_value=5))):
        kind = draw(st.sampled_from(["tools", "pause", "fallback_then_tools"]))
        if kind == "pause":
            script.append(fake.message(fake.thinking("…"), stop_reason="pause_turn"))
            continue
        names = draw(st.lists(st.sampled_from(["read_file", "write_file"]), min_size=1, max_size=3))
        calls = [_call(position * 10 + i, n) for i, n in enumerate(names)]
        if kind == "tools":
            script.append(fake.message(fake.thinking(), *calls, stop_reason="tool_use"))
        else:  # a declined attempt's internals, the boundary, then the fallback's own calls
            declined = [fake.thinking("declined"), fake.text("partial "), _call(999, "read_file")]
            served = [fake.fallback(fake_model(), FALLBACK_TO), fake.thinking(), *calls]
            script.append(
                fake.message(*declined, *served, stop_reason="tool_use", model=FALLBACK_TO)
            )
    return [*script, fake.answer(VERDICT)]


def fake_model() -> str:
    return config().models.model_for(Role.REVIEWER)


def sent(script: list[dict[str, Any]]) -> list[Request]:
    client = fake.ScriptedClient(*script)
    gateway = Gateway(client, config())
    asyncio.run(gateway.invoke(invocation(tools=TOOLS)))
    assert client.exhausted
    return client.requests


# ---------- the contract


@settings(max_examples=60)
@given(turns())
def test_every_request_is_well_formed(script: list[dict[str, Any]]) -> None:
    cfg = config()
    for request in sent(script):
        assert set(request) <= SENT_FIELDS
        assert not set(request) & FORBIDDEN
        assert request["model"] == cfg.models.model_for(Role.REVIEWER)
        assert request["thinking"] == {
            "type": "adaptive",
            "block_binding": {"prefix_mismatch_behavior": "error"},
        }
        assert cfg.api.betas.thinking_binding in request["betas"]
        assert request["output_config"]["effort"] == cfg.models.effort_for(Role.REVIEWER).value
        assert cfg.api.min_output_tokens <= request["max_tokens"] <= cfg.api.max_tokens
        assert cfg.api.betas.server_side_fallback in request["betas"]
        assert request["betas"] == sorted(set(request["betas"]))
        assert request["fallbacks"] == cfg.api.fallbacks
        assert request["cache_control"] == {"type": "ephemeral"}
        answer_format = request["output_config"]["format"]
        assert answer_format["type"] == "json_schema"
        assert strictness_problems(answer_format["schema"]) == []
        tools = request["tools"]
        assert [t["name"] for t in tools] == sorted(t["name"] for t in tools)
        for tool in tools:
            assert tool["strict"] is True
            assert strictness_problems(tool["input_schema"]) == []


@settings(max_examples=60)
@given(turns())
def test_the_cached_prefix_is_frozen_and_the_conversation_only_grows(
    script: list[dict[str, Any]],
) -> None:
    requests = sent(script)
    first = requests[0]
    for earlier, later in itertools.pairwise(requests):
        for frozen in ("system", "tools", "model", "thinking", "output_config", "betas"):
            assert later[frozen] == first[frozen], frozen
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]
        assert len(later["messages"]) > len(earlier["messages"])


@settings(max_examples=60)
@given(turns())
def test_no_request_prefills_the_assistant(script: list[dict[str, Any]]) -> None:
    """The last message is the user's, except to continue a turn the server paused."""
    for index, request in enumerate(sent(script)):
        last = request["messages"][-1]
        if last["role"] == "assistant":
            assert script[index - 1]["stop_reason"] == "pause_turn"
        else:
            assert last["role"] == "user"


@settings(max_examples=60)
@given(turns())
def test_nothing_from_before_a_fallback_boundary_is_echoed_but_text(
    script: list[dict[str, Any]],
) -> None:
    final = sent(script)[-1]["messages"]
    for message in final:
        if message["role"] != "assistant":
            continue
        blocks = message["content"]
        types = [b["type"] for b in blocks]
        if "fallback" in types:
            boundary = len(types) - 1 - types[::-1].index("fallback")
            assert set(types[:boundary]) <= {"text", "fallback"}
        assert "call_999" not in {b.get("id") for b in blocks}


def test_tool_results_answer_the_calls_the_fallback_model_made() -> None:
    script = [
        fake.message(
            fake.thinking("declined"),
            _call(999, "read_file"),
            fake.fallback(fake_model(), FALLBACK_TO),
            _call(1, "read_file"),
            stop_reason="tool_use",
            model=FALLBACK_TO,
        ),
        fake.answer(VERDICT, model=FALLBACK_TO),
    ]
    results = sent(script)[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["call_1"]


# ---------- dependency boundary


def test_only_the_llm_layer_imports_the_sdk_or_its_http_client() -> None:
    """Everything above msa.llm talks to models through the gateway, never the SDK."""
    offenders: list[str] = []
    for path in (ROOT / "src" / "msa").rglob("*.py"):
        if path.is_relative_to(ROOT / "src" / "msa" / "llm"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            offenders += [
                f"{path.relative_to(ROOT)}:{node.lineno}: {name}"
                for name in names
                if name.split(".")[0] in {"anthropic", "httpx", "httpx2"}
            ]
    assert offenders == []
