"""An append-only conversation.

The API checks that everything sent before a thinking block is byte-identical to when the
block was produced (preserved thinking), and the prompt cache only hits on an unchanged
prefix. Both reward the same discipline, so the type enforces it: there is no way to edit,
remove, or reorder a message. Operator instructions that arrive mid-run are appended as
``role: "system"`` messages instead of editing the system prompt.
"""

from __future__ import annotations

import copy
from typing import Any

type Block = dict[str, Any]
type Message = dict[str, Any]


class TranscriptError(ValueError):
    pass


class Transcript:
    __slots__ = ("_messages",)

    def __init__(self, prompt: str | list[Block]) -> None:
        self._messages: list[Message] = [{"role": "user", "content": copy.deepcopy(prompt)}]

    @property
    def messages(self) -> list[Message]:
        """A deep copy: callers can never mutate the history through it."""
        return copy.deepcopy(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    @property
    def last_role(self) -> str:
        return str(self._messages[-1]["role"])

    def add_assistant(self, content: list[Block]) -> None:
        """Append model output exactly as it should be echoed back.

        Consecutive assistant messages are allowed: a ``pause_turn`` continuation appends
        the next part of the same turn.
        """
        if not content:
            raise TranscriptError("an assistant message needs content")
        self._messages.append({"role": "assistant", "content": copy.deepcopy(content)})

    def add_tool_results(self, results: list[Block]) -> None:
        """Answer every ``tool_use`` of the last assistant message, in one user message.

        Splitting results across messages teaches the model to stop calling tools in
        parallel, so they always travel together.
        """
        if self.last_role != "assistant":
            raise TranscriptError("tool results must answer an assistant message")
        asked = {str(b["id"]) for b in self._messages[-1]["content"] if b.get("type") == "tool_use"}
        answered = [str(r.get("tool_use_id")) for r in results]
        if sorted(answered) != sorted(asked):
            raise TranscriptError(f"tool results {answered} do not answer calls {sorted(asked)}")
        self._messages.append({"role": "user", "content": copy.deepcopy(results)})

    def add_system(self, text: str) -> None:
        """An operator instruction mid-run, without touching the cached system prompt."""
        if self.last_role != "user":
            raise TranscriptError("a mid-conversation system message must follow a user message")
        self._messages.append({"role": "system", "content": text})

    def is_extended_by(self, later: list[Message]) -> bool:
        """True when ``later`` starts with exactly this transcript's messages."""
        return later[: len(self._messages)] == self._messages
