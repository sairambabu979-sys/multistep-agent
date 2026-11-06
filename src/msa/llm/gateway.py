"""One agent invocation: a bounded, budgeted, replayable tool loop on the Messages API.

    request -> (store hit? replay : call API, retrying mid-stream failures)
            -> price the attempt(s) -> by stop reason:
        tool_use   : run the calls, append all results in one message, loop
        pause_turn : append the partial turn, loop
        end_turn   : validate the structured answer, return it with the spend
        refusal    : raise InvocationRefused (spend included)
        anything else, or budget/turn limits : raise InvocationFailed (spend included)

Every request follows the current API's rules for this model family (ADR-008):
- adaptive thinking (it cannot be disabled), with effort set explicitly per role, and
  ``block_binding: error`` so an edited history is a loud 400, not silently lost reasoning;
- no forced ``tool_choice``;
- no sampling parameters;
- strict tools and a strict JSON-schema answer format;
- a frozen system prompt and tool list, cached;
- an append-only transcript;
- server-side refusal fallbacks, with their echo rules.

Failures carry the spend, because the money is gone whether or not an answer came back,
and the ledger must say so.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from anthropic.types.beta import BetaMessage
from pydantic import BaseModel, ValidationError

from msa.config import Effort, MsaConfig, Role
from msa.core.budget import NO_SPEND, Spend
from msa.llm.accounting import (
    affordable_output_tokens,
    estimate_input_tokens,
    spend_of,
    task_budget_tokens,
)
from msa.llm.client import LlmError, MessagesClient, Request, count_tokens_request, digest
from msa.llm.schema import strict_schema
from msa.llm.store import ResponseStore
from msa.llm.transcript import Block, Message, Transcript

TASK_BUDGET_MINIMUM = 20_000  # the API rejects smaller task budgets
_KEPT_BEFORE_FALLBACK = frozenset({"text", "fallback"})


# ---------- tools


@dataclass(frozen=True, slots=True)
class ToolOutput:
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class Tool[I: BaseModel]:
    """A tool the model may call. Inputs are validated against ``input_model`` before the
    handler runs. A tool marked ``parallel_safe`` (read-only) may run concurrently with
    other parallel-safe calls of the same turn."""

    name: str
    description: str
    input_model: type[I]
    handler: Callable[[I], Awaitable[ToolOutput]]
    parallel_safe: bool = False

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": strict_schema(self.input_model),
            "strict": True,
        }


# ---------- invocations


@dataclass(frozen=True, slots=True)
class Invocation[O: BaseModel]:
    role: Role
    system: str
    prompt: str
    output: type[O]
    budget_usd_micros: int  # the effect's reservation: a hard cap
    idempotency_key: str  # the effect id: re-execution after a crash replays paid turns
    tools: tuple[Tool[Any], ...] = ()
    effort: Effort | None = None  # overrides the role's effort from msa.toml
    max_turns: int = 50


@dataclass(frozen=True, slots=True)
class Turn:
    index: int
    model: str  # the model that served it (a refusal fallback may differ from the request)
    stop_reason: str
    max_tokens: int
    spend: Spend
    replayed: bool  # answered from the response store, not the API
    cache_read_tokens: int
    retries: int  # mid-stream failures retried before this turn's response arrived
    thinking_dropped: int  # earlier thinking the served model could not read (a downgrade)


@dataclass(frozen=True, slots=True)
class InvocationResult[O: BaseModel]:
    output: O
    spend: Spend
    turns: tuple[Turn, ...]
    messages: list[Message]


class InvocationError(Exception):
    def __init__(self, message: str, *, spend: Spend, turns: tuple[Turn, ...]) -> None:
        super().__init__(message)
        self.spend = spend
        self.turns = turns


class InvocationFailed(InvocationError):
    """No answer: an API error, a malformed answer, or a budget or turn limit."""


class InvocationRefused(InvocationError):
    """The model declined, and so did the server-side fallbacks."""

    def __init__(
        self,
        message: str,
        *,
        category: str | None,
        spend: Spend,
        turns: tuple[Turn, ...],
    ) -> None:
        super().__init__(message, spend=spend, turns=turns)
        self.category = category


# ---------- echo rules


def _boundary(blocks: list[Block]) -> int:
    """Index of the last ``fallback`` block, or -1."""
    return max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)


def echo(message: BetaMessage) -> list[Block]:
    """The content to append to the transcript, following the fallback echo rules.

    After a mid-output fallback, model-internal blocks (thinking, tool calls, anything
    unrecognized) produced *before* the last fallback boundary must not be sent back.
    Text before it may be, and everything after it is echoed unchanged.
    """
    blocks = [b.model_dump(mode="json", by_alias=True, exclude_none=True) for b in message.content]
    boundary = _boundary(blocks)
    return [b for i, b in enumerate(blocks) if i > boundary or b["type"] in _KEPT_BEFORE_FALLBACK]


def _answer_text(content: list[Block]) -> str:
    after = content[_boundary(content) + 1 :]
    return "".join(b["text"] for b in after if b.get("type") == "text")


def _result(call: Block, content: str, *, error: bool) -> Block:
    block: Block = {"type": "tool_result", "tool_use_id": call["id"], "content": content}
    if error:
        block["is_error"] = True
    return block


# ---------- the gateway


class Gateway:
    def __init__(
        self,
        client: MessagesClient,
        config: MsaConfig,
        *,
        store: ResponseStore | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.config = config
        self.store = store
        self.sleep = sleep

    async def invoke[O: BaseModel](self, invocation: Invocation[O]) -> InvocationResult[O]:
        return await _Run(self, invocation).run()


class _Run[O: BaseModel]:
    def __init__(self, gateway: Gateway, invocation: Invocation[O]) -> None:
        self.gateway = gateway
        self.config = gateway.config
        self.inv = invocation
        self.transcript = Transcript(invocation.prompt)
        self.tools = {tool.name: tool for tool in invocation.tools}
        self.definitions = [t.definition() for t in sorted(invocation.tools, key=lambda t: t.name)]
        self.schema = strict_schema(invocation.output)
        self.model = self.config.models.model_for(invocation.role)
        self.effort = invocation.effort or self.config.models.effort_for(invocation.role)
        self.served = self.model
        self.spend = NO_SPEND
        self.turns: list[Turn] = []

    def _fail(self, message: str) -> InvocationFailed:
        return InvocationFailed(message, spend=self.spend, turns=tuple(self.turns))

    async def run(self) -> InvocationResult[O]:
        for index in range(self.inv.max_turns):
            request = await self._request()
            message, replayed, retries = await self._ask(index, request)
            self.spend += (turn_spend := spend_of(message, self.config))
            self.served = message.model
            self.turns.append(
                Turn(
                    index=index,
                    model=message.model,
                    stop_reason=message.stop_reason or "none",
                    max_tokens=request["max_tokens"],
                    spend=turn_spend,
                    replayed=replayed,
                    cache_read_tokens=message.usage.cache_read_input_tokens or 0,
                    retries=retries,
                    thinking_dropped=sum(
                        t.type == "thinking_dropped" for t in message.input_transformations or ()
                    ),
                )
            )
            if self.spend.usd_micros > self.inv.budget_usd_micros:
                raise self._fail(
                    f"spent {self.spend.usd_micros} of a {self.inv.budget_usd_micros} "
                    "micro-USD reservation"
                )
            content = echo(message)
            match message.stop_reason:
                case "end_turn" | "stop_sequence":
                    return self._answer(content)
                case "tool_use":
                    self.transcript.add_assistant(content)
                    self.transcript.add_tool_results(await self._results(index, content, replayed))
                case "pause_turn":
                    self.transcript.add_assistant(content)
                case "refusal":
                    details = message.stop_details
                    category = getattr(details, "category", None)
                    raise InvocationRefused(
                        f"declined ({category or 'uncategorized'}) on turn {index}",
                        category=category,
                        spend=self.spend,
                        turns=tuple(self.turns),
                    )
                case "max_tokens":
                    raise self._fail(
                        f"turn {index} hit max_tokens ({request['max_tokens']}) before finishing"
                    )
                case other:
                    raise self._fail(f"turn {index} stopped for an unsupported reason: {other}")
        raise self._fail(f"no answer within {self.inv.max_turns} turns")

    async def _request(self) -> Request:
        config, api = self.config, self.config.api
        output_config: dict[str, Any] = {
            "effort": self.effort.value,
            "format": {"type": "json_schema", "schema": self.schema},
        }
        betas = {api.betas.server_side_fallback, api.betas.thinking_binding}
        # Sized from the requested model's price so it stays stable across turns.
        budget_tokens = task_budget_tokens(
            self.inv.budget_usd_micros, config.pricing_for_model(self.model)
        )
        if budget_tokens >= TASK_BUDGET_MINIMUM:
            output_config["task_budget"] = {"type": "tokens", "total": budget_tokens}
            betas.add(api.betas.task_budgets)
        request: Request = {
            "model": self.model,
            "system": [
                {"type": "text", "text": self.inv.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": self.transcript.messages,
            # The transcript is append-only by type, so a thinking block failing the API's
            # prefix check is a bug here: fail loudly rather than drop it and carry on. (A
            # model-binding mismatch after a downgrade fallback is a 200 in this mode.)
            "thinking": {
                "type": "adaptive",
                "block_binding": {"prefix_mismatch_behavior": "error"},
            },
            "output_config": output_config,
            "cache_control": {"type": "ephemeral"},
            "fallbacks": api.fallbacks,
            "betas": sorted(betas),
        }
        if self.definitions:
            request["tools"] = self.definitions

        # Plan with the price of whoever served last: sticky fallbacks keep serving.
        pricing = config.pricing_for_model(self.served)
        remaining = self.inv.budget_usd_micros - self.spend.usd_micros
        max_tokens = affordable_output_tokens(
            remaining_usd_micros=remaining,
            input_tokens=estimate_input_tokens(request),
            pricing=pricing,
            cap=api.max_tokens,
        )
        if max_tokens < api.max_tokens:  # the conservative estimate binds: count exactly
            try:
                exact = await self.gateway.client.count_tokens(count_tokens_request(request))
            except LlmError:
                exact = None  # keep the conservative estimate
            if exact is not None:
                max_tokens = affordable_output_tokens(
                    remaining_usd_micros=remaining,
                    input_tokens=exact,
                    pricing=pricing,
                    cap=api.max_tokens,
                )
        if max_tokens < api.min_output_tokens:
            raise self._fail(
                f"{remaining} micro-USD left cannot fund a turn of "
                f"{api.min_output_tokens} output tokens"
            )
        request["max_tokens"] = max_tokens
        return request

    async def _ask(self, index: int, request: Request) -> tuple[BetaMessage, bool, int]:
        store, key, request_digest = self.gateway.store, self.inv.idempotency_key, digest(request)
        if store is not None and (hit := store.get(key, index)) and hit[0] == request_digest:
            return BetaMessage.model_validate(hit[1]), True, 0
        data, retries = await self._create(index, request)
        try:
            message = BetaMessage.model_validate(data)
        except ValidationError as exc:
            raise self._fail(f"turn {index} returned a malformed message: {exc}") from exc
        if store is not None:
            store.put(key, index, request_digest, data)  # durable before acting on it
        return message, False, retries

    async def _create(self, index: int, request: Request) -> tuple[Request, int]:
        """Send the request, retrying only what the SDK could not: failures mid-stream."""
        api = self.config.api
        retries = 0
        while True:
            try:
                return await self.gateway.client.create(request), retries
            except LlmError as exc:
                if not (exc.retryable and exc.mid_stream and retries < api.max_retries):
                    attempts = f" after {retries + 1} attempts" if retries else ""
                    raise self._fail(f"turn {index} failed{attempts}: {exc}") from exc
            await self.gateway.sleep(api.retry_backoff_seconds * 2**retries)
            retries += 1

    async def _results(self, index: int, content: list[Block], replayed: bool) -> list[Block]:
        """This turn's tool results: the recorded ones when the turn itself was replayed
        (its tools already ran before the crash), otherwise run now and recorded."""
        store, key = self.gateway.store, self.inv.idempotency_key
        if replayed and store is not None:
            recorded = store.get_results(key, index)
            if recorded is not None:
                return recorded
        results = await self._call_tools(content)
        if store is not None:
            store.put_results(key, index, results)  # durable before the next request
        return results

    async def _call_tools(self, content: list[Block]) -> list[Block]:
        calls = [b for b in content if b.get("type") == "tool_use"]
        if not calls:
            raise self._fail("the model stopped for tool use without a call to make")
        if all((t := self.tools.get(c["name"])) is not None and t.parallel_safe for c in calls):
            return list(await asyncio.gather(*(self._call(c) for c in calls)))
        return [await self._call(call) for call in calls]

    async def _call(self, call: Block) -> Block:
        tool = self.tools.get(call["name"])
        if tool is None:
            return _result(call, f"unknown tool {call['name']!r}", error=True)
        try:
            arguments = tool.input_model.model_validate(call["input"])
        except ValidationError as exc:
            problems = [{"at": list(e["loc"]), "problem": e["msg"]} for e in exc.errors()]
            return _result(call, f"invalid input: {json.dumps(problems)}", error=True)
        try:
            output = await tool.handler(arguments)
        except Exception as exc:  # a tool's failure is the model's information, not a crash
            return _result(call, f"{type(exc).__name__}: {exc}", error=True)
        return _result(call, output.content, error=output.is_error)

    def _answer(self, content: list[Block]) -> InvocationResult[O]:
        self.transcript.add_assistant(content)
        try:
            output = self.inv.output.model_validate_json(_answer_text(content))
        except ValidationError as exc:
            raise self._fail(
                f"the answer did not match {self.inv.output.__name__}: "
                f"{exc.error_count()} problem(s)"
            ) from exc
        return InvocationResult(
            output=output,
            spend=self.spend,
            turns=tuple(self.turns),
            messages=self.transcript.messages,
        )
