"""The production ``MessagesClient``: the Anthropic SDK, always streaming.

Streaming keeps long or large-``max_tokens`` requests clear of HTTP timeouts; the gateway
only needs the final message, so the stream is drained with ``get_final_message()``.

Failures the SDK raises before a response starts have already been retried by it. Failures
after the stream started are not: an ``error`` event arrives as a bare ``APIStatusError``
with HTTP status 200, a transport failure while reading the body escapes as a raw
``httpx2`` exception, and a cut-off stream yields a message with no stop reason. All three
are mapped here to ``LlmError(mid_stream=True)`` so the gateway can retry them.

The base URL is always passed explicitly from ``msa.toml``. Left implicit, the SDK would
read ``ANTHROPIC_BASE_URL`` from the environment, which some hosts set for their own
traffic. The API key still comes from the SDK's usual resolution (``ANTHROPIC_API_KEY``, an
``ant`` profile, workload identity).
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2

from msa.config import MsaConfig
from msa.llm.client import ErrorKind, LlmError, Request, Response, count_tokens_request

# Most specific first: APITimeoutError subclasses APIConnectionError, and OverloadedError
# subclasses InternalServerError, so a less specific class would hide them.
_KINDS: tuple[tuple[type[Exception], ErrorKind], ...] = (
    (anthropic.RateLimitError, "rate_limited"),
    (anthropic.OverloadedError, "overloaded"),
    (anthropic.APITimeoutError, "timeout"),
    (anthropic.APIConnectionError, "connection"),
    (anthropic.AuthenticationError, "auth"),
    (anthropic.PermissionDeniedError, "permission"),
    (anthropic.NotFoundError, "not_found"),
    (anthropic.RequestTooLargeError, "too_large"),
    (anthropic.BadRequestError, "bad_request"),
    (anthropic.UnprocessableEntityError, "bad_request"),
    (anthropic.InternalServerError, "server"),
)


# The API's own error types, for errors that carry no specific class: those that arrive as
# an ``error`` event mid-stream. Types not listed (billing, conflicts) are "other".
_ERROR_TYPES: dict[str, ErrorKind] = {
    "invalid_request_error": "bad_request",
    "authentication_error": "auth",
    "permission_error": "permission",
    "not_found_error": "not_found",
    "rate_limit_error": "rate_limited",
    "timeout_error": "timeout",
    "api_error": "server",
    "overloaded_error": "overloaded",
}


def _error_type(exc: anthropic.AnthropicError) -> str | None:
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    return kind if isinstance(kind, str) else None


def map_error(exc: anthropic.AnthropicError) -> LlmError:
    status = getattr(exc, "status_code", None)
    # An error status below 400 means the error came as an event on a started stream.
    mid_stream = isinstance(status, int) and status < 400
    message = str(exc)
    for exc_type, kind in _KINDS:
        if isinstance(exc, exc_type):
            return LlmError(kind, message, status=status, mid_stream=mid_stream)
    if (by_type := _ERROR_TYPES.get(_error_type(exc) or "")) is not None:
        return LlmError(by_type, message, status=status, mid_stream=mid_stream)
    if isinstance(status, int) and status >= 500:
        return LlmError("server", message, status=status)
    return LlmError("other", message, status=status, mid_stream=mid_stream)


_NO_CREDENTIALS = "Could not resolve authentication method"


def map_type_error(exc: TypeError) -> LlmError:
    """The SDK reports missing credentials as a TypeError when the request is built; any
    other TypeError is a bug and is re-raised unchanged."""
    if _NO_CREDENTIALS not in str(exc):
        raise exc
    return LlmError(
        "auth", "no API credentials: set ANTHROPIC_API_KEY, or configure an ant profile"
    )


def map_transport_error(exc: httpx2.HTTPError) -> LlmError:
    """A raw transport error: the SDK wraps those before a response starts, so this one
    happened while the body was being read."""
    kind: ErrorKind = "timeout" if isinstance(exc, httpx2.TimeoutException) else "connection"
    return LlmError(kind, f"{type(exc).__name__}: {exc}", mid_stream=True)


class AnthropicMessages:
    def __init__(
        self,
        *,
        base_url: str,
        max_retries: int,
        timeout_seconds: float,
        api_key: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            timeout=timeout_seconds,
            http_client=http_client,
        )

    @classmethod
    def from_config(cls, config: MsaConfig, **overrides: Any) -> AnthropicMessages:
        api = config.api
        return cls(
            base_url=api.base_url,
            max_retries=api.max_retries,
            timeout_seconds=api.timeout_seconds,
            **overrides,
        )

    async def create(self, request: Request) -> Response:
        try:
            async with self._client.beta.messages.stream(**request) as stream:
                message = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise map_error(exc) from exc
        except httpx2.HTTPError as exc:
            raise map_transport_error(exc) from exc
        except TypeError as exc:
            raise map_type_error(exc) from exc
        if message.stop_reason is None:
            raise LlmError("connection", "the stream ended before message_stop", mid_stream=True)
        return message.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def count_tokens(self, request: Request) -> int:
        try:
            counted = await self._client.beta.messages.count_tokens(**count_tokens_request(request))
        except anthropic.AnthropicError as exc:  # not streamed: the SDK wraps transport errors
            raise map_error(exc) from exc
        except TypeError as exc:
            raise map_type_error(exc) from exc
        return counted.input_tokens

    async def aclose(self) -> None:
        await self._client.close()
