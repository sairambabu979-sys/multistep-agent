"""The port between the gateway and whatever answers Messages API requests.

Requests and responses cross this boundary as plain JSON-shaped dicts. That one choice makes
three things simple: cassettes and the response store persist exactly what was sent and
received, the fake client needs no SDK objects, and the request digest is stable. The
gateway validates every response into the SDK's own ``BetaMessage`` type before reading it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol

type Request = dict[str, Any]
type Response = dict[str, Any]

ErrorKind = Literal[
    "rate_limited",
    "overloaded",
    "timeout",
    "connection",
    "server",
    "bad_request",
    "auth",
    "permission",
    "not_found",
    "too_large",
    "other",
]
RETRYABLE: frozenset[ErrorKind] = frozenset(
    {"rate_limited", "overloaded", "timeout", "connection", "server"}
)

# Fields count_tokens accepts; everything else in a Messages request is dropped for it.
COUNT_TOKENS_FIELDS = frozenset(
    {
        "betas",
        "cache_control",
        "context_management",
        "messages",
        "model",
        "output_config",
        "speed",
        "system",
        "thinking",
        "tool_choice",
        "tools",
    }
)


class LlmError(Exception):
    """A request failed. ``retryable`` says whether trying again could succeed.

    ``mid_stream`` marks a failure after the response had started streaming: an ``error``
    event, a transport failure while reading the body, or a stream cut off before
    ``message_stop``. The SDK retries failures before a response starts (with backoff,
    honouring ``retry-after``) but cannot retry these, so the gateway does.
    """

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status: int | None = None,
        mid_stream: bool = False,
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.status = status
        self.mid_stream = mid_stream
        self.retryable = kind in RETRYABLE


class MessagesClient(Protocol):
    async def create(self, request: Request) -> Response:
        """Send one Messages request and return the final message as JSON."""
        ...

    async def count_tokens(self, request: Request) -> int:
        """Exact input tokens for ``request`` (fields count_tokens does not accept dropped)."""
        ...


def count_tokens_request(request: Request) -> Request:
    return {key: value for key, value in request.items() if key in COUNT_TOKENS_FIELDS}


def digest(payload: Request) -> str:
    """A canonical digest: identical requests hash identically regardless of key order."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
