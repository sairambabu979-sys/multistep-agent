"""Behavior that mutation testing showed was not pinned down.

Each test here exists because a deliberately broken version of the core survived the suite:
operator-facing messages that could be garbled, a deadline check that could be off by one,
transition metadata that could be wrong, a clock that could stop advancing. See
``make mutation``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from msa.core import (
    InvariantViolation,
    Rejection,
    ReplayError,
    TransitionRejected,
    fold,
    replay,
    transition,
)
from msa.core.budget import Spend
from msa.core.effects import (
    AuthorAcceptanceTests,
    AwaitHumanRuling,
    AwaitPlanApproval,
    Finalize,
    ImplementPlan,
    IntegrateBuild,
    PrepareWorkspace,
    RequestArbitration,
    RequestPlan,
    RequestReview,
    ReviseChanges,
    RunNegotiationRound,
    RunTestSuite,
    RunVerification,
)
from msa.core.events import (
    AcceptanceTestsAuthored,
    AgentFailed,
    AgentRefused,
    ArbitrationRuled,
    CancelRequested,
    DeadlineExceeded,
    EventBase,
    Finalized,
    HumanRuled,
    ImplementationCompleted,
    IntakeCompleted,
    NegotiationRoundCompleted,
    PlanProduced,
    PlanRejected,
    ReviewProduced,
    RevisionCompleted,
    TestsFailed,
    TestsPassed,
    VerificationFailed,
    VerificationPassed,
)
from msa.core.ids import FindingId
from msa.core.state import BuildProgress
from msa.core.table import Edge, TransitionTable
from msa.core.values import (
    Escalation,
    FailureKind,
    OutcomeReason,
    Owner,
    Phase,
    PlanGate,
    Resolution,
    Severity,
    Triage,
    Verdict,
)
from tests.support.driver import Driver
from tests.support.factory import BASE, BUILD, T0, digest, finding, limits, plan_ref, tree

P = Phase


def _verdicts(**by_finding: Verdict) -> tuple[Resolution, ...]:
    return tuple(Resolution(finding_id=FindingId(k), verdict=v) for k, v in by_finding.items())


def _built() -> Driver:
    d = Driver()
    d.intake()
    d.plan()
    d.build()
    return d


def _disputed(escalation: Escalation = Escalation.HUMAN) -> Driver:
    d = Driver(limits(escalation=escalation))
    d.to_review()
    d.review(finding("a", Severity.BLOCKER), finding("b", Severity.MAJOR))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    return d


# ---------- rejection messages: what an operator reads when an event is refused


def _terminal() -> tuple[Driver, EventBase]:
    d = Driver()
    d.send(CancelRequested(at=d.tick(), reason="stop"))
    return d, DeadlineExceeded(at=d.tick())


def _restarted() -> tuple[Driver, EventBase]:
    d = Driver()
    return d, d.events[0]


def _duplicate() -> tuple[Driver, EventBase]:
    d = Driver()
    d.intake()
    return d, d.events[-1]


def _mismatched() -> tuple[Driver, EventBase]:
    d = Driver()
    d.intake()
    d.plan()
    return d, ImplementationCompleted(at=d.tick(), effect_id=d.effect(AuthorAcceptanceTests).id)


def _stale_verification_pass() -> tuple[Driver, EventBase]:
    d = _built()
    return d, d.answer(RunVerification, VerificationPassed, tree=BASE)


def _stale_verification_fail() -> tuple[Driver, EventBase]:
    d = _built()
    return d, d.answer(RunVerification, VerificationFailed, tree=BASE, report=digest("x"))


def _stale_tests_pass() -> tuple[Driver, EventBase]:
    d = _built()
    d.verify()
    return d, d.answer(RunTestSuite, TestsPassed, tree=BASE)


def _stale_tests_fail() -> tuple[Driver, EventBase]:
    d = _built()
    d.verify()
    triage = Triage(kind=FailureKind.IMPL_BUG, owner=Owner.CODER)
    return d, d.answer(RunTestSuite, TestsFailed, tree=BASE, triage=triage, report=digest("x"))


def _stale_review() -> tuple[Driver, EventBase]:
    d = Driver()
    d.to_review()
    return d, d.answer(RequestReview, ReviewProduced, tree=BASE, findings=())


def _stale_artifact() -> tuple[Driver, EventBase]:
    d = Driver()
    d.to_review()
    d.review()
    return d, d.answer(Finalize, Finalized, tree=BASE, artifact=digest("x"))


def _unknown_finding() -> tuple[Driver, EventBase]:
    d = _disputed()
    rs = _verdicts(a=Verdict.UPHELD, b=Verdict.UPHELD, zzz=Verdict.UPHELD)
    return d, d.answer(AwaitHumanRuling, HumanRuled, resolutions=rs)


def _illegal_verdict() -> tuple[Driver, EventBase]:
    d = _disputed()
    rs = _verdicts(a=Verdict.ACCEPTED, b=Verdict.UPHELD)
    return d, d.answer(AwaitHumanRuling, HumanRuled, resolutions=rs)


def _incomplete_human_ruling() -> tuple[Driver, EventBase]:
    d = _disputed()
    return d, d.answer(AwaitHumanRuling, HumanRuled, resolutions=_verdicts(a=Verdict.UPHELD))


def _incomplete_arbiter_ruling() -> tuple[Driver, EventBase]:
    d = _disputed(Escalation.ARBITER)
    rs = _verdicts(a=Verdict.DISMISSED)
    return d, d.answer(RequestArbitration, ArbitrationRuled, resolutions=rs)


def _stale(what: str) -> str:
    return f"stale_evidence: {what} is for tree {BASE}, head is {BUILD}"


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        (_terminal, "terminal: run is aborted"),
        (_restarted, "already_started: run run-1 exists"),
        (_duplicate, "unknown_effect: run-1:1:0 is not pending (stale or duplicate)"),
        (
            _mismatched,
            "mismatched_completion: ImplementationCompleted cannot complete "
            "author_acceptance_tests",
        ),
        (_stale_verification_pass, _stale("verification result")),
        (_stale_verification_fail, _stale("verification result")),
        (_stale_tests_pass, _stale("test result")),
        (_stale_tests_fail, _stale("test result")),
        (_stale_review, _stale("review")),
        (_stale_artifact, _stale("finalized artifact")),
        (_unknown_finding, "unknown_finding: zzz is not an open finding"),
        (_illegal_verdict, "illegal_verdict: accepted is not allowed here"),
        (_incomplete_human_ruling, "incomplete_ruling: no verdict for ['b']"),
        (_incomplete_arbiter_ruling, "incomplete_ruling: no verdict for ['b']"),
    ],
    ids=lambda v: v.__name__.strip("_") if callable(v) else "",
)
def test_rejections_say_exactly_what_was_wrong(
    scenario: Callable[[], tuple[Driver, EventBase]], message: str
) -> None:
    d, event = scenario()
    rejected = d.reject(event)
    assert str(rejected) == message
    assert rejected.detail == message.split(": ", 1)[1]


def test_a_clock_regression_names_both_times() -> None:
    d = Driver()
    d.intake()
    event = d.answer(RequestPlan, PlanProduced, plan=plan_ref(1))
    early = event.model_copy(update={"at": T0 + timedelta(milliseconds=500)})
    assert str(d.reject(early)) == (
        f"clock_regression: event at {early.at} precedes {T0 + timedelta(seconds=1)}"
    )


def test_an_event_without_a_rule_names_event_and_phase() -> None:
    d = Driver()
    event = d.answer(PrepareWorkspace, IntakeCompleted, base_tree=BASE)
    with pytest.raises(TransitionRejected) as info:
        transition(d.state, event, table=TransitionTable(()))
    assert str(info.value) == "not_accepted_in_phase: IntakeCompleted in intake"


# ---------- outcome details: what an operator reads when a run ends


def test_the_revision_limit_detail_counts_correctly() -> None:
    d = Driver(limits(max_revisions=1))
    d.intake()
    d.plan()
    d.build()
    d.complete(RunVerification, VerificationFailed, tree=BUILD, report=digest("lint"))
    d.complete(ReviseChanges, RevisionCompleted, head_tree=tree("r1"))
    d.complete(RunVerification, VerificationFailed, tree=tree("r1"), report=digest("lint"))
    assert d.state.outcome is not None
    assert d.state.outcome.detail == "revision 2 needed; limit is 1"


def test_the_replan_limit_detail_counts_correctly_and_quotes_the_feedback() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN, max_replans=1))
    d.intake()
    d.plan()
    d.complete(AwaitPlanApproval, PlanRejected, feedback="smaller")
    d.plan()
    d.complete(AwaitPlanApproval, PlanRejected, feedback="still too big")
    assert d.state.outcome is not None
    assert d.state.outcome.detail == "plan rejected 2 time(s); last feedback: still too big"


@pytest.mark.parametrize(
    ("finish", "reason", "detail"),
    [
        (
            lambda d: d.complete(
                RunTestSuite,
                TestsFailed,
                tree=BUILD,
                triage=Triage(kind=FailureKind.ENVIRONMENT, owner=None),
                report=digest("tests"),
            ),
            OutcomeReason.ENVIRONMENT_FAILURE,
            f"test report {digest('tests')}",
        ),
        (
            lambda d: d.complete(RunTestSuite, AgentRefused),
            OutcomeReason.AGENT_REFUSED,
            "uncategorized",
        ),
        (
            lambda d: d.complete(RunTestSuite, AgentFailed, error="timeout"),
            OutcomeReason.AGENT_FAILED,
            "timeout",
        ),
    ],
)
def test_failure_details_are_exact(
    finish: Callable[[Driver], object], reason: OutcomeReason, detail: str
) -> None:
    d = _built()
    d.verify()
    finish(d)
    assert d.state.outcome is not None
    assert (d.state.outcome.reason, d.state.outcome.detail) == (reason, detail)


def test_completion_names_the_artifact() -> None:
    d = Driver()
    d.to_review()
    d.review()
    d.complete(Finalize, Finalized, tree=BUILD, artifact=digest("patch"))
    assert d.state.outcome is not None
    assert d.state.outcome.detail == f"artifact {digest('patch')}"


def test_budget_details_report_the_numbers() -> None:
    overspent = Driver(limits(budget_usd_micros=1_000))
    overspent.intake()
    overspent.complete(RequestPlan, PlanProduced, plan=plan_ref(1), spend=Spend(usd_micros=1_500))
    assert overspent.state.outcome is not None
    assert overspent.state.outcome.detail == "committed 1500 of 1000 micro-USD"

    unfundable = Driver(limits(budget_usd_micros=3))
    unfundable.intake()
    unfundable.complete(RequestPlan, PlanProduced, plan=plan_ref(1), spend=Spend(usd_micros=2))
    assert unfundable.state.outcome is not None
    assert unfundable.state.outcome.detail == (
        "1 micro-USD available, 2 spending effect(s) to fund"
    )


def test_deadline_detail_names_the_deadline() -> None:
    d = Driver()
    d.send(DeadlineExceeded(at=d.tick()))
    assert d.state.outcome is not None
    assert d.state.outcome.detail == f"deadline {d.state.deadline}"


# ---------- time


def test_updated_at_tracks_the_last_accepted_event() -> None:
    d = Driver()
    d.intake()
    assert d.state.updated_at == d.events[-1].at
    d.plan()
    assert d.state.updated_at == d.events[-1].at


def test_clock_regression_is_judged_against_the_last_event_not_the_start() -> None:
    d = Driver()
    d.intake()
    d.tick(60)
    d.plan()  # now at T0 + 62s
    late_but_not_latest = d.answer(ImplementPlan, ImplementationCompleted)
    late_but_not_latest = late_but_not_latest.model_copy(update={"at": T0 + timedelta(seconds=30)})
    assert d.reject(late_but_not_latest).reason == "clock_regression"


def test_an_event_exactly_at_the_deadline_is_still_on_time() -> None:
    d = Driver(limits(wall_clock=timedelta(minutes=5)))
    on_time = d.answer(PrepareWorkspace, IntakeCompleted, base_tree=BASE)
    d.send(on_time.model_copy(update={"at": d.state.deadline}))
    assert d.phase is P.PLANNING
    late = d.answer(RequestPlan, PlanProduced, plan=plan_ref(1))
    d.send(late.model_copy(update={"at": d.state.deadline + timedelta(microseconds=1)}))
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.DEADLINE_EXCEEDED


# ---------- transition metadata


def test_edges_describe_rule_transitions_and_escapes_accurately() -> None:
    d = Driver()
    assert d.steps[0].edge == Edge(None, "RunStarted", P.INTAKE, escape=False)
    d.intake()
    assert d.steps[-1].edge == Edge(P.INTAKE, "IntakeCompleted", P.PLANNING, escape=False)
    d.plan()
    d.complete(ImplementPlan, AgentFailed, error="x")
    assert d.steps[-1].edge == Edge(P.BUILDING, "AgentFailed", P.FAILED, escape=True)

    cancelled = Driver()
    cancelled.intake()
    cancelled.send(CancelRequested(at=cancelled.tick(), reason="x"))
    assert cancelled.steps[-1].edge == Edge(P.PLANNING, "CancelRequested", P.ABORTED, escape=True)

    unfundable = Driver(limits(budget_usd_micros=3))
    unfundable.intake()
    unfundable.complete(RequestPlan, PlanProduced, plan=plan_ref(1), spend=Spend(usd_micros=2))
    assert unfundable.steps[-1].edge == Edge(P.PLANNING, "PlanProduced", P.FAILED, escape=True)


def test_replay_and_fold_use_the_table_they_are_given() -> None:
    d = Driver()
    d.intake()
    empty = TransitionTable(())
    with pytest.raises(ReplayError, match="not_accepted_in_phase"):
        replay(d.events, table=empty)
    with pytest.raises(ReplayError, match="not_accepted_in_phase"):
        fold(d.steps[0].state, d.events[1:], table=empty)


# ---------- state that must be recorded


def test_build_progress_is_recorded_region_by_region() -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.complete(ImplementPlan, ImplementationCompleted)
    assert d.state.build == BuildProgress(implementation_done=True, acceptance_tests_done=False)
    d.complete(AuthorAcceptanceTests, AcceptanceTestsAuthored)
    assert d.state.build == BuildProgress(implementation_done=True, acceptance_tests_done=True)
    assert d.effect(IntegrateBuild).command == IntegrateBuild(base_tree=BASE)


def test_a_test_failure_revision_carries_the_report() -> None:
    d = _built()
    d.verify()
    triage = Triage(kind=FailureKind.TEST_BUG, owner=Owner.TESTER)
    d.complete(RunTestSuite, TestsFailed, tree=BUILD, triage=triage, report=digest("tests"))
    assert d.effect(ReviseChanges).command == ReviseChanges(
        owner=Owner.TESTER, tree=BUILD, failure_report=digest("tests")
    )


def test_error_types_render_their_parts() -> None:
    assert str(InvariantViolation(("a", "b"))) == "a; b"
    rejected = TransitionRejected(Rejection.TERMINAL, "run is done")
    assert (str(rejected), rejected.reason, rejected.detail) == (
        "terminal: run is done",
        Rejection.TERMINAL,
        "run is done",
    )


@pytest.mark.parametrize(
    ("accessor", "message"),
    [
        ("require_head", "head_tree is not set before BUILDING completes"),
        ("require_base", "base_tree is not set before INTAKE completes"),
        ("require_plan", "plan is not set before PLANNING completes"),
    ],
)
def test_missing_facts_are_named_exactly(accessor: str, message: str) -> None:
    with pytest.raises(InvariantViolation) as info:
        getattr(Driver().state, accessor)()
    assert str(info.value) == message
