"""The Arbiter (PLAN §3, §4.2): the last rung of the arbitration ladder.

It rules only on what the objective rungs left open: findings no repro test could decide,
and that no preference or style rule covers. It has no tools; the dispute, both sides'
positions and every rung's result are in its prompt. Escalation runs it at the effort set
for the role (``max`` by default: it rules rarely, and a wrong ruling is expensive).
"""

from __future__ import annotations

from msa.agents.common import prompt, section, system_prompt
from msa.agents.schemas import Plan, Ruling
from msa.config import Role
from msa.llm.gateway import Invocation


def rule(
    *,
    task: str,
    plan: Plan,
    preferences: str,
    disputes: str,
    budget_usd_micros: int,
    key: str,
    max_turns: int,
) -> Invocation[Ruling]:
    body = prompt(
        section("task", task),
        section("plan", plan.render()),
        section("disputes", disputes),
        ask="Rule on every finding under disputes, then answer with one ruling per finding.",
    )
    return Invocation(
        role=Role.ARBITER,
        system=system_prompt("arbiter", preferences),
        prompt=body,
        output=Ruling,
        budget_usd_micros=budget_usd_micros,
        idempotency_key=key,
        max_turns=max_turns,
    )
