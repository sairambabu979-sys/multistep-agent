"""The Tester (PLAN §3, §6.1): writes acceptance tests blind, revises them, triages failures.

- ``author``: from the task and the plan only. Its worktree is at the base tree while the
  Coder builds, so it cannot see the implementation, and its tests cannot inherit the
  implementation's mistakes.
- ``revise``: fixes its own tests after a failure report (flaky, passing before the change,
  wrong, or unlinted). It then sees the head tree.
- ``triage``: diagnoses consistent failures, read-only, into ``impl_bug``, ``test_bug`` or
  ``environment``. Flakes never reach it: the harness tells them apart by rerunning.

Its write capability (``tests/acceptance/**``, ``tests/repro/**``) lives in its workspace.
All three share one system prompt, so they share one cached prefix.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from msa.agents.common import only, prompt, section, system_prompt
from msa.agents.schemas import Plan, TestSuiteSummary, TestTriage
from msa.config import Role
from msa.llm.gateway import Invocation, Tool

TRIAGE_TOOLS = frozenset({"read_file", "search", "git_diff"})


def author(
    *,
    task: str,
    plan: Plan,
    preferences: str,
    tools: Iterable[Tool[Any]],
    budget_usd_micros: int,
    key: str,
    max_turns: int,
) -> Invocation[TestSuiteSummary]:
    body = prompt(
        section("task", task),
        section("plan", plan.render()),
        ask=(
            "Write acceptance tests for the plan's acceptance criteria under tests/acceptance/. "
            "The implementation does not exist in your worktree yet, so your tests should "
            "fail when you run them. Then answer with the summary."
        ),
    )
    return Invocation(
        role=Role.TESTER,
        system=system_prompt("tester", preferences),
        prompt=body,
        output=TestSuiteSummary,
        budget_usd_micros=budget_usd_micros,
        idempotency_key=key,
        tools=tuple(tools),
        max_turns=max_turns,
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
) -> Invocation[TestSuiteSummary]:
    body = prompt(
        section("task", task),
        section("plan", plan.render()),
        section("failure_report", failure_report),
        ask=(
            "The worktree now holds the implementation and your tests, and the report above "
            "says what is wrong with your tests. Fix them without weakening any assertion, "
            "run them, then answer with the summary."
        ),
    )
    return Invocation(
        role=Role.TESTER,
        system=system_prompt("tester", preferences),
        prompt=body,
        output=TestSuiteSummary,
        budget_usd_micros=budget_usd_micros,
        idempotency_key=key,
        tools=tuple(tools),
        max_turns=max_turns,
    )


def triage(
    *,
    task: str,
    plan: Plan,
    preferences: str,
    failure_report: str,
    tools: Iterable[Tool[Any]],
    budget_usd_micros: int,
    key: str,
    max_turns: int,
) -> Invocation[TestTriage]:
    body = prompt(
        section("task", task),
        section("plan", plan.render()),
        section("failure_report", failure_report),
        ask=(
            "These tests failed on every run, so they are not flaky. Read the failing tests and "
            "the code they exercise, then answer with your triage of the most important failure."
        ),
    )
    return Invocation(
        role=Role.TESTER,
        system=system_prompt("tester", preferences),
        prompt=body,
        output=TestTriage,
        budget_usd_micros=budget_usd_micros,
        idempotency_key=key,
        tools=only(tools, TRIAGE_TOOLS),
        max_turns=max_turns,
    )
