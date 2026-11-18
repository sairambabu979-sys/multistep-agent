"""The Planner (PLAN §3): reads the base tree, answers a ``Plan``. It writes nothing.

Its tools are the read-only ones, and its workspace carries the planner's write
capability (none), so even a tool it was never given could not write.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from msa.agents.common import only, prompt, repo_map, section, system_prompt
from msa.agents.schemas import Plan
from msa.config import Role
from msa.llm.gateway import Invocation, Tool

PLANNER_TOOLS = frozenset({"read_file", "search"})


def invocation(
    *,
    task: str,
    files: list[str],
    preferences: str,
    feedback: str | None,
    tools: Iterable[Tool[Any]],
    budget_usd_micros: int,
    key: str,
    max_files: int,
    max_turns: int,
) -> Invocation[Plan]:
    sections = [
        section("task", task),
        section("repository_files", repo_map(files, limit=max_files)),
    ]
    if feedback is not None:
        sections.append(section("feedback_on_previous_plan", feedback))
    ask = (
        "Read what you need, then answer with the plan."
        if feedback is None
        else "A previous plan was rejected with the feedback above. Plan again, addressing it."
    )
    return Invocation(
        role=Role.PLANNER,
        system=system_prompt("planner", preferences),
        prompt=prompt(*sections, ask=ask),
        output=Plan,
        budget_usd_micros=budget_usd_micros,
        idempotency_key=key,
        tools=only(tools, PLANNER_TOOLS),
        max_turns=max_turns,
    )
