"""A scripted ``MessagesClient`` and builders for API-shaped responses, for offline tests.

Responses are plain dicts shaped like the Messages API's, and the gateway validates them
with the SDK's own types, so a fake response that the SDK would not accept fails loudly.
"""

from __future__ import annotations

import copy
import json
from collections import deque
from collections.abc import Callable
from typing import Any

from msa.llm.accounting import estimate_input_tokens
from msa.llm.client import LlmError, Request, Response

type Step = Response | LlmError | Callable[[Request], Response | LlmError]


# ---------- builders


def usage(
    input_tokens: int = 100,
    output_tokens: int = 50,
    *,
    cache_read: int = 0,
    cache_write: int = 0,
    iterations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }
    if iterations is not None:
        value["iterations"] = iterations
    return value


def attempt(
    model: str, input_tokens: int, output_tokens: int, *, fallback: bool = False
) -> dict[str, Any]:
    """One entry of ``usage.iterations``: a declined attempt or the one that served."""
    return {
        "type": "fallback_message" if fallback else "message",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def text(value: object) -> dict[str, Any]:
    return {"type": "text", "text": value if isinstance(value, str) else json.dumps(value)}


def thinking(summary: str = "", signature: str = "sig") -> dict[str, Any]:
    return {"type": "thinking", "thinking": summary, "signature": signature}


def tool_use(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}


def fallback(from_model: str, to_model: str) -> dict[str, Any]:
    return {
        "type": "fallback",
        "from": {"model": from_model},
        "to": {"model": to_model},
        "trigger": {"type": "refusal"},
    }


def message(
    *content: dict[str, Any],
    stop_reason: str = "end_turn",
    model: str | None = None,
    usage_: dict[str, Any] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> Response:
    """A Messages API response. Without ``model``, ``ScriptedClient`` answers as the
    requested model, as the API does when no fallback runs."""
    response: Response = {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "content": list(content),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage_ or usage(),
    }
    if model is not None:
        response["model"] = model
    if stop_details is not None:
        response["stop_details"] = stop_details
    return response


def answer(value: object, **kwargs: Any) -> Response:
    """A final turn whose structured answer is ``value``."""
    return message(thinking(), text(value), **kwargs)


def refusal(category: str | None = "cyber", **kwargs: Any) -> Response:
    details = {"type": "refusal", "category": category, "explanation": "declined"}
    return message(stop_reason="refusal", stop_details=details, **kwargs)


# ---------- the client


class ScriptedClient:
    """Answers each ``create`` with the next scripted step and records every request.

    A step is a response, an ``LlmError`` to raise, or a function of the request returning
    either: that last form lets a test assert on what the gateway sent at that point.
    """

    def __init__(self, *steps: Step, count_tokens: Callable[[Request], int] | None = None) -> None:
        self._steps: deque[Step] = deque(steps)
        self._count_tokens = count_tokens or estimate_input_tokens
        self.requests: list[Request] = []
        self.counted: list[Request] = []

    async def create(self, request: Request) -> Response:
        self.requests.append(copy.deepcopy(request))
        if not self._steps:
            raise AssertionError(f"unscripted request #{len(self.requests)}")
        step = self._steps.popleft()
        result = step(request) if callable(step) else step
        if isinstance(result, LlmError):
            raise result
        response = copy.deepcopy(result)
        response.setdefault("model", request["model"])
        return response

    async def count_tokens(self, request: Request) -> int:
        self.counted.append(copy.deepcopy(request))
        return self._count_tokens(request)

    @property
    def exhausted(self) -> bool:
        return not self._steps

    async def aclose(self) -> None:
        """Nothing to release; present so it can stand in for a live client."""
