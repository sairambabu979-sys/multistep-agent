"""Skills and the security policy in a whole live run (ADR-016): a role's skills reach its
system prompt and narrow its tools; the policy takes tools away whatever a skill allows; and
a skill that grants what a role may not have stops the run before any model is paid."""

from __future__ import annotations

from pathlib import Path

import pytest

from msa.core.values import OutcomeReason, Phase
from msa.skills import policy, registry
from tests.support.runs import (
    GOOD_GREETER,
    Harness,
    RoleScripts,
    author_script,
    coder_script,
    make_harness,
    planner_script,
)

SKILL = """format: 1
id: py.greeter
version: 1.0.0
description: How the greeter module is extended.
roles: [coder]
instructions: Add each new greeting next to greet(), with the same signature style.
constraints: [Every greeting returns a str.]
allowed_tools: [read_file, search, edit, create_file, run]
"""


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    monkeypatch.setenv("MSA_USER_SKILLS", str(tmp_path / "no-user-skills"))
    return make_harness(tmp_path)


def install(harness: Harness, *, skill: str = SKILL, forbid: str | None = None) -> None:
    target = harness.repo / registry.REPO_DIR / "py.greeter" / "1.0.0.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(skill, encoding="utf-8")
    if forbid is not None:
        (harness.repo / policy.POLICY_FILE).write_text(forbid, encoding="utf-8")


def test_a_roles_skills_reach_its_prompt_and_narrow_its_tools(harness: Harness) -> None:
    install(harness, forbid="format: 1\nforbidden_tools:\n  tester: [run]\n")
    client = RoleScripts(
        planner=planner_script(), coder=coder_script(GOOD_GREETER), tester=author_script()
    )
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.COMPLETED, final.outcome

    coder = client.requests("coder")[0]
    system = "".join(block["text"] for block in coder["system"])
    assert "# Skills" in system
    assert "## py.greeter@1.0.0: How the greeter module is extended." in system
    assert "- Every greeting returns a str." in system
    assert [t["name"] for t in coder["tools"]] == [
        "create_file",
        "edit",
        "read_file",
        "run",
        "search",
    ]  # no git_diff: the skill does not allow it

    tester = client.requests("tester")[0]
    assert "run" not in [t["name"] for t in tester["tools"]]  # the policy forbids it
    assert "# Skills" not in "".join(block["text"] for block in tester["system"])
    planner = client.requests("planner")[0]
    assert "# Skills" not in "".join(block["text"] for block in planner["system"])


def test_a_skill_granting_what_a_role_may_not_have_stops_the_run_unpaid(
    harness: Harness,
) -> None:
    install(harness, forbid="format: 1\nforbidden_tools:\n  coder: [run]\n")
    final, journal = harness.run(RoleScripts())  # no script: no model may be called
    journal.close()
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.REQUEST_REJECTED
    assert "skills: py.greeter@1.0.0: allows coder ['run']" in final.outcome.detail
    assert final.ledger.spent.usd_micros == 0
