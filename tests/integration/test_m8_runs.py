"""The Reviewer, negotiation and arbitration end to end (PLAN §4, §6.2; ADR-004, ADR-014).

Each scenario is a whole live run on the greeter fixture: every executor real, the model
scripted per role, the sandbox and git real.

- I-05: a blocker the author accepts: one round, a revision, a clean second review.
- I-06: a rebuttal with evidence that the reviewer withdraws: no revision.
- I-07: a disputed correctness finding settled by a repro test, both ways.
- I-08: a design deadlock: a person is asked, the run parks, ``msa rule`` answers, and the
  run resumes to COMPLETED. The clock stopped while it waited.
- I-09: on the review after a revision, a new finding on lines the revision did not touch
  is not admitted, and neither is one outside the change.
- I-10: the budget runs out mid-negotiation: FAILED, a report, and the ledger never spent
  beyond its limit.
- The cheap rungs: a hard preference upholds, a soft one defers, uncovered style is
  dismissed, all without a model; with escalation = arbiter, the Arbiter rules on the rest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from msa.cli import app
from msa.core.values import Escalation, OutcomeReason, Phase, PlanGate
from msa.llm import fake
from msa.observability.report import timeline
from tests.support.runs import (
    ACCEPTANCE,
    GOOD_GREETER,
    RELEASE,
    RUN,
    SUMMARY,
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
    tool_turn,
)

REPRO = "tests/repro/test_run_1_c0_1.py"
STRIPPED = GOOD_GREETER.replace('f"goodbye {name}"', 'f"goodbye {name.strip()}"')


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    return make_harness(tmp_path, review=True)


# ---------- script builders


def finding(
    claim: str,
    *,
    severity: str = "blocker",
    kind: str = "correctness",
    path: str = "src/greeter.py",
    lines: tuple[int, int] = (9, 9),
    fix: str = "",
    preference: str = "",
    reopens: str = "",
) -> dict[str, Any]:
    return {
        "severity": severity,
        "kind": kind,
        "location": {"path": path, "start_line": lines[0], "end_line": lines[1]},
        "claim": claim,
        "evidence": f"src/greeter.py line {lines[0]}",
        "suggested_fix": fix,
        "preference_ref": preference,
        "reopens": reopens,
        "confidence": 0.8,
    }


def review(*findings: dict[str, Any], **kwargs: Any) -> fake.Step:
    return fake.answer({"summary": "Reviewed the change.", "findings": list(findings)}, **kwargs)


def moves(*items: tuple[str, str, str]) -> fake.Step:
    return fake.answer(
        {
            "moves": [
                {"finding_id": f, "action": a, "rationale": f"{a}: my reason", "evidence": e}
                for f, a, e in items
            ]
        }
    )


def replies(*items: tuple[str, str]) -> fake.Step:
    return fake.answer(
        {
            "moves": [
                {"finding_id": f, "action": a, "rationale": f"{a}: my reason", "amendment": ""}
                for f, a in items
            ]
        }
    )


def strip_revision() -> list[fake.Step]:
    return [
        tool_turn(
            edit(
                "r1",
                "src/greeter.py",
                'f"goodbye {name}"',
                'f"goodbye {name.strip()}"',
                GOOD_GREETER,
            )
        ),
        fake.answer({**SUMMARY, "summary": "farewell strips the name."}),
    ]


def repro_script(test: str) -> list[fake.Step]:
    return [
        tool_turn(create("x1", REPRO, test)),
        fake.answer({"written": True, "explanation": "farewell's output for a padded name."}),
    ]


def records(harness: Harness, name: str) -> list[dict[str, Any]]:
    path = harness.run_dir / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run_to_end(harness: Harness, client: RoleScripts) -> list[str]:
    final, journal = harness.run(client)
    with journal:
        assert final.phase is Phase.COMPLETED, final.outcome
        seen = phases(timeline(journal.events(RUN)))
    assert client.exhausted
    return seen


# ---------- I-05


def test_i05_a_blocker_the_author_accepts_is_fixed_in_one_revision(harness: Harness) -> None:
    padded = finding("farewell keeps surrounding spaces in the name", fix="strip the name")
    client = RoleScripts(
        planner=planner_script(),
        coder=[*coder_script(GOOD_GREETER), moves(("c0-1", "accept", "")), *strip_revision()],
        tester=author_script(),
        reviewer=[review(padded), review()],
    )
    seen = run_to_end(harness, client)
    i = seen.index("CODE_REVIEW -> NEGOTIATION")
    assert seen[i : i + 8] == [
        "CODE_REVIEW -> NEGOTIATION",
        "NEGOTIATION -> REWORK",
        "REWORK -> STATIC_ANALYSIS",
        "STATIC_ANALYSIS -> UNIT_TESTING",
        "UNIT_TESTING -> INTEGRATION_TESTING",
        "INTEGRATION_TESTING -> SECURITY_REVIEW",
        "SECURITY_REVIEW -> CODE_REVIEW",
        "CODE_REVIEW -> FINAL_VERIFICATION",
    ]
    (row,) = records(harness, "negotiation.jsonl")
    assert (row["finding"], row["author"], row["verdict"]) == ("c0-1", "accept", "accepted")
    author, revision = client.requests("coder")[4:6]
    assert sorted(t["name"] for t in author["tools"]) == ["git_diff", "read_file", "search"]
    asked = prompt_of(revision)
    assert "c0-1: blocker correctness at src/greeter.py:9" in asked
    assert "Agreed fix: strip the name" in asked
    assert "name.strip()" in (harness.run_dir / "patch.diff").read_text()
    report = harness.report()
    assert "| `c0-1` | blocker | correctness | `src/greeter.py:9-9` |" in report
    assert "| accepted | round 1 |" in report
    assert "## Stand-ins" not in report


# ---------- I-06


def test_i06_a_rebuttal_with_evidence_that_the_reviewer_withdraws_needs_no_revision(
    harness: Harness,
) -> None:
    none = finding("farewell(None) raises TypeError", severity="major", lines=(8, 9))
    evidence = "the signature is name: str, and mypy checks every caller"
    client = RoleScripts(
        planner=planner_script(),
        coder=[*coder_script(GOOD_GREETER), moves(("c0-1", "rebut", evidence))],
        tester=author_script(),
        reviewer=[review(none), replies(("c0-1", "withdraw"))],
    )
    seen = run_to_end(harness, client)
    assert "REWORK -> STATIC_ANALYSIS" not in seen[seen.index("CODE_REVIEW -> NEGOTIATION") :]
    assert seen[-4:] == ["NEGOTIATION -> FINAL_VERIFICATION", *RELEASE]
    (row,) = records(harness, "negotiation.jsonl")
    assert (row["author"], row["reviewer"], row["verdict"]) == ("rebut", "withdraw", "withdrawn")
    reply = prompt_of(client.requests("reviewer")[1])
    assert f"The author's evidence: {evidence}" in reply  # the artifact, not the reasoning


# ---------- I-07


PADDED_TEST = (
    "from greeter import farewell\n\n\ndef test_farewell_strips_the_name() -> None:\n"
    '    assert farewell(" ada ") == "goodbye ada"\n'
)
PLAIN_TEST = (
    "from greeter import farewell\n\n\ndef test_farewell_has_one_space() -> None:\n"
    '    assert farewell("ada") == "goodbye ada"\n'
)


def test_i07_a_repro_test_that_fails_upholds_the_finding_and_joins_the_tree(
    harness: Harness,
) -> None:
    claim = finding("farewell keeps surrounding spaces in the name", fix="strip the name")
    client = RoleScripts(
        planner=planner_script(),
        coder=[
            *coder_script(GOOD_GREETER),
            moves(("c0-1", "rebut", "the plan never asks to strip names")),
            *strip_revision(),
        ],
        tester=[*author_script(), *repro_script(PADDED_TEST)],
        reviewer=[review(claim), replies(("c0-1", "maintain")), review()],
    )
    seen = run_to_end(harness, client)
    assert "NEGOTIATION -> BLOCKED" not in seen  # the test decided it, not a person
    (ruling,) = records(harness, "arbitration.jsonl")
    assert (ruling["rung"], ruling["verdict"], ruling["path"]) == ("repro", "upheld", REPRO)
    assert "fails on the head tree" in ruling["detail"]
    patch = (harness.run_dir / "patch.diff").read_text()
    assert f"b/{REPRO}" in patch  # kept as a regression test
    assert "name.strip()" in patch
    revision = prompt_of(client.requests("coder")[5])
    assert f"Repro test (make it pass; do not change it): {REPRO}" in revision
    last = records(harness, "tests.jsonl")[-1]
    matrix = harness.artifact(last["report"])
    assert f"| `{REPRO}::test_farewell_strips_the_name` | repro | pass |" in matrix
    assert f"| `{ACCEPTANCE}::test_farewell_says_goodbye` | fail | pass |" in matrix


def test_i07_a_repro_test_that_passes_dismisses_the_finding(harness: Harness) -> None:
    claim = finding('farewell("ada") puts two spaces before the name')
    client = RoleScripts(
        planner=planner_script(),
        coder=[
            *coder_script(GOOD_GREETER),
            moves(("c0-1", "rebut", 'the f-string is "goodbye {name}": one space')),
        ],
        tester=[*author_script(), *repro_script(PLAIN_TEST)],
        reviewer=[review(claim), replies(("c0-1", "maintain"))],
    )
    seen = run_to_end(harness, client)
    assert seen[-4:] == ["NEGOTIATION -> FINAL_VERIFICATION", *RELEASE]
    (ruling,) = records(harness, "arbitration.jsonl")
    assert (ruling["rung"], ruling["verdict"]) == ("repro", "dismissed")
    assert REPRO not in (harness.run_dir / "patch.diff").read_text()
    assert "| dismissed | repro |" in harness.report()


# ---------- I-08


def test_i08_a_design_deadlock_parks_for_a_person_and_resumes_with_the_ruling(
    harness: Harness,
) -> None:
    helper = finding(
        "farewell duplicates greet's formatting; extract a _salute(word, name) helper",
        severity="major",
        kind="design",
        lines=(8, 9),
    )
    client = RoleScripts(
        planner=planner_script(),
        coder=[
            *coder_script(GOOD_GREETER),
            moves(("c0-1", "rebut", "two one-line functions: a helper adds indirection")),
        ],
        tester=author_script(),
        reviewer=[review(helper), replies(("c0-1", "maintain"))],
    )
    parked, journal = harness.run(client)
    journal.close()
    assert parked.phase is Phase.BLOCKED
    assert parked.waiting_since is not None
    assert client.exhausted
    question = (harness.run_dir / "question.md").read_text(encoding="utf-8")
    assert "c0-1: major design at src/greeter.py:8-9" in question
    assert "Round 1: author rebut" in question
    assert f"`msa rule {RUN} c0-1=upheld`" in question
    assert "BLOCKED (live" in harness.report()

    db = str(harness.state_dir / "runs.db")
    wrong = CliRunner().invoke(app, ["rule", RUN, "c0-1=accepted", "--db", db, "--no-resume"])
    assert wrong.exit_code != 0
    assert "expected FINDING=upheld|dismissed|deferred" in wrong.output
    ruled = CliRunner().invoke(app, ["rule", RUN, "c0-1=dismissed", "--db", db, "--no-resume"])
    assert ruled.exit_code == 0, ruled.output
    assert f"{RUN}: FINAL_VERIFICATION (live" in ruled.output
    again = CliRunner().invoke(app, ["rule", RUN, "c0-1=upheld", "--db", db, "--no-resume"])
    assert "is not waiting for await_human_ruling" in again.output

    final, journal = harness.resume(RoleScripts())
    journal.close()
    assert final.phase is Phase.COMPLETED, final.outcome
    assert final.paused > parked.paused  # the wait moved the deadline, not the work
    assert final.deadline == final.started_at + final.limits.wall_clock + final.paused
    (human,) = (r for r in records(harness, "arbitration.jsonl") if r["rung"] == "human")
    assert (human["finding"], human["verdict"]) == ("c0-1", "dismissed")
    assert "| dismissed | human |" in harness.report()


# ---------- I-09


def test_i09_later_findings_must_be_on_lines_the_revision_changed(harness: Harness) -> None:
    padded = finding("farewell keeps surrounding spaces in the name", fix="strip the name")
    elsewhere = finding("greet should also strip", lines=(4, 5))  # base code, untouched
    late = finding(
        "the Coder's test does not cover padding", path="tests/test_farewell.py", lines=(5, 5)
    )
    client = RoleScripts(
        planner=planner_script(),
        coder=[*coder_script(GOOD_GREETER), moves(("c0-1", "accept", "")), *strip_revision()],
        tester=author_script(),
        reviewer=[review(padded, elsewhere), review(late)],
    )
    seen = run_to_end(harness, client)
    assert seen[-4:] == ["CODE_REVIEW -> FINAL_VERIFICATION", *RELEASE]
    found = {r["id"]: r for r in records(harness, "findings.jsonl")}
    assert (found["c0-1"]["admitted"], found["c0-2"]["admitted"], found["c1-1"]["admitted"]) == (
        True,
        False,
        False,
    )
    assert found["c0-2"]["reason"] == "src/greeter.py:4-5 is not on a line this change touched"
    assert found["c1-1"]["reason"] == (
        "tests/test_farewell.py:5 is not on a line the latest revision changed"
    )
    second = prompt_of(client.requests("reviewer")[1])
    assert "<earlier_findings>" in second
    assert "c0-1: blocker correctness" in second
    assert "Outcome: accepted" in second
    assert "| `c1-1` | blocker |" in harness.report()
    assert "| not admitted |" in harness.report()


# ---------- I-10


def test_i10_the_budget_running_out_mid_negotiation_fails_the_run_within_its_limit(
    harness: Harness,
) -> None:
    limits = harness.config.limits.model_copy(update={"budget_usd": 1.0})
    harness.config = harness.config.model_copy(update={"limits": limits})
    padded = finding("farewell keeps surrounding spaces in the name")
    # The review costs nearly all the budget: no author turn can be afforded after it.
    expensive = review(padded, usage_=fake.usage(input_tokens=100, output_tokens=48_500))
    client = RoleScripts(
        planner=planner_script(),
        coder=coder_script(GOOD_GREETER),
        tester=author_script(),
        reviewer=[expensive],
    )
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.FAILED
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.AGENT_FAILED
    assert "cannot fund a turn" in final.outcome.detail
    assert final.ledger.spent.usd_micros <= final.ledger.limit_usd_micros
    assert client.exhausted  # the author was never asked: its turn was refused unpaid
    report = harness.report()
    assert "**Outcome:** FAILED: agent_failed" in report
    assert "| `c0-1` | blocker | correctness |" in report
    assert "| open | - |" in report


# ---------- the cheap rungs, and the Arbiter


def test_preferences_and_style_settle_without_a_model_then_the_arbiter_rules(
    harness: Harness,
) -> None:
    escalated = harness.config.limits.model_copy(update={"escalation": Escalation.ARBITER})
    harness.config = harness.config.model_copy(update={"limits": escalated})
    hard = finding("a bare except", kind="preference", preference="py.errors.no-bare-except")
    soft = finding("prints", severity="major", kind="preference", preference="py.output.no-print")
    taste = finding("prefer a docstring here", kind="style", lines=(8, 8))
    design = finding("farewell belongs in its own module", severity="major", kind="design")
    contested = [(f, "rebut", "not so") for f in ("c0-1", "c0-2", "c0-3", "c0-4")]
    client = RoleScripts(
        planner=planner_script(),
        coder=[*coder_script(GOOD_GREETER), moves(*contested), *strip_revision()],
        tester=author_script(),
        reviewer=[
            review(hard, soft, taste, design),
            replies(*((f, "maintain") for f, _, _ in contested)),
            review(),
        ],
        arbiter=[
            fake.answer(
                {"rulings": [{"finding_id": "c0-4", "verdict": "deferred", "rationale": "later"}]}
            )
        ],
    )
    run_to_end(harness, client)
    ruled = {r["finding"]: (r["rung"], r["verdict"]) for r in records(harness, "arbitration.jsonl")}
    assert ruled == {
        "c0-1": ("preference", "upheld"),
        "c0-2": ("preference", "deferred"),
        "c0-3": ("style", "dismissed"),
        "c0-4": ("arbiter", "deferred"),
    }
    disputes = prompt_of(client.requests("arbiter")[0])
    assert "c0-4: major design" in disputes
    assert "c0-1" not in disputes  # the Arbiter sees only what the cheap rungs left open
    assert "tools" not in client.requests("arbiter")[0]  # the Arbiter has no tools
    assert "**Follow-ups** (deferred): `c0-2`, `c0-4`" in harness.report()


# ---------- the plan gate: the same parking, answered by `msa approve`


def test_a_human_plan_gate_parks_until_msa_approve_answers(harness: Harness) -> None:
    gated = harness.config.limits.model_copy(update={"plan_gate": PlanGate.HUMAN})
    harness.config = harness.config.model_copy(update={"limits": gated})
    db = str(harness.state_dir / "runs.db")

    first, journal = harness.run(RoleScripts(planner=planner_script()))
    journal.close()
    assert first.phase is Phase.PLAN_APPROVAL
    question = (harness.run_dir / "question.md").read_text(encoding="utf-8")
    assert "# Run run-1: approve the plan?" in question
    assert "Add farewell next to greet" in question
    assert f'`msa approve {RUN} --reject "what to change"`' in question

    both = CliRunner().invoke(app, ["approve", RUN, "--reject", "x", "--abort", "y", "--db", db])
    assert both.exit_code != 0
    assert "--reject and --abort are alternatives" in both.output
    sent_back = CliRunner().invoke(
        app, ["approve", RUN, "--reject", "test the empty name too", "--db", db, "--no-resume"]
    )
    assert f"{RUN}: PLANNING (live" in sent_back.output, sent_back.output

    replanner = RoleScripts(planner=planner_script())
    second, journal = harness.resume(replanner)
    journal.close()
    assert second.phase is Phase.PLAN_APPROVAL
    assert "test the empty name too" in prompt_of(replanner.requests("planner")[0])

    approved = CliRunner().invoke(app, ["approve", RUN, "--db", db, "--no-resume"])
    assert f"{RUN}: IMPLEMENTING (live" in approved.output, approved.output
    rest = RoleScripts(
        coder=coder_script(GOOD_GREETER), tester=author_script(), reviewer=[review()]
    )
    final, journal = harness.resume(rest)
    journal.close()
    assert final.phase is Phase.COMPLETED, final.outcome
    assert final.paused > first.paused
    assert rest.exhausted


def test_a_person_can_stop_the_run_instead_of_answering(harness: Harness) -> None:
    gated = harness.config.limits.model_copy(update={"plan_gate": PlanGate.HUMAN})
    harness.config = harness.config.model_copy(update={"limits": gated})
    db = str(harness.state_dir / "runs.db")
    parked, journal = harness.run(RoleScripts(planner=planner_script()))
    journal.close()
    assert parked.phase is Phase.PLAN_APPROVAL
    wrong = CliRunner().invoke(app, ["rule", RUN, "--abort", "no", "--db", db])
    assert "is not waiting for await_human_ruling: it is PLAN_APPROVAL" in wrong.output
    stopped = CliRunner().invoke(
        app, ["approve", RUN, "--abort", "not now", "--by", "grace", "--db", db]
    )
    assert f"{RUN}: CANCELLED: human_aborted (grace: not now)" in stopped.output, stopped.output
    none = CliRunner().invoke(app, ["rule", RUN, "--db", db])
    assert "give FINDING=VERDICT for every disputed finding, or --abort" in none.output


# ---------- when a repro test cannot decide, the dispute climbs the ladder


SKIPPED_REPRO = PADDED_TEST.replace(
    "from greeter import farewell\n\n\ndef",
    'import pytest\n\nfrom greeter import farewell\n\n\n@pytest.mark.skip(reason="no")\ndef',
)


@pytest.mark.parametrize(
    ("tester", "detail"),
    [
        (
            [fake.answer({"written": False, "explanation": "it is about intent, not output"})],
            "no repro test: it is about intent, not output",
        ),
        (repro_script("from greeter import nothing_here\n"), "the repro test has no test"),
        (repro_script(PADDED_TEST.replace("from greeter", "from nowhere")), "does not import"),
        (repro_script(SKIPPED_REPRO), "the repro test is marked skip or xfail"),
        (
            repro_script(PADDED_TEST.replace('farewell(" ada ") == "goodbye ada"', "True")),
            "the repro test has no test with a live check",
        ),
    ],
)
def test_a_repro_test_that_cannot_decide_leaves_the_dispute_to_a_person(
    harness: Harness, tester: list[fake.Step], detail: str
) -> None:
    claim = finding("farewell keeps surrounding spaces in the name")
    client = RoleScripts(
        planner=planner_script(),
        coder=[*coder_script(GOOD_GREETER), moves(("c0-1", "rebut", "the plan never asks"))],
        tester=[*author_script(), *tester],
        reviewer=[review(claim), replies(("c0-1", "maintain"))],
    )
    parked, journal = harness.run(client)
    journal.close()
    assert parked.phase is Phase.BLOCKED, parked.outcome
    (ruling,) = records(harness, "arbitration.jsonl")
    assert (ruling["rung"], ruling["verdict"]) == ("repro", None)
    assert detail in ruling["detail"]
    assert f"Arbitration so far: repro: {ruling['detail']}" in (
        harness.run_dir / "question.md"
    ).read_text(encoding="utf-8")


def test_an_arbiter_that_skips_a_finding_fails_the_run(harness: Harness) -> None:
    escalated = harness.config.limits.model_copy(update={"escalation": Escalation.ARBITER})
    harness.config = harness.config.model_copy(update={"limits": escalated})
    design = finding("farewell belongs in its own module", severity="major", kind="design")
    client = RoleScripts(
        planner=planner_script(),
        coder=[*coder_script(GOOD_GREETER), moves(("c0-1", "rebut", "one function"))],
        tester=author_script(),
        reviewer=[review(design), replies(("c0-1", "maintain"))],
        arbiter=[fake.answer({"rulings": []})],
    )
    final, journal = harness.run(client)
    journal.close()
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.AGENT_FAILED
    assert final.outcome.detail == "the arbiter did not rule on c0-1"
    # What the effect spent before failing is charged, not lost.
    assert final.ledger.spent.usd_micros > 0


def test_a_failure_in_the_reviewers_reply_charges_what_the_round_spent(harness: Harness) -> None:
    claim = finding("farewell keeps surrounding spaces in the name")
    client = RoleScripts(
        planner=planner_script(),
        coder=[*coder_script(GOOD_GREETER), moves(("c0-1", "rebut", "the plan never asks"))],
        tester=author_script(),
        reviewer=[review(claim), fake.refusal()],
    )
    final, journal = harness.run(client)
    journal.close()
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.AGENT_REFUSED
    rounds = [
        i for i in records(harness, "invocations.jsonl") if i["kind"] == "run_negotiation_round"
    ]
    assert [(i["role"], i["status"]) for i in rounds] == [
        ("coder", "ok"),
        ("reviewer", "refused: declined (cyber) on turn 0"),
    ]
    spent = sum(i["usd_micros"] for i in records(harness, "invocations.jsonl"))
    assert final.ledger.spent.usd_micros == spent  # the author's turn is in the ledger
