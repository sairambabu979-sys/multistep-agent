"""Role system prompts: versioned Markdown, read as package data.

A prompt is part of every request digest, so editing one deliberately invalidates the
cassettes and the response store for that role (ADR-008). Prompts are read byte-for-byte
(line endings normalised to ``\\n``), so a checkout's line-ending settings cannot change
what is sent.
"""

from __future__ import annotations

import hashlib
from functools import cache
from importlib import resources
from typing import Literal

type PromptName = Literal["planner", "coder"]
NAMES: tuple[PromptName, ...] = ("planner", "coder")


@cache
def load(name: PromptName) -> str:
    raw = resources.files(__name__).joinpath(f"{name}.md").read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n").strip() + "\n"


def digest(name: PromptName) -> str:
    """Identifies a prompt version in run reports."""
    return "sha256:" + hashlib.sha256(load(name).encode()).hexdigest()
