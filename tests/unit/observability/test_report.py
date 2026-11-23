"""The run report and its timeline, re-derived from events by the state machine."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from msa.core import start, transition
from msa.core.effects import AuthorAcceptanceTests
from msa.core.events import (
    AcceptanceTestsAuthored,
    BuildIntegrated,
    CancelRequested,
    Completion,
    ContextLoaded,
    EventBase,
    ImplementationCompleted,
    IntegrationTestsPassed,
    PlanProduced,
    ReleaseApproved,
    ReleasePublished,
    ReleaseVerified,
    RequestValidated,
    ReviewProduced,
    RunAdmitted,
    SecurityReviewPassed,
    StaticAnalysisPassed,
    UnitTestsPassed,
)
from msa.observability.report import Gates, Review, render, timeline
from tests.support.factory import BASE, BUILD, PERSON, T0, bundle, digest, plan_ref, run_started


def happy_events() -> list[EventBase]:
    """A whole run, completions in the order a live run produces them."""
    first = run_started()
    events: list[EventBase] = [first]
    state = start(first).state
    at = T0

    def answer(make: type[Completion], **fields: Any) -> None:
        nonlocal state, at
        at += timedelta(seconds=1)
        effect = next(e for e in state.pending if e.command.kind in make.completes)
        event = make(at=at, effect_id=effect.id, **fields)
        events.append(event)
        state = transition(state, event).state

    answer(RunAdmitted)
    answer(RequestValidated)
    answer(ContextLoaded, base_tree=BASE, context=digest("ctx"))
    answer(PlanProduced, plan=plan_ref())
    answer(AcceptanceTestsAuthored)
    answer(ImplementationCompleted)
    answer(BuildIntegrated, head_tree=BUILD)
    answer(StaticAnalysisPassed, tree=BUILD)
    answer(UnitTestsPassed, tree=BUILD)
    answer(IntegrationTestsPassed, tree=BUILD)
    answer(SecurityReviewPassed, tree=BUILD, report=digest("sec"))
    answer(ReviewProduced, tree=BUILD, findings=())
    answer(ReleaseVerified, tree=BUILD, bundle=bundle(BUILD))
    answer(ReleaseApproved, by=PERSON)
    answer(ReleasePublished, tree=BUILD, reference="aegis/run-1 at abc")
    return events


def test_the_timeline_lists_every_transition_and_what_it_issued() -> None:
    lines = timeline(happy_events())
    assert lines[0] == "  1  START -> CREATED  RunStarted  [admit_run]"
    assert lines[4] == (
        "  5  PLANNING -> IMPLEMENTING  PlanProduced  [implement_plan, author_acceptance_tests]"
    )
    assert lines[-3] == (
        " 14  FINAL_VERIFICATION -> HUMAN_APPROVAL  ReleaseVerified  [await_release_approval]"
    )
    assert lines[-2] == " 15  HUMAN_APPROVAL -> HUMAN_APPROVAL  ReleaseApproved  [publish_release]"
    assert lines[-1] == " 16  HUMAN_APPROVAL -> COMPLETED  ReleasePublished  [publish_outcome]"
    assert len(lines) == 16


def test_the_timeline_marks_escapes() -> None:
    events = happy_events()[:5]
    events.append(CancelRequested(at=T0 + timedelta(minutes=1), reason="stop"))
    assert timeline(events)[-1] == (
        "  6  IMPLEMENTING -> CANCELLED  CancelRequested (escape)  "
        "[cancel_effects, publish_outcome]"
    )


def test_a_journal_must_begin_with_run_started() -> None:
    with pytest.raises(ValueError, match="begins with RunStarted"):
        timeline(happy_events()[1:])


def test_the_report_names_stand_ins_and_holds_the_patch() -> None:
    events = happy_events()
    state = start(run_started()).state
    for event in events[1:]:
        state = transition(state, event).state
    text = render(
        state,
        events,
        task="Add farewell.",
        plan="## Plan\n\nDo it.\n",
        invocations=[
            {
                "effect": "run-1:2:0",
                "role": "planner",
                "status": "ok",
                "turns": 2,
                "replayed": 0,
                "usd_micros": 1500,
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
            },
        ],
        verifications=[{"tree": BUILD, "passed": True, "failed": [], "report": "sha256:x"}],
        summaries=[
            {
                "effect": "run-1:3:0",
                "kind": "implement_plan",
                "summary": "Added it.",
                "files_changed": ["src/greeter.py"],
                "checks_run": [],
                "open_issues": [],
            }
        ],
        stand_ins={
            AuthorAcceptanceTests.model_fields["kind"].default: "Tester (M7): none",
            "request_arbitration": "never issued here",
        },
        patch="diff --git a/x b/x\n+```\n",
    )
    assert text.startswith("# Run run-1\n\n**Outcome:** COMPLETED: completed\n")
    assert f"**Release approved by:** {PERSON}" in text
    assert "**Waiting on people:** 0:00:01 (not counted against the wall clock)" in text
    assert "- `author_acceptance_tests`: Tester (M7): none" in text
    assert "request_arbitration" not in text  # only stand-ins the run used are listed
    assert "## Plan\n\nDo it." in text
    assert "| `run-1:2:0` | planner | ok | 2 | 0 | $0.0015 | 10 | 5 | 0 |" in text
    assert "- Files: src/greeter.py" in text
    assert f"- `{BUILD}`: passed" in text
    assert "````diff\ndiff --git a/x b/x\n+```\n````" in text  # a fence the patch cannot close


# ---------- the review section


def _finding(
    fid: str, severity: str = "blocker", admitted: bool = True, reason: str = ""
) -> dict[str, object]:
    return {
        "id": fid,
        "severity": severity,
        "admitted": admitted,
        "reason": reason,
        "finding": {
            "kind": "correctness",
            "claim": "a | b\nwrong",
            "location": {"path": "src/a.py", "start_line": 3, "end_line": 4},
        },
    }


def _rendered(review: Review) -> str:
    events = happy_events()
    state = start(run_started()).state
    for event in events[1:]:
        state = transition(state, event).state
    return render(
        state,
        events,
        task="t",
        plan=None,
        invocations=[],
        verifications=[],
        summaries=[],
        stand_ins={},
        patch=None,
        review=review,
    )


def test_the_review_section_says_how_every_finding_ended() -> None:
    text = _rendered(
        Review(
            reviews=[{"cycle": 0, "tree": BUILD, "raised": 5, "admitted": 4, "summary": "ok"}],
            findings=[
                _finding("c0-1"),
                _finding("c0-2", admitted=False, reason="off the change"),
                _finding("c0-3", severity="minor", reason="the Tester's"),
                _finding("c0-4"),
                _finding("c0-5", severity="nit"),
            ],
            rounds=[
                {"finding": "c0-1", "round": 1, "author": "accept", "verdict": "accepted"},
                {"finding": "c0-4", "round": 1, "author": "defer", "reviewer": "maintain"},
            ],
            rulings=[{"finding": "c0-4", "rung": "human", "verdict": "deferred"}],
        )
    )
    assert f"- cycle 0 on `{BUILD}`: 5 finding(s) raised, 4 admitted. ok" in text
    assert (
        "| `c0-1` | blocker | correctness | `src/a.py:3-4` | a \\| b wrong | accepted | round 1 |"
        in text
    )
    assert "| not admitted | off the change |" in text
    where = "correctness | `src/a.py:3-4` | a \\| b wrong"
    assert f"| `c0-3` | minor | {where} | reported | the Tester's |" in text
    assert "| reported | not negotiated |" in text
    assert (
        "| `c0-4` | blocker | correctness | `src/a.py:3-4` | a \\| b wrong | deferred | human |"
        in text
    )
    assert "**Follow-ups** (deferred): `c0-4`" in text
    assert "the author accepted 50% of 2 move(s); the reviewer withdrew 0% of 1 contested." in text
    assert "**Flag:**" not in text  # two moves say little either way
    assert _rendered(Review(reviews=[], findings=[], rounds=[], rulings=[])).count("## Review") == 0


@pytest.mark.parametrize(
    ("author", "reviewer", "moves", "flag"),
    [
        ("accept", None, 5, "the author accepts nearly everything"),
        ("rebut", "withdraw", 5, "the reviewer withdraws nearly everything"),
        ("accept", None, 4, None),
        ("rebut", "maintain", 9, None),
    ],
)
def test_a_degenerate_negotiation_is_flagged_once_there_are_enough_moves(
    author: str, reviewer: str | None, moves: int, flag: str | None
) -> None:
    rounds = [
        {"finding": f"c0-{n}", "round": 1, "author": author, "reviewer": reviewer}
        for n in range(moves)
    ]
    text = _rendered(
        Review(
            reviews=[{"cycle": 0, "tree": BUILD, "raised": moves, "admitted": moves}],
            findings=[],
            rounds=rounds,
            rulings=[],
        )
    )
    if flag is None:
        assert "**Flag:**" not in text
    else:
        assert f"**Flag:** degenerate negotiation: {flag}." in text


def test_the_later_gates_and_the_release_are_reported() -> None:
    events = happy_events()
    state = start(run_started()).state
    for event in events[1:]:
        state = transition(state, event).state
    text = render(
        state,
        events,
        task="t",
        plan=None,
        invocations=[],
        verifications=[],
        summaries=[],
        stand_ins={},
        patch=None,
        gates=Gates(
            integrations=[
                {"tree": BASE, "verdict": "passed", "ran": False, "report": "sha256:a"},
                {"tree": BUILD, "verdict": "passed", "ran": True, "isolated": True, "report": "r"},
                {"tree": BASE, "verdict": "failed", "kind": "environment", "report": "e"},
            ],
            security=[
                {"tree": BASE, "verdict": "failed", "blocking": 2, "owner": "coder", "report": "s"},
                {
                    "tree": BUILD,
                    "verdict": "passed",
                    "advisory": 1,
                    "manifests": ["pyproject.toml"],
                    "report": "t",
                },
            ],
            releases=[
                {"tree": BASE, "verdict": "failed", "report": "f"},
                {"tree": BUILD, "verdict": "passed", "bundle": "b"},
                {
                    "tree": BUILD,
                    "verdict": "published",
                    "branch": "aegis/run-1",
                    "commit": "abc",
                    "approved_by": PERSON,
                },
            ],
        ),
    )
    assert f"- `{BASE}`: nothing to run: no integration suite (report `sha256:a`)" in text
    assert f"- `{BUILD}`: passed, without network (report `r`)" in text
    assert f"- `{BASE}`: failed: environment (report `e`)" in text
    assert f"- `{BASE}`: failed: 2 blocking (to the coder) (report `s`)" in text
    assert (
        f"- `{BUILD}`: passed, 1 advisory; manifests changed: pyproject.toml (report `t`)" in text
    )
    assert f"- `{BASE}`: verification failed (report `f`)" in text
    assert f"- `{BUILD}`: verified; bundle `b` (pr.md)" in text
    assert (
        f"- `{BUILD}`: published to branch `aegis/run-1` at `abc`, approved by {PERSON}. "
        "Nothing was pushed." in text
    )
