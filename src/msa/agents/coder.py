"""The Coder (PLAN §3): implements the plan in its worktree, then revises on request.

It gets every tool of PLAN §3.2. Its write capability (everything except the Tester's
paths) lives in its workspace, so a refused write is the harness's decision, not the
prompt's (ADR-006).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from msa.agents.common import prompt, section, system_prompt
from msa.agents.schemas import ImplementationSummary, Plan
from msa.config import Role
from msa.llm.gateway import Invocation, Tool


def implement(
    *,
    task: str,
    plan: Plan,
    preferences: str,
    tools: Iterable[Tool[Any]],
    budget_usd_micros: int,
    key: str,
    max_turns: int,
) -> Invocation[ImplementationSummary]:
    body = prompt(
        section("task", task),
        section("plan", plan.render()),
        ask="Implement the plan in the worktree, check your work, then answer with the summary.",
    )
    return _invocation(
        body, preferences, tools, budget=budget_usd_micros, key=key, max_turns=max_turns
    )


def revise(
    *,
    task: str,
    plan: Plan,
    preferences: str,
    failure_report: str,
    tools: Iterable[Tool[Any]],
    budget_usd_micros: int,
    key: str,
    max_turns: int,
) -> Invocation[ImplementationSummary]:
    body = prompt(
        section("task", task),
        section("plan", plan.render()),
        section("failure_report", failure_report),
        ask=(
            "The worktree holds your implementation, and the checks in the failure report "
            "failed on it. Use git_diff to see what you changed. Fix every failure, "
            "check your work again, then answer with the summary."
        ),
    )
    return _invocation(
        body, preferences, tools, budget=budget_usd_micros, key=key, max_turns=max_turns
    )


def _invocation(
    body: str,
    preferences: str,
    tools: Iterable[Tool[Any]],
    *,
    budget: int,
    key: str,
    max_turns: int,
) -> Invocation[ImplementationSummary]:
    return Invocation(
        role=Role.CODER,
        system=system_prompt("coder", preferences),
        prompt=body,
        output=ImplementationSummary,
        budget_usd_micros=budget,
        idempotency_key=key,
        tools=tuple(tools),
        max_turns=max_turns,
    )
