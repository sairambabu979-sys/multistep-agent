"""Cassettes: record real API exchanges once, replay them offline forever.

A cassette is a JSON file of ``(kind, request digest) -> response`` interactions. In
``record`` mode every request goes to the wrapped client and is written down. In
``replay`` mode nothing touches the network: a request whose digest was never recorded
is a ``CassetteMiss``, which means the prompt, tools, schema or parameters changed and the
cassette must be re-recorded. Because the digest covers the whole request, a cassette is
also a precise regression test of what the gateway sends.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal

from msa.llm.client import MessagesClient, Request, Response, count_tokens_request, digest

CASSETTE_VERSION = 1
type Kind = Literal["create", "count_tokens"]


class CassetteMiss(LookupError):
    pass


class CassetteClient:
    def __init__(
        self,
        path: Path,
        *,
        mode: Literal["record", "replay"],
        inner: MessagesClient | None = None,
    ) -> None:
        if mode == "record" and inner is None:
            raise ValueError("recording needs a client to record from")
        self.path = path
        self.mode = mode
        self._inner = inner
        self._recorded: list[dict[str, Any]] = []
        self._replay: dict[tuple[str, str], deque[Any]] = defaultdict(deque)
        if mode == "replay":
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != CASSETTE_VERSION:
                raise ValueError(f"{path}: cassette version {data.get('version')} is unsupported")
            for interaction in data["interactions"]:
                key = (interaction["kind"], interaction["digest"])
                self._replay[key].append(interaction["response"])

    async def create(self, request: Request) -> Response:
        response: Response = await self._exchange("create", request)
        return response

    async def count_tokens(self, request: Request) -> int:
        counted: int = await self._exchange("count_tokens", count_tokens_request(request))
        return counted

    async def _exchange(self, kind: Kind, request: Request) -> Any:
        request_digest = digest(request)
        if self.mode == "replay":
            queue = self._replay.get((kind, request_digest))
            if not queue:
                raise CassetteMiss(
                    f"{self.path.name}: no recorded {kind} for {request_digest}; "
                    "the request changed, so re-record the cassette"
                )
            return queue.popleft()
        if self._inner is None:  # pragma: no cover - the constructor guarantees it
            raise RuntimeError("no client to record from")
        result: Any = (
            await self._inner.create(request)
            if kind == "create"
            else await self._inner.count_tokens(request)
        )
        self._recorded.append(
            {"kind": kind, "digest": request_digest, "request": request, "response": result}
        )
        self._save()
        return result

    def _save(self) -> None:
        """Atomic: a crash mid-write never leaves a half-written cassette behind."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": CASSETTE_VERSION, "interactions": self._recorded}
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        Path(temp).replace(self.path)
