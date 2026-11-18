"""The Tester end to end (PLAN §6.1, §6.2): blind acceptance tests that must mean something.

- I-03: an acceptance test that already passes on the base tree is rejected by the
  fail-before check, and the Tester revises it.
- I-04: a flaky test is classified ``flaky`` by rerunning it alone, surfaced in the report,
  and sent back to the Tester. It is never skipped.
- Triage: a consistent failure is diagnosed by the Tester role; an ``impl_bug`` goes to the
  Coder, whose revision makes the acceptance test pass.
- The weakened-test guard: a Tester revision that drops a check fails the run.
- Blindness: while BUILDING, the Tester's worktree holds the base tree, not the Coder's work.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from msa.core.values import OutcomeReason, Phase
from msa.llm import fake
from msa.observability.report import timeline
from tests.support.runs import (
    ACCEPTANCE,
    ACCEPTANCE_TEST,
    BASE_GREETER,
    GOOD_GREETER,
    RUN,
    SUMMARY,
    TESTS_SUMMARY,
    Harness,
    RoleScripts,
    author_script,
    coder_script,
    create,
    edit,
    make_harness,
    phases,
    planner_script,
    prompt_of,
    read,
    tool_results,
    tool_turn,
)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    return make_harness(tmp_path)


def suite_runs(harness: Harness) -> list[dict[str, Any]]:
    text = (harness.run_dir / "tests.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def revised_by_tester(path: str, old: str, new: str, before: str) -> list[fake.Step]:
    return [
        tool_turn(read("v1", path)),
        tool_turn(edit("v2", path, old, new, before)),
        fake.answer({**TESTS_SUMMARY, "summary": "Revised the tests."}),
    ]


# ---------- I-03


GREETING_TEST = (
    "from greeter import greet\n\n\ndef test_greeting() -> None:\n"
    '    assert greet("ada") == "hello ada"\n'
)


def test_i03_a_test_that_passes_before_the_change_is_rejected_and_revised(
    harness: Harness,
) -> None:
    client = RoleScripts(
        planner=planner_script(),
        coder=coder_script(GOOD_GREETER),
        tester=[
            *author_script(GREETING_TEST),
            *revised_by_tester(ACCEPTANCE, GREETING_TEST, ACCEPTANCE_TEST, GREETING_TEST),
        ],
    )
    final, journal = harness.run(client)
    with journal:
        assert final.phase is Phase.DONE, final.outcome
        seen = phases(timeline(journal.events(RUN)))
    i = seen.index("TESTING -> REVISING")
    assert seen[i : i + 4] == [
        "TESTING -> REVISING",
        "REVISING -> VERIFYING",
        "VERIFYING -> TESTING",
        "TESTING -> REVIEWING",
    ]
    assert client.exhausted

    first, last = suite_runs(harness)
    assert (first["verdict"], first["kind"]) == ("failed", "test_bug")
    rejected = harness.artifact(first["report"])
    assert f"| `{ACCEPTANCE}::test_greeting` | pass | pass |" in rejected
    assert "passes on the base tree, before the change" in rejected
    # The Tester's revision request is that report, verbatim; no model was paid to triage.
    revision = client.requests("tester")[2]
    assert "passes on the base tree" in prompt_of(revision)
    assert [t["name"] for t in revision["tools"]][:2] == ["create_file", "edit"]
    assert last["verdict"] == "passed"
    # Renaming the rejected test is not weakening: that test was the problem.
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.COMPLETED


# ---------- I-04


def flaky_test(counter: str, *, deterministic: bool) -> str:
    check = (
        '    assert farewell("bo") == "goodbye bo"\n'
        if deterministic
        else "    runs = int(COUNTER.read_text()) + 1 if COUNTER.exists() else 1\n"
        "    COUNTER.write_text(str(runs))\n"
        "    assert runs % 2 == 0\n"
    )
    return (
        "import tempfile\nfrom pathlib import Path\n\nfrom greeter import farewell\n\n"
        f'COUNTER = Path(tempfile.gettempdir()) / "{counter}"\n\n\n'
        "def test_farewell_says_goodbye() -> None:\n"
        '    assert farewell("ada") == "goodbye ada"\n\n\n'
        f"def test_farewell_every_time() -> None:\n{check}"
    )


def test_i04_a_flaky_test_is_classified_reported_and_never_skipped(harness: Harness) -> None:
    counter = f"msa-flaky-{uuid.uuid4().hex}"
    flaky, fixed = (flaky_test(counter, deterministic=d) for d in (False, True))
    client = RoleScripts(
        planner=planner_script(),
        coder=coder_script(GOOD_GREETER),
        tester=[*author_script(flaky), *revised_by_tester(ACCEPTANCE, flaky, fixed, flaky)],
    )
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.DONE, final.outcome

    first, last = suite_runs(harness)
    assert (first["verdict"], first["kind"]) == ("failed", "flaky")
    report = harness.artifact(first["report"])
    assert "## Flaky tests" in report
    assert f"- `{ACCEPTANCE}::test_farewell_every_time`: fail, pass" in report
    assert "not skipped" in report
    # It still ran after the revision, and passed: never quarantined.
    assert f"| `{ACCEPTANCE}::test_farewell_every_time` | fail | pass |" in harness.artifact(
        last["report"]
    )
    assert "flaky" in harness.report()


# ---------- triage of consistent failures


WRONG_GREETER = BASE_GREETER + '\n\ndef farewell(name: str) -> str:\n    return f"bye {name}"\n'


def test_a_consistent_failure_is_triaged_and_an_impl_bug_goes_to_the_coder(
    harness: Harness,
) -> None:
    coder = [
        tool_turn(read("c1", "src/greeter.py")),
        tool_turn(edit("c2", "src/greeter.py", BASE_GREETER, WRONG_GREETER, BASE_GREETER)),
        fake.answer(SUMMARY),
        # The revision the triage asks for.
        tool_turn(edit("c3", "src/greeter.py", '"bye {name}"', '"goodbye {name}"', WRONG_GREETER)),
        fake.answer({**SUMMARY, "summary": "farewell now says goodbye."}),
    ]
    triage = {
        "kind": "impl_bug",
        "test_id": f"{ACCEPTANCE}::test_farewell_says_goodbye",
        "diagnosis": 'src/greeter.py farewell returns "bye", the plan says "goodbye".',
    }
    client = RoleScripts(
        planner=planner_script(), coder=coder, tester=[*author_script(), fake.answer(triage)]
    )
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.DONE, final.outcome
    assert client.exhausted

    first, _ = suite_runs(harness)
    assert (first["verdict"], first["kind"]) == ("failed", "impl_bug")
    report = harness.artifact(first["report"])
    assert "## Consistent failures" in report
    assert "## Triage: impl_bug (owner: coder)" in report
    triage_request = client.requests("tester")[2]
    assert [t["name"] for t in triage_request["tools"]] == ["git_diff", "read_file", "search"]
    revision = prompt_of(client.requests("coder")[3])
    assert 'farewell returns "bye", the plan says "goodbye"' in revision


# ---------- the weakened-test guard


def test_a_tester_revision_that_drops_a_check_fails_the_run(harness: Harness) -> None:
    counter = f"msa-flaky-{uuid.uuid4().hex}"
    flaky = flaky_test(counter, deterministic=False)
    gutted = flaky.replace("    assert runs % 2 == 0\n", "")
    client = RoleScripts(
        planner=planner_script(),
        coder=coder_script(GOOD_GREETER),
        tester=[*author_script(flaky), *revised_by_tester(ACCEPTANCE, flaky, gutted, flaky)],
    )
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.FAILED
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.AGENT_FAILED
    assert final.outcome.detail == (
        "the tester weakened its tests without a reviewer's approval: "
        f"{ACCEPTANCE}::test_farewell_every_time went from 1 check(s) to 0"
    )


# ---------- blindness


def test_the_tester_writes_blind_at_the_base_tree(harness: Harness) -> None:
    """The Tester reads src/greeter.py during BUILDING and sees the base, whatever the
    Coder has done by then: its worktree never holds the implementation."""
    tester = [
        tool_turn(read("b1", "src/greeter.py")),
        tool_turn(create("t1", ACCEPTANCE, ACCEPTANCE_TEST)),
        fake.answer(TESTS_SUMMARY),
    ]
    client = RoleScripts(planner=planner_script(), coder=coder_script(GOOD_GREETER), tester=tester)
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.DONE, final.outcome
    shown = tool_results(client.requests("tester")[1])[0]["content"]
    assert "def greet" in shown
    assert "farewell" not in shown
