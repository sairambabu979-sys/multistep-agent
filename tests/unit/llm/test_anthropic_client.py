"""The SDK adapter, against a mock transport: what goes over the wire and how failures map.

No network: ``httpx2.MockTransport`` answers every request, so these tests pin the SDK's
behavior (paths, headers, streaming, error classes) at the version in ``uv.lock``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from msa.llm.anthropic_client import AnthropicMessages, map_error
from msa.llm.client import ErrorKind, LlmError
from tests.support.llm import config

BASE_URL = "https://api.example.test"
REQUEST: dict[str, Any] = {
    "model": "claude-opus-5-5",
    "max_tokens": 1_024,
    "messages": [{"role": "user", "content": "hi"}],
    "betas": ["beta-b", "beta-a"],
    "thinking": {"type": "adaptive"},
}
START = {
    "type": "message_start",
    "message": {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1},
    },
}
COMPLETE: list[dict[str, Any]] = [
    START,
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '{"ok":'}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "true}"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 7},
    },
    {"type": "message_stop"},
]

type Handler = Callable[[httpx2.Request], httpx2.Response]


def sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def streaming(*events: dict[str, Any]) -> httpx2.Response:
    return httpx2.Response(200, content=sse(*events), headers={"content-type": "text/event-stream"})


class _Broken(httpx2.AsyncByteStream):
    """A body that starts streaming, then fails the way a dropped connection does."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield sse(START)
        raise self.error


def client(handler: Handler, seen: list[httpx2.Request] | None = None) -> AnthropicMessages:
    def recording(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    transport = httpx2.MockTransport(recording)
    return AnthropicMessages(
        base_url=BASE_URL,
        max_retries=0,
        timeout_seconds=5,
        api_key="test-key",
        http_client=httpx2.AsyncClient(transport=transport),
    )


async def _create(messages: AnthropicMessages) -> dict[str, Any]:
    try:
        return await messages.create(dict(REQUEST))
    finally:
        await messages.aclose()


def create(handler: Handler, seen: list[httpx2.Request] | None = None) -> dict[str, Any]:
    return asyncio.run(_create(client(handler, seen)))


def failure(handler: Handler) -> LlmError:
    with pytest.raises(LlmError) as info:
        create(handler)
    return info.value


# ---------- the happy path


def test_a_streamed_message_comes_back_as_its_final_json() -> None:
    assert create(lambda _: streaming(*COMPLETE)) == {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": '{"ok":true}'}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 7},
    }


def test_the_request_goes_to_the_configured_host_with_betas_as_a_header() -> None:
    seen: list[httpx2.Request] = []
    create(lambda _: streaming(*COMPLETE), seen)
    (sent,) = seen
    body = json.loads(sent.content)
    assert (sent.method, str(sent.url)) == ("POST", f"{BASE_URL}/v1/messages")
    assert sent.headers["anthropic-beta"] == "beta-b,beta-a"
    assert sent.headers["authorization"] == "Bearer test-key"
    assert "betas" not in body
    assert body["stream"] is True
    assert {k: body[k] for k in ("model", "max_tokens", "messages", "thinking")} == {
        k: REQUEST[k] for k in ("model", "max_tokens", "messages", "thinking")
    }


def test_count_tokens_sends_only_the_fields_it_accepts() -> None:
    seen: list[httpx2.Request] = []

    async def count() -> int:
        messages = client(lambda _: httpx2.Response(200, json={"input_tokens": 77}), seen)
        try:
            return await messages.count_tokens(dict(REQUEST))
        finally:
            await messages.aclose()

    assert asyncio.run(count()) == 77
    (sent,) = seen
    assert sent.url.path == "/v1/messages/count_tokens"
    assert "max_tokens" not in json.loads(sent.content)


def test_from_config_uses_the_explicit_base_url_not_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://somewhere-else.test")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return streaming(*COMPLETE)

    transport = httpx2.MockTransport(handler)
    messages = AnthropicMessages.from_config(
        config(base_url=BASE_URL),
        api_key="test-key",
        http_client=httpx2.AsyncClient(transport=transport),
    )
    asyncio.run(_create(messages))
    assert seen[0].url.host == "api.example.test"


# ---------- failures before a response starts: the SDK's to retry


@pytest.mark.parametrize(
    ("status", "error_type", "kind"),
    [
        (400, "invalid_request_error", "bad_request"),
        (401, "authentication_error", "auth"),
        (403, "permission_error", "permission"),
        (404, "not_found_error", "not_found"),
        (413, "request_too_large", "too_large"),
        (422, "invalid_request_error", "bad_request"),
        (429, "rate_limit_error", "rate_limited"),
        (500, "api_error", "server"),
        (503, "api_error", "server"),
        (529, "overloaded_error", "overloaded"),
        (409, "conflict_error", "other"),
    ],
)
def test_http_errors_map_to_their_kind(status: int, error_type: str, kind: ErrorKind) -> None:
    body = {"type": "error", "error": {"type": error_type, "message": "nope"}}
    error = failure(lambda _: httpx2.Response(status, json=body))
    assert (error.kind, error.status, error.mid_stream) == (kind, status, False)


@pytest.mark.parametrize(
    ("raised", "kind"),
    [(httpx2.ConnectTimeout("slow"), "timeout"), (httpx2.ConnectError("refused"), "connection")],
)
def test_transport_errors_before_a_response_map_to_their_kind(
    raised: Exception, kind: ErrorKind
) -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        raise raised

    error = failure(handler)
    assert (error.kind, error.retryable, error.mid_stream) == (kind, True, False)


# ---------- failures after the stream started: the gateway's to retry


@pytest.mark.parametrize(
    ("error_type", "kind", "retryable"),
    [
        ("overloaded_error", "overloaded", True),
        ("rate_limit_error", "rate_limited", True),
        ("api_error", "server", True),
        ("timeout_error", "timeout", True),
        ("invalid_request_error", "bad_request", False),
        ("billing_error", "other", False),
    ],
)
def test_an_error_event_mid_stream_maps_by_its_error_type(
    error_type: str, kind: ErrorKind, retryable: bool
) -> None:
    event = {"type": "error", "error": {"type": error_type, "message": "mid-stream"}}
    error = failure(lambda _: streaming(START, event))
    assert (error.kind, error.retryable, error.mid_stream) == (kind, retryable, True)


@pytest.mark.parametrize(
    ("raised", "kind"),
    [
        (httpx2.ReadTimeout("slow"), "timeout"),
        (httpx2.ReadError("connection reset"), "connection"),
        (httpx2.RemoteProtocolError("peer closed"), "connection"),
    ],
)
def test_a_transport_failure_mid_body_is_a_retryable_llm_error(
    raised: Exception, kind: ErrorKind
) -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        stream = _Broken(raised)
        return httpx2.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    error = failure(handler)
    assert (error.kind, error.retryable, error.mid_stream) == (kind, True, True)
    assert type(raised).__name__ in str(error)


def test_a_stream_cut_off_before_message_stop_is_a_retryable_failure() -> None:
    error = failure(lambda _: streaming(START))
    assert (error.kind, error.mid_stream) == ("connection", True)
    assert "before message_stop" in str(error)


# ---------- map_error on its own


def test_specific_sdk_classes_win_over_their_bases() -> None:
    request = httpx2.Request("POST", f"{BASE_URL}/v1/messages")
    timeout = anthropic.APITimeoutError(request=request)  # subclasses APIConnectionError
    assert map_error(timeout).kind == "timeout"
    overloaded = anthropic.OverloadedError(
        "busy", response=httpx2.Response(529, request=request), body=None
    )  # subclasses InternalServerError
    assert map_error(overloaded).kind == "overloaded"


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (502, None, "server"),
        (418, None, "other"),
        (418, {"error": "not a dict"}, "other"),
        (418, {"error": {"type": 7}}, "other"),
    ],
)
def test_unclassified_status_errors_fall_back_on_the_status(
    status: int, body: object, kind: ErrorKind
) -> None:
    request = httpx2.Request("POST", f"{BASE_URL}/v1/messages")
    error = anthropic.APIStatusError(
        "odd", response=httpx2.Response(status, request=request), body=body
    )
    mapped = map_error(error)
    assert (mapped.kind, mapped.status, mapped.mid_stream) == (kind, status, False)


