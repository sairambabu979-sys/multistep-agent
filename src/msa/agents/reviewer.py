"""The Reviewer (PLAN §3, §4): reviews the change, then replies to the author's moves.

- ``review``: the task, the plan and the test report in; findings with evidence out. It
  reads the head tree and diffs it against the base; it sees the artifacts, never the
  Coder's reasoning (PLAN §4.2, anti-sycophancy).
- ``reply``: in each negotiation round, for every finding the author contested, withdraw,
  maintain or amend.

Its tools are read-only, and its workspace carries the reviewer's write capability (none).
Both share one system prompt, so they share one cached prefix.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from msa.agents.common import only, prompt, section, system_prompt
from msa.agents.schemas import Plan, Review, ReviewerReply
from msa.config import Role
from msa.llm.gateway import Invocation, Tool

REVIEWER_TOOLS = frozenset({"read_file", "search", "git_diff"})


def review(
    *,
    task: str,
    plan: Plan,
    preferences: str,
    test_report: str,
    changed: str,
    earlier: str | None,
    tools: Iterable[Tool[Any]],
    budget_usd_micros: int,
    key: str,
    max_turns: int,
) -> Invocation[Review]:
    sections = [
        section("task", task),
        section("plan", plan.render()),
        section("test_report", test_report),
        section("changed_lines", changed),
    ]
    if earlier is not None:
        sections.append(section("earlier_findings", earlier))
    ask = (
        "Review the change: use git_diff to read it and read_file for context. Then answer "
        "with your findings. Each must point at lines listed under changed_lines."
    )
    if earlier is not None:
        ask += (
            " This is a review after a revision: a new finding must point at lines the "
            "revision changed, and an agreed fix that is still missing is reopened by its id."
        )
    return _invocation(
        prompt(*sections, ask=ask),
        Review,
        preferences,
        tools,
        budget=budget_usd_micros,
        key=key,
        max_turns=max_turns,
    )


def reply(
    *,
    task: str,
    plan: Plan,
    preferences: str,
    contested: str,
    tools: Iterable[Tool[Any]],
    budget_usd_micros: int,
    key: str,
    max_turns: int,
) -> Invocation[ReviewerReply]:
    body = prompt(
        section("task", task),
        section("plan", plan.render()),
        section("contested_findings", contested),
        ask=(
            "The author contested these findings of yours. Weigh each move's evidence against "
            "your claim, reading the code where it helps, then answer with one move per finding."
        ),
    )
    return _invocation(
        body,
        ReviewerReply,
        preferences,
        tools,
        budget=budget_usd_micros,
        key=key,
        max_turns=max_turns,
    )


def _invocation[O: Review | ReviewerReply](
    body: str,
    output: type[O],
    preferences: str,
    tools: Iterable[Tool[Any]],
    *,
    budget: int,
    key: str,
    max_turns: int,
) -> Invocation[O]:
    return Invocation(
        role=Role.REVIEWER,
        system=system_prompt("reviewer", preferences),
        prompt=body,
        output=output,
        budget_usd_micros=budget,
        idempotency_key=key,
        tools=only(tools, REVIEWER_TOOLS),
        max_turns=max_turns,
    )
