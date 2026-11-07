"""What every role's invocation is built from: a cached system prompt and tagged inputs.

The system prompt is the role's prompt followed by the run's preference block. Both are
byte-stable for a run, so the cached prefix holds across every turn and every invocation
of the role (ADR-008). Everything that varies goes in the user prompt, in tagged sections:
repository-derived text is data, and the tags keep it visibly apart from instructions.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from msa.agents import prompts
from msa.llm.gateway import Tool


def system_prompt(name: prompts.PromptName, preferences: str) -> str:
    base = prompts.load(name)
    return base if not preferences else f"{base}\n{preferences}"


def section(tag: str, body: str) -> str:
    """One tagged input. A closing tag inside ``body`` is defused so the section cannot be
    ended early by repository content."""
    safe = body.replace(f"</{tag}>", f"<\\/{tag}>").strip("\n")
    return f"<{tag}>\n{safe}\n</{tag}>"


def prompt(*sections: str, ask: str) -> str:
    return "\n\n".join([*sections, ask]) + "\n"


def only(tools: Iterable[Tool[Any]], names: frozenset[str]) -> tuple[Tool[Any], ...]:
    return tuple(t for t in tools if t.name in names)


def repo_map(paths: list[str], *, limit: int) -> str:
    shown = paths[:limit]
    more = len(paths) > limit
    lines = [f"{len(paths)} tracked files" + (f"; the first {limit}:" if more else ":")]
    lines += shown
    if more:
        lines.append(f"[{len(paths) - limit} more: use search to find them]")
    return "\n".join(lines)