def test_an_error_without_a_status_is_other() -> None:
    mapped = map_error(anthropic.AnthropicError("local"))
    assert (mapped.kind, mapped.status, mapped.retryable) == ("other", None, False)


# ---------- credentials and bugs


def test_missing_credentials_are_an_auth_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ant profile either

    def unreachable(_: httpx2.Request) -> httpx2.Response:
        raise AssertionError("a request went out without credentials")

    messages = AnthropicMessages(
        base_url=BASE_URL,
        max_retries=0,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(unreachable)),
    )
    with pytest.raises(LlmError) as info:
        asyncio.run(_create(messages))
    assert (info.value.kind, info.value.retryable) == ("auth", False)
    assert "ANTHROPIC_API_KEY" in str(info.value)


def test_any_other_type_error_is_a_bug_and_propagates() -> None:
    messages = client(lambda _: streaming(*COMPLETE))

    async def unknown_parameter() -> dict[str, Any]:
        try:
            return await messages.create({**REQUEST, "not_a_parameter": 1})
        finally:
            await messages.aclose()

    with pytest.raises(TypeError, match="not_a_parameter"):
        asyncio.run(unknown_parameter())


def test_count_tokens_body_failures_are_wrapped_and_retried_by_the_sdk() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=_Broken(httpx2.ReadError("reset")))

    async def count() -> int:
        messages = client(handler)
        try:
            return await messages.count_tokens(dict(REQUEST))
        finally:
            await messages.aclose()

    with pytest.raises(LlmError) as info:
        asyncio.run(count())
    assert (info.value.kind, info.value.mid_stream) == ("connection", False)


def test_count_tokens_without_credentials_is_an_auth_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    messages = AnthropicMessages(base_url=BASE_URL, max_retries=0, timeout_seconds=5)

    async def count() -> int:
        try:
            return await messages.count_tokens(dict(REQUEST))
        finally:
            await messages.aclose()

    with pytest.raises(LlmError, match="no API credentials"):
        asyncio.run(count())
