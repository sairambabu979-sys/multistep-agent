"""Example-based tests: one readable scenario per behavior of the state machine."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from msa.core import (
    InvariantViolation,
    Rejection,
    ReplayError,
    RunState,
    Step,
    TransitionRejected,
    fold,
    replay,
    start,
    transition,
)
from msa.core.budget import Spend
from msa.core.effects import (
    AdmitRun,
    AuthorAcceptanceTests,
    AwaitHumanRuling,
    AwaitPlanApproval,
    AwaitReleaseApproval,
    AwaitResume,
    AwaitUnblock,
    CancelEffects,
    Command,
    ImplementPlan,
    IntegrateBuild,
    LoadContext,
    PublishOutcome,
    PublishRelease,
    RequestArbitration,
    RequestPlan,
    RequestReview,
    ReviseChanges,
    RunIntegrationTests,
    RunNegotiationRound,
    RunSecurityReview,
    RunStaticAnalysis,
    RunUnitTests,
    ValidateRequest,
    VerifyRelease,
)
from msa.core.events import (
    AcceptanceTestsAuthored,
    AgentFailed,
    AgentRefused,
    ArbitrationRuled,
    CancelRequested,
    Completion,
    ContextLoaded,
    DeadlineExceeded,
    EffectFailed,
    HumanAborted,
    HumanRuled,
    ImplementationCompleted,
    IntegrationTestsFailed,
    NegotiationRoundCompleted,
    PauseRequested,
    PlanApproved,
    PlanProduced,
    PlanRejected,
    ReleaseApproved,
    ReleasePublished,
    ReleaseRejected,
    ReleaseVerificationFailed,
    ReleaseVerified,
    RequestRejected,
    RequestValidated,
    Resumed,
    RevisionCompleted,
    RunAdmitted,
    RunDenied,
    SecurityReviewFailed,
    StaticAnalysisFailed,
    StaticAnalysisPassed,
    Unblocked,
    UnitTestsFailed,
    UnitTestsPassed,
)
from msa.core.ids import EffectId, FindingId
from msa.core.state import FindingStatus
from msa.core.table import Decision, Rule, TransitionTable
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
from tests.support.factory import (
    BASE,
    BUILD,
    PERSON,
    T0,
    bundle,
    digest,
    finding,
    limits,
    plan_ref,
    run_started,
    tree,
)

P = Phase


def _verdicts(**by_finding: Verdict) -> tuple[Resolution, ...]:
    return tuple(Resolution(finding_id=FindingId(k), verdict=v) for k, v in by_finding.items())


# ---------- the happy path


def test_happy_path_visits_every_stage_in_order_and_publishes_the_outcome() -> None:
    d = Driver()
    d.to_review()
    d.review()
    d.release()

    assert d.phases() == [
        P.CREATED,
        P.VALIDATING,
        P.CONTEXT,
        P.PLANNING,
        P.IMPLEMENTING,  # plan produced; both regions start
        P.IMPLEMENTING,  # implementation done
        P.IMPLEMENTING,  # acceptance tests done -> integrate
        P.STATIC_ANALYSIS,
        P.UNIT_TESTING,
        P.INTEGRATION_TESTING,
        P.SECURITY_REVIEW,
        P.CODE_REVIEW,
        P.FINAL_VERIFICATION,
        P.HUMAN_APPROVAL,  # release verified; a person is asked
        P.HUMAN_APPROVAL,  # approved; publishing
        P.COMPLETED,
    ]
    assert d.state.outcome is not None
    assert (d.state.outcome.reason, d.state.outcome.detail) == (
        OutcomeReason.COMPLETED,
        "aegis/run-1",
    )
    assert [e.command.kind for e in d.steps[-1].effects] == ["publish_outcome"]
    assert d.state.pending == ()
    assert d.state.ledger.reservations == ()
    assert (d.state.release, d.state.approved_by) == (bundle(BUILD), PERSON)


def test_effects_carry_the_inputs_their_executors_need() -> None:
    d = Driver()
    assert d.effect(AdmitRun).command == AdmitRun()
    d.intake()
    assert d.state.base_tree == BASE
    d.plan()
    assert d.effect(ImplementPlan).command == ImplementPlan(plan=plan_ref(1), base_tree=BASE)
    assert d.effect(AuthorAcceptanceTests).command == AuthorAcceptanceTests(
        plan=plan_ref(1), base_tree=BASE
    )
    d.build()
    assert d.effect(RunStaticAnalysis).command == RunStaticAnalysis(tree=BUILD)
    d.static()
    assert d.effect(RunUnitTests).command == RunUnitTests(tree=BUILD, base_tree=BASE)
    d.unit()
    assert d.effect(RunIntegrationTests).command == RunIntegrationTests(tree=BUILD)
    d.integration()
    assert d.effect(RunSecurityReview).command == RunSecurityReview(tree=BUILD)
    d.security()
    assert d.effect(RequestReview).command == RequestReview(tree=BUILD, cycle=0)
    d.review()
    assert d.effect(VerifyRelease).command == VerifyRelease(tree=BUILD)
    d.verify_release()
    assert d.effect(AwaitReleaseApproval).command == AwaitReleaseApproval(
        tree=BUILD, bundle=bundle(BUILD)
    )
    d.approve_release()
    assert d.effect(PublishRelease).command == PublishRelease(tree=BUILD, bundle=bundle(BUILD))


def test_effect_ids_are_deterministic_functions_of_the_journal() -> None:
    first, second = Driver(), Driver()
    for d in (first, second):
        d.to_review()
    assert [e.id for s in first.steps for e in s.effects] == [
        e.id for s in second.steps for e in s.effects
    ]
    assert first.steps[1].effects[0].id == EffectId("run-1:2:0")


# ---------- admission, validation, context


def test_a_denied_admission_fails_the_run_before_any_work() -> None:
    d = Driver()
    d.complete(AdmitRun, RunDenied, reason="concurrent run quota reached")
    assert d.state.outcome is not None
    assert (d.state.outcome.reason, d.state.outcome.detail) == (
        OutcomeReason.ADMISSION_DENIED,
        "concurrent run quota reached",
    )
    assert d.state.ledger.spent.usd_micros == 0


def test_a_rejected_request_fails_the_run() -> None:
    d = Driver()
    d.complete(AdmitRun, RunAdmitted)
    d.expect(P.VALIDATING)
    d.complete(ValidateRequest, RequestRejected, reason="not a git repository")
    assert d.state.outcome is not None
    assert (d.state.outcome.reason, d.state.outcome.detail) == (
        OutcomeReason.REQUEST_REJECTED,
        "not a git repository",
    )


def test_context_is_loaded_before_planning() -> None:
    d = Driver()
    d.complete(AdmitRun, RunAdmitted)
    d.complete(ValidateRequest, RequestValidated)
    d.expect(P.CONTEXT)
    assert d.effect(LoadContext).command == LoadContext()
    assert d.state.base_tree is None
    d.complete(LoadContext, ContextLoaded, base_tree=BASE, context=digest("ctx"))
    d.expect(P.PLANNING)
    assert d.effect(RequestPlan).command == RequestPlan(attempt=1)


# ---------- planning and the plan gate


def test_human_plan_gate_waits_for_approval() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN))
    d.intake()
    d.plan()
    d.expect(P.PLAN_APPROVAL)
    assert d.effect(AwaitPlanApproval).command == AwaitPlanApproval(plan=plan_ref(1))
    d.complete(AwaitPlanApproval, PlanApproved, by=PERSON)
    d.expect(P.IMPLEMENTING)


def test_rejected_plan_is_replanned_with_feedback_until_the_limit() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN, max_replans=1))
    d.intake()
    d.plan()
    d.complete(AwaitPlanApproval, PlanRejected, by=PERSON, feedback="smaller steps")
    d.expect(P.PLANNING)
    assert d.state.plan is None
    assert d.effect(RequestPlan).command == RequestPlan(attempt=2, feedback="smaller steps")

    d.plan()
    assert d.steps[-1].state.plan == plan_ref(2)  # distinct expression: no stale narrowing
    d.complete(AwaitPlanApproval, PlanRejected, by=PERSON, feedback="still too big")
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.REPLAN_LIMIT


def test_human_can_abort_at_the_plan_gate() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN))
    d.intake()
    d.plan()
    d.complete(AwaitPlanApproval, HumanAborted, by=PERSON, reason="wrong task")
    d.expect(P.CANCELLED)
    assert d.state.outcome is not None
    assert d.state.outcome.detail == f"{PERSON}: wrong task"


# ---------- no agent approves


@pytest.mark.parametrize(
    ("event_type", "fields"),
    [
        (PlanApproved, {}),
        (PlanRejected, {"feedback": "x"}),
        (HumanRuled, {"resolutions": ()}),
        (ReleaseApproved, {}),
        (ReleaseRejected, {"feedback": "x"}),
        (Unblocked, {}),
        (Resumed, {}),
        (HumanAborted, {}),
    ],
)
@pytest.mark.parametrize("principal", ["agent:coder", "agent:release-verifier", ""])
def test_no_agent_and_no_one_anonymous_can_answer_a_human_gate(
    event_type: type[Completion], fields: dict[str, object], principal: str
) -> None:
    with pytest.raises(ValidationError):
        event_type.model_validate({"at": T0, "effect_id": "run-1:1:0", "by": principal, **fields})


def test_no_agent_can_pause_a_run() -> None:
    with pytest.raises(ValidationError):
        PauseRequested(at=T0, by="agent:orchestrator")


# ---------- implementing: two parallel regions and a join


@pytest.mark.parametrize("implementation_first", [True, False])
def test_build_regions_join_in_either_order(implementation_first: bool) -> None:
    d = Driver()
    d.intake()
    d.plan()
    order: list[tuple[type[Command], type[Completion]]] = [
        (ImplementPlan, ImplementationCompleted),
        (AuthorAcceptanceTests, AcceptanceTestsAuthored),
    ]
    if not implementation_first:
        order.reverse()

    d.complete(*order[0])
    d.expect(P.IMPLEMENTING)
    assert not any(isinstance(e.command, IntegrateBuild) for e in d.state.pending)

    d.complete(*order[1])
    assert d.effect(IntegrateBuild).command == IntegrateBuild(base_tree=BASE)


def test_parallel_regions_get_disjoint_halves_of_the_remaining_budget() -> None:
    d = Driver(limits(budget_usd_micros=1_000_001))
    d.intake()
    d.complete(RequestPlan, PlanProduced, plan=plan_ref(1), spend=Spend(usd_micros=1))
    caps = [e.budget_usd_micros for e in d.steps[-1].effects]
    assert caps == [500_000, 500_000]
    assert d.state.ledger.committed_usd_micros == 1_000_001


# ---------- the mandatory gates, and evidence binding


@pytest.mark.parametrize(
    ("passed", "command_type", "failure_type", "fields"),
    [
        (0, RunStaticAnalysis, StaticAnalysisFailed, {"report": digest("lint")}),
        (
            3,
            RunSecurityReview,
            SecurityReviewFailed,
            {"report": digest("secret")},
        ),
    ],
)
def test_a_failed_deterministic_gate_sends_the_coder_back_with_the_report(
    passed: int,
    command_type: type[Command],
    failure_type: type[Completion],
    fields: dict[str, object],
) -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.build()
    for gate in (d.static, d.unit, d.integration)[:passed]:
        gate()
    d.complete(command_type, failure_type, tree=BUILD, **fields)
    d.expect(P.REWORK)
    assert d.effect(ReviseChanges).command == ReviseChanges(
        owner=Owner.CODER, tree=BUILD, failure_report=fields["report"]
    )


def test_a_security_failure_never_reaches_negotiation() -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.build()
    d.static()
    d.unit()
    d.integration()
    d.complete(RunSecurityReview, SecurityReviewFailed, tree=BUILD, report=digest("secret"))
    assert d.state.negotiation_round == 0
    assert d.state.findings == ()
    assert not any(
        isinstance(e.command, (RunNegotiationRound, RequestArbitration)) for e in d.state.pending
    )


def test_a_revision_reruns_every_gate_from_static_analysis() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    d.complete(
        RunNegotiationRound,
        NegotiationRoundCompleted,
        resolutions=_verdicts(f1=Verdict.ACCEPTED),
    )
    fixed = tree("fixed")
    d.complete(ReviseChanges, RevisionCompleted, head_tree=fixed)
    d.expect(P.STATIC_ANALYSIS)
    assert d.effect(RunStaticAnalysis).command == RunStaticAnalysis(tree=fixed)
    assert not d.state.evidence.holds_through("static", fixed)
    d.gates()
    d.expect(P.CODE_REVIEW)
    assert d.state.evidence.holds_through("security", fixed)


def test_evidence_for_an_old_tree_is_rejected_at_every_gate() -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.build()
    d.complete(RunStaticAnalysis, StaticAnalysisFailed, tree=BUILD, report=digest("lint"))
    revised = tree("revised")
    d.complete(ReviseChanges, RevisionCompleted, head_tree=revised)
    stale = d.answer(RunStaticAnalysis, StaticAnalysisPassed, tree=BUILD)
    assert d.reject(stale).reason is Rejection.STALE_EVIDENCE
    d.static()
    stale_unit = d.answer(RunUnitTests, UnitTestsPassed, tree=BUILD)
    assert d.reject(stale_unit).reason is Rejection.STALE_EVIDENCE


@pytest.mark.parametrize(
    ("kind", "owner"),
    [
        (FailureKind.IMPL_BUG, Owner.CODER),
        (FailureKind.TEST_BUG, Owner.TESTER),
        (FailureKind.FLAKY, Owner.TESTER),
    ],
)
@pytest.mark.parametrize("stage", ["unit", "integration"])
def test_test_failures_are_routed_to_the_triaged_owner(
    kind: FailureKind, owner: Owner, stage: str
) -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.build()
    d.static()
    triage = Triage(kind=kind, owner=owner)
    if stage == "unit":
        d.complete(RunUnitTests, UnitTestsFailed, tree=BUILD, triage=triage, report=digest("unit"))
    else:
        d.unit()
        d.complete(
            RunIntegrationTests,
            IntegrationTestsFailed,
            tree=BUILD,
            triage=triage,
            report=digest("unit"),
        )
    d.expect(P.REWORK)
    assert d.effect(ReviseChanges).command == ReviseChanges(
        owner=owner, tree=BUILD, failure_report=digest("unit")
    )


def test_revisions_are_capped() -> None:
    d = Driver(limits(max_revisions=1))
    d.intake()
    d.plan()
    d.build()
    d.complete(RunStaticAnalysis, StaticAnalysisFailed, tree=BUILD, report=digest("lint"))
    d.complete(ReviseChanges, RevisionCompleted, head_tree=tree("r1"))
    d.complete(RunStaticAnalysis, StaticAnalysisFailed, tree=tree("r1"), report=digest("lint"))
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.REVISION_LIMIT
    assert d.state.revisions == 1


# ---------- BLOCKED on the environment


ENVIRONMENT = Triage(kind=FailureKind.ENVIRONMENT, owner=None)


def _blocked_in_integration(d: Driver) -> None:
    d.intake()
    d.plan()
    d.build()
    d.static()
    d.unit()
    d.complete(
        RunIntegrationTests,
        IntegrationTestsFailed,
        tree=BUILD,
        triage=ENVIRONMENT,
        report=digest("no postgres"),
    )


def test_an_environment_failure_blocks_the_stage_instead_of_failing_the_run() -> None:
    d = Driver()
    _blocked_in_integration(d)
    d.expect(P.BLOCKED)
    assert d.state.outcome is None
    assert d.effect(AwaitUnblock).command == AwaitUnblock(
        reason=f"the integration_testing stage could not run: {digest('no postgres')}"
    )
    assert (d.state.resume_phase, d.state.resume_commands) == (
        P.INTEGRATION_TESTING,
        (RunIntegrationTests(tree=BUILD),),
    )
    assert d.state.revisions == 0  # nobody's fault: no revision is spent


def test_unblocking_reruns_the_stage_without_redoing_earlier_work() -> None:
    d = Driver()
    _blocked_in_integration(d)
    before = d.state.ledger.spent
    d.complete(AwaitUnblock, Unblocked, by=PERSON, note="started postgres")
    d.expect(P.INTEGRATION_TESTING)
    assert d.effect(RunIntegrationTests).command == RunIntegrationTests(tree=BUILD)
    assert (d.state.resume_phase, d.state.resume_commands) == (None, ())
    assert d.state.ledger.spent == before
    assert d.state.evidence.holds_through("unit", BUILD)
    d.integration()
    d.expect(P.SECURITY_REVIEW)


def test_a_unit_test_stage_blocked_on_the_environment_retries_against_the_same_base() -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.build()
    d.static()
    d.complete(
        RunUnitTests, UnitTestsFailed, tree=BUILD, triage=ENVIRONMENT, report=digest("no uv")
    )
    d.expect(P.BLOCKED)
    d.complete(AwaitUnblock, Unblocked, by=PERSON)
    d.expect(P.UNIT_TESTING)
    assert d.effect(RunUnitTests).command == RunUnitTests(tree=BUILD, base_tree=BASE)


def test_a_person_can_give_up_on_a_blocked_run() -> None:
    d = Driver()
    _blocked_in_integration(d)
    d.complete(AwaitUnblock, HumanAborted, by=PERSON, reason="no database here")
    d.expect(P.CANCELLED)
    assert (d.state.resume_phase, d.state.resume_commands) == (None, ())


def test_time_blocked_on_the_environment_does_not_count_against_the_wall_clock() -> None:
    d = Driver()
    _blocked_in_integration(d)
    asked, original = d.state.updated_at, d.state.deadline
    d.tick(HOUR)
    d.complete(AwaitUnblock, Unblocked, by=PERSON)
    assert d.state.outcome is None
    assert d.state.deadline == original + (d.state.updated_at - asked)


# ---------- review and negotiation


def test_advisory_findings_never_block_release() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("style", Severity.NIT), finding("naming", Severity.MINOR))
    d.expect(P.FINAL_VERIFICATION)
    assert {f.status for f in d.state.findings} == {FindingStatus.ADVISORY}


def test_accepted_findings_lead_to_a_revision_and_a_fresh_review() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER), finding("f2", Severity.NIT))
    d.expect(P.NEGOTIATION)
    assert d.effect(RunNegotiationRound).command == RunNegotiationRound(
        tree=BUILD, round=1, findings=(FindingId("f1"),)
    )

    d.complete(
        RunNegotiationRound,
        NegotiationRoundCompleted,
        resolutions=_verdicts(f1=Verdict.ACCEPTED),
    )
    assert d.effect(ReviseChanges).command == ReviseChanges(
        owner=Owner.CODER, tree=BUILD, findings=(FindingId("f1"),)
    )

    d.complete(ReviseChanges, RevisionCompleted, head_tree=tree("fixed"))
    d.gates()
    assert d.effect(RequestReview).command == RequestReview(tree=tree("fixed"), cycle=1)
    assert d.state.negotiation_round == 0


def test_withdrawn_findings_go_to_final_verification_without_a_revision() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.MAJOR))
    d.complete(
        RunNegotiationRound,
        NegotiationRoundCompleted,
        resolutions=_verdicts(f1=Verdict.WITHDRAWN),
    )
    d.expect(P.FINAL_VERIFICATION)


def _to_human(d: Driver) -> None:
    """A round without progress goes to arbitration first; its objective rungs (a repro
    test, the preference store, the style rule) decide nothing, so a person is asked."""
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.complete(RequestArbitration, ArbitrationRuled, resolutions=())


def test_a_round_without_progress_goes_to_the_objective_rungs_then_to_a_person() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.expect(P.NEGOTIATION)
    assert d.effect(RequestArbitration).command == RequestArbitration(
        tree=BUILD, findings=(FindingId("f1"),), arbiter=False
    )
    d.complete(RequestArbitration, ArbitrationRuled, resolutions=())
    d.expect(P.BLOCKED)
    assert d.effect(AwaitHumanRuling).command == AwaitHumanRuling(
        tree=BUILD, findings=(FindingId("f1"),)
    )
    assert d.state.resume_phase is None  # a ruling decides the way on; nothing resumes


def test_a_person_is_asked_only_about_what_the_objective_rungs_left_open() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("a", Severity.BLOCKER), finding("b", Severity.MAJOR))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.complete(RequestArbitration, ArbitrationRuled, resolutions=_verdicts(a=Verdict.DISMISSED))
    d.expect(P.BLOCKED)
    assert d.effect(AwaitHumanRuling).command == AwaitHumanRuling(
        tree=BUILD, findings=(FindingId("b"),)
    )
    d.complete(AwaitHumanRuling, HumanRuled, by=PERSON, resolutions=_verdicts(b=Verdict.UPHELD))
    d.expect(P.REWORK)
    assert d.effect(ReviseChanges).command == ReviseChanges(
        owner=Owner.CODER, tree=BUILD, findings=(FindingId("b"),)
    )


@pytest.mark.parametrize(
    ("verdict", "phase"), [(Verdict.UPHELD, P.REWORK), (Verdict.DISMISSED, P.FINAL_VERIFICATION)]
)
def test_when_the_objective_rungs_decide_everything_no_person_is_asked(
    verdict: Verdict, phase: Phase
) -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.complete(RequestArbitration, ArbitrationRuled, resolutions=_verdicts(f1=verdict))
    d.expect(phase)
    assert not any(isinstance(e.command, AwaitHumanRuling) for e in d.state.pending)


def test_a_partial_ruling_uses_ruling_verdicts_on_open_findings_only() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("a", Severity.BLOCKER), finding("b", Severity.MAJOR))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    accepted = d.answer(
        RequestArbitration, ArbitrationRuled, resolutions=_verdicts(a=Verdict.ACCEPTED)
    )
    assert d.reject(accepted).reason is Rejection.ILLEGAL_VERDICT
    unknown = d.answer(
        RequestArbitration, ArbitrationRuled, resolutions=_verdicts(z=Verdict.UPHELD)
    )
    assert d.reject(unknown).reason is Rejection.UNKNOWN_FINDING


def test_with_no_revisions_left_a_person_is_still_asked_while_no_change_is_demanded() -> None:
    d = Driver(limits(max_revisions=0))
    d.to_review()
    d.review(finding("a", Severity.BLOCKER), finding("b", Severity.MAJOR))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.complete(RequestArbitration, ArbitrationRuled, resolutions=_verdicts(a=Verdict.DISMISSED))
    d.expect(P.BLOCKED)  # dismissing "b" could still let the run finish


def test_a_partial_ruling_that_demands_a_change_with_no_revisions_left_fails_fast() -> None:
    d = Driver(limits(max_revisions=0))
    d.to_review()
    d.review(finding("a", Severity.BLOCKER), finding("b", Severity.MAJOR))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.complete(RequestArbitration, ArbitrationRuled, resolutions=_verdicts(a=Verdict.UPHELD))
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.REVISION_LIMIT


def test_rounds_that_progress_still_stop_at_the_round_limit() -> None:
    d = Driver(limits(max_negotiation_rounds=2))
    d.to_review()
    d.review(
        finding("a", Severity.BLOCKER),
        finding("b", Severity.BLOCKER),
        finding("c", Severity.BLOCKER),
    )
    d.complete(
        RunNegotiationRound, NegotiationRoundCompleted, resolutions=_verdicts(a=Verdict.WITHDRAWN)
    )
    assert d.effect(RunNegotiationRound).command == RunNegotiationRound(
        tree=BUILD, round=2, findings=(FindingId("b"), FindingId("c"))
    )
    d.complete(
        RunNegotiationRound, NegotiationRoundCompleted, resolutions=_verdicts(b=Verdict.ACCEPTED)
    )
    d.expect(P.NEGOTIATION)
    assert d.effect(RequestArbitration).command == RequestArbitration(
        tree=BUILD, findings=(FindingId("c"),), arbiter=False
    )
    assert d.state.negotiation_round == 2


def test_a_ruling_upholding_a_finding_triggers_a_revision() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    _to_human(d)
    d.complete(AwaitHumanRuling, HumanRuled, by=PERSON, resolutions=_verdicts(f1=Verdict.UPHELD))
    d.expect(P.REWORK)


def test_a_ruling_dismissing_every_finding_goes_to_final_verification() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    _to_human(d)
    d.complete(AwaitHumanRuling, HumanRuled, by=PERSON, resolutions=_verdicts(f1=Verdict.DISMISSED))
    d.expect(P.FINAL_VERIFICATION)


def test_rulings_must_cover_every_open_finding_and_use_ruling_verdicts() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("a", Severity.BLOCKER), finding("b", Severity.MAJOR))
    _to_human(d)

    partial = d.answer(
        AwaitHumanRuling, HumanRuled, by=PERSON, resolutions=_verdicts(a=Verdict.UPHELD)
    )
    assert d.reject(partial).reason is Rejection.INCOMPLETE_RULING

    accepted = d.answer(
        AwaitHumanRuling,
        HumanRuled,
        by=PERSON,
        resolutions=_verdicts(a=Verdict.ACCEPTED, b=Verdict.DISMISSED),
    )
    assert d.reject(accepted).reason is Rejection.ILLEGAL_VERDICT

    unknown = d.answer(
        AwaitHumanRuling,
        HumanRuled,
        by=PERSON,
        resolutions=_verdicts(a=Verdict.UPHELD, b=Verdict.UPHELD, zzz=Verdict.UPHELD),
    )
    assert d.reject(unknown).reason is Rejection.UNKNOWN_FINDING


def test_arbiter_escalation_stays_in_negotiation_until_the_ruling() -> None:
    d = Driver(limits(escalation=Escalation.ARBITER))
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.expect(P.NEGOTIATION)
    assert d.effect(RequestArbitration).command == RequestArbitration(
        tree=BUILD, findings=(FindingId("f1"),), arbiter=True
    )
    d.complete(RequestArbitration, ArbitrationRuled, resolutions=_verdicts(f1=Verdict.DISMISSED))
    d.expect(P.FINAL_VERIFICATION)


def test_a_person_can_abort_instead_of_ruling() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    _to_human(d)
    d.complete(AwaitHumanRuling, HumanAborted, by=PERSON, reason="rethink the approach")
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.HUMAN_ABORTED


def test_a_demanded_change_with_no_revisions_left_fails_fast() -> None:
    """Without this, the run would keep negotiating (and page a person) towards an outcome
    that is already decided. Regression for a liveness bug found by the model checker."""
    d = Driver(limits(max_revisions=0))
    d.to_review()
    d.review(finding("a", Severity.BLOCKER), finding("b", Severity.BLOCKER))
    d.complete(
        RunNegotiationRound, NegotiationRoundCompleted, resolutions=_verdicts(a=Verdict.ACCEPTED)
    )
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.REVISION_LIMIT
    assert not any(isinstance(e.command, RunNegotiationRound) for e in d.steps[-1].effects)


# ---------- final verification, approval, publishing


def _to_release(d: Driver) -> None:
    d.to_review()
    d.review()


def test_a_failed_release_verification_goes_back_to_the_coder() -> None:
    d = Driver()
    _to_release(d)
    d.complete(VerifyRelease, ReleaseVerificationFailed, tree=BUILD, report=digest("scope"))
    d.expect(P.REWORK)
    assert d.effect(ReviseChanges).command == ReviseChanges(
        owner=Owner.CODER, tree=BUILD, failure_report=digest("scope")
    )


def test_a_release_verified_for_another_tree_is_rejected() -> None:
    d = Driver()
    _to_release(d)
    stale = d.answer(VerifyRelease, ReleaseVerified, tree=BASE, bundle=bundle(BASE))
    assert d.reject(stale).reason is Rejection.STALE_EVIDENCE


def test_nothing_is_published_before_a_person_approves() -> None:
    d = Driver()
    _to_release(d)
    d.verify_release()
    d.expect(P.HUMAN_APPROVAL)
    assert [e.command.kind for e in d.state.pending] == ["await_release_approval"]
    assert d.state.approved_by is None
    assert d.state.waiting_since == d.state.updated_at
    d.approve_release()
    assert [e.command.kind for e in d.state.pending] == ["publish_release"]
    assert d.state.approved_by == PERSON


def test_a_rejected_release_is_reworked_with_the_persons_feedback() -> None:
    d = Driver()
    _to_release(d)
    d.verify_release()
    d.complete(AwaitReleaseApproval, ReleaseRejected, by=PERSON, feedback="rename the flag")
    d.expect(P.REWORK)
    assert d.effect(ReviseChanges).command == ReviseChanges(
        owner=Owner.CODER, tree=BUILD, feedback=f"{PERSON}: rename the flag"
    )
    assert d.state.release is None  # the bundle described the old tree


def test_a_rejected_release_must_pass_every_gate_and_be_verified_again() -> None:
    d = Driver()
    _to_release(d)
    d.verify_release()
    d.complete(AwaitReleaseApproval, ReleaseRejected, by=PERSON, feedback="rename the flag")
    renamed = tree("renamed")
    d.complete(ReviseChanges, RevisionCompleted, head_tree=renamed)
    d.gates()
    d.review()
    d.release()
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.COMPLETED
    assert d.state.release == bundle(renamed)


def test_a_person_can_abort_at_release_approval() -> None:
    d = Driver()
    _to_release(d)
    d.verify_release()
    d.complete(AwaitReleaseApproval, HumanAborted, by=PERSON, reason="superseded")
    d.expect(P.CANCELLED)


def test_a_publication_for_another_tree_is_rejected() -> None:
    d = Driver()
    _to_release(d)
    d.verify_release()
    d.approve_release()
    wrong = d.answer(PublishRelease, ReleasePublished, tree=BASE, reference="x")
    assert d.reject(wrong).reason is Rejection.STALE_EVIDENCE


# ---------- pausing


def _pause(d: Driver) -> Step:
    return d.send(PauseRequested(at=d.tick(), by=PERSON, reason="maintenance"))


def test_pausing_cancels_work_in_flight_releases_its_budget_and_waits_for_an_operator() -> None:
    d = Driver()
    d.intake()
    d.plan()
    in_flight = tuple(e.id for e in d.state.pending)
    step = _pause(d)
    d.expect(P.PAUSED)
    assert step.edge.escape
    assert [e.command for e in step.effects] == [
        CancelEffects(effect_ids=in_flight),
        AwaitResume(),
    ]
    assert d.state.ledger.reservations == ()
    assert d.state.resume_phase is P.IMPLEMENTING
    assert d.state.waiting_since == d.state.updated_at


def test_resuming_reissues_exactly_the_interrupted_work() -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.complete(ImplementPlan, ImplementationCompleted)
    _pause(d)
    d.complete(AwaitResume, Resumed, by=PERSON)
    d.expect(P.IMPLEMENTING)
    assert [e.command for e in d.state.pending] == [
        AuthorAcceptanceTests(plan=plan_ref(1), base_tree=BASE)
    ]
    assert d.state.build.implementation_done
    assert d.state.ledger.reservations != ()  # the re-issued work is funded again
    d.complete(AuthorAcceptanceTests, AcceptanceTestsAuthored)
    assert d.effect(IntegrateBuild).command == IntegrateBuild(base_tree=BASE)


def test_an_answer_to_work_cancelled_by_a_pause_is_rejected() -> None:
    d = Driver()
    d.intake()
    late = d.answer(RequestPlan, PlanProduced, plan=plan_ref(1))
    _pause(d)
    late = late.model_copy(update={"at": d.tick()})
    assert d.reject(late).reason is Rejection.UNKNOWN_EFFECT


def test_a_paused_run_cannot_be_paused_again() -> None:
    d = Driver()
    _pause(d)
    again = PauseRequested(at=d.tick(), by=PERSON)
    assert d.reject(again).reason is Rejection.NOT_PAUSABLE


@pytest.mark.parametrize("where", ["plan_gate", "blocked", "release_approval"])
def test_a_run_waiting_on_a_person_cannot_be_paused(where: str) -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN))
    d.intake()
    d.plan()
    if where != "plan_gate":
        d.complete(AwaitPlanApproval, PlanApproved, by=PERSON)
        d.build()
        d.gates()
        d.review(finding("f1", Severity.BLOCKER))
        if where == "blocked":
            _to_human(d)
        else:
            d.complete(
                RunNegotiationRound,
                NegotiationRoundCompleted,
                resolutions=_verdicts(f1=Verdict.WITHDRAWN),
            )
            d.verify_release()
    assert d.reject(PauseRequested(at=d.tick(), by=PERSON)).reason is Rejection.NOT_PAUSABLE


def test_an_approved_release_can_be_paused_before_it_is_published() -> None:
    d = Driver()
    _to_release(d)
    d.verify_release()
    d.approve_release()
    _pause(d)
    d.complete(AwaitResume, Resumed, by=PERSON)
    d.expect(P.HUMAN_APPROVAL)
    assert d.state.approved_by == PERSON
    d.publish()
    d.expect(P.COMPLETED)


def test_time_paused_does_not_count_against_the_wall_clock() -> None:
    d = Driver()
    d.intake()
    asked, original = d.tick(), d.state.deadline
    d.send(PauseRequested(at=asked, by=PERSON))
    d.tick(HOUR)
    d.complete(AwaitResume, Resumed, by=PERSON)
    d.expect(P.PLANNING)
    assert d.state.deadline == original + (d.state.updated_at - asked)
    assert d.state.paused == d.state.updated_at - asked


@pytest.mark.parametrize("stop", ["abort", "cancel"])
def test_a_paused_run_can_be_stopped(stop: str) -> None:
    d = Driver()
    d.intake()
    _pause(d)
    if stop == "abort":
        d.complete(AwaitResume, HumanAborted, by=PERSON, reason="not coming back")
    else:
        d.send(CancelRequested(at=d.tick(), reason="operator"))
    d.expect(P.CANCELLED)
    assert (d.state.resume_phase, d.state.resume_commands, d.state.pending) == (None, (), ())


# ---------- global escapes


def test_cancellation_cancels_in_flight_effects_and_releases_their_budget() -> None:
    d = Driver()
    d.intake()
    d.plan()
    in_flight = tuple(e.id for e in d.state.pending)
    step = d.send(CancelRequested(at=d.tick(), reason="operator"))
    d.expect(P.CANCELLED)
    assert d.state.outcome is not None
    assert step.edge.escape
    assert [e.command for e in step.effects] == [
        CancelEffects(effect_ids=in_flight),
        PublishOutcome(outcome=d.state.outcome),
    ]
    assert d.state.ledger.reservations == ()


def test_deadline_event_fails_the_run() -> None:
    d = Driver()
    d.send(DeadlineExceeded(at=d.tick()))
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.DEADLINE_EXCEEDED


def test_any_event_stamped_after_the_deadline_fails_the_run_but_keeps_its_spend() -> None:
    d = Driver(limits(wall_clock=timedelta(minutes=5)))
    d.intake()
    late = d.answer(RequestPlan, PlanProduced, plan=plan_ref(1), spend=Spend(usd_micros=42))
    late = late.model_copy(update={"at": T0 + timedelta(minutes=6)})
    d.send(late)
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.DEADLINE_EXCEEDED
    assert d.state.ledger.spent.usd_micros == 42


def test_overspending_a_reservation_fails_the_run_and_records_the_real_spend() -> None:
    d = Driver(limits(budget_usd_micros=1_000))
    d.intake()
    d.complete(RequestPlan, PlanProduced, plan=plan_ref(1), spend=Spend(usd_micros=1_500))
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.BUDGET_EXHAUSTED
    assert d.state.ledger.spent.usd_micros == 1_500
    assert d.steps[-1].edge.escape


def test_a_budget_too_small_to_fund_the_next_effects_fails_before_issuing_them() -> None:
    d = Driver(limits(budget_usd_micros=3))
    d.intake()
    d.complete(RequestPlan, PlanProduced, plan=plan_ref(1), spend=Spend(usd_micros=2))
    # 1 micro-USD left cannot fund the two IMPLEMENTING regions.
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.BUDGET_EXHAUSTED
    assert [e.command.kind for e in d.steps[-1].effects] == ["publish_outcome"]


def test_agent_failure_in_one_region_cancels_the_other() -> None:
    d = Driver()
    d.intake()
    d.plan()
    tests_effect = d.effect(AuthorAcceptanceTests).id
    step = d.complete(ImplementPlan, AgentFailed, error="gateway: 3 retries exhausted")
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.AGENT_FAILED
    assert step.effects[0].command == CancelEffects(effect_ids=(tests_effect,))


def test_agent_refusal_fails_the_run_with_its_category() -> None:
    d = Driver()
    d.intake()
    d.complete(RequestPlan, AgentRefused, category="cyber")
    assert d.state.outcome is not None
    assert (d.state.outcome.reason, d.state.outcome.detail) == (
        OutcomeReason.AGENT_REFUSED,
        "cyber",
    )


# ---------- admission: what is rejected, and that rejection changes nothing


def test_terminal_runs_absorb_every_event() -> None:
    d = Driver()
    d.send(CancelRequested(at=d.tick(), reason="stop"))
    assert d.reject(DeadlineExceeded(at=d.tick())).reason is Rejection.TERMINAL
    assert d.reject(PauseRequested(at=d.tick(), by=PERSON)).reason is Rejection.TERMINAL


def test_duplicate_completions_are_rejected() -> None:
    d = Driver()
    d.intake()
    duplicate = d.events[-1]
    assert d.reject(duplicate).reason is Rejection.UNKNOWN_EFFECT


def test_a_completion_cannot_answer_a_different_kind_of_effect() -> None:
    d = Driver()
    d.intake()
    d.plan()
    wrong = ImplementationCompleted(at=d.tick(), effect_id=d.effect(AuthorAcceptanceTests).id)
    assert d.reject(wrong).reason is Rejection.MISMATCHED_COMPLETION


def test_time_cannot_run_backwards() -> None:
    d = Driver()
    d.intake()
    early = d.answer(RequestPlan, PlanProduced, plan=plan_ref(1))
    early = early.model_copy(update={"at": T0 - timedelta(seconds=1)})
    assert d.reject(early).reason is Rejection.CLOCK_REGRESSION


def test_a_run_can_only_start_once() -> None:
    d = Driver()
    assert d.reject(d.events[0]).reason is Rejection.ALREADY_STARTED


def test_an_event_without_a_rule_in_this_phase_is_rejected() -> None:
    d = Driver()
    event = d.answer(AdmitRun, RunAdmitted)
    with pytest.raises(TransitionRejected) as info:
        transition(d.state, event, table=TransitionTable(()))
    assert info.value.reason is Rejection.NOT_ACCEPTED_IN_PHASE


def test_a_handler_that_takes_an_undeclared_edge_is_an_invariant_violation() -> None:
    def rogue(state: RunState, _: RunAdmitted) -> Decision:
        return Decision(state.model_copy(update={"phase": Phase.UNIT_TESTING}))

    table = TransitionTable((Rule(Phase.CREATED, RunAdmitted, frozenset({P.VALIDATING}), rogue),))
    d = Driver()
    event = d.answer(AdmitRun, RunAdmitted)
    with pytest.raises(InvariantViolation) as info:
        transition(d.state, event, table=table)
    assert str(info.value) == "rule created x RunAdmitted moved to undeclared phase unit_testing"


# ---------- replay


def test_replay_rebuilds_the_exact_state() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    assert replay(d.events) == d.state


def test_replay_rebuilds_a_paused_and_resumed_run() -> None:
    d = Driver()
    d.intake()
    _pause(d)
    d.complete(AwaitResume, Resumed, by=PERSON)
    d.plan()
    assert replay(d.events) == d.state


def test_replay_rejects_a_journal_that_does_not_begin_with_run_started() -> None:
    d = Driver()
    d.intake()
    with pytest.raises(ReplayError) as info:
        replay(d.events[1:])
    assert str(info.value) == "journal must begin with RunStarted, got RunAdmitted"
    with pytest.raises(ReplayError, match="must begin with RunStarted"):
        replay([])


def test_replay_reports_the_first_corrupt_event() -> None:
    d = Driver()
    d.intake()
    with pytest.raises(ReplayError, match="event #5 rejected"):
        replay([*d.events, d.events[-1]])


def test_start_asks_for_admission() -> None:
    step = start(run_started())
    assert [e.command for e in step.effects] == [AdmitRun()]
    assert step.state.phase is P.CREATED
    assert step.edge.source is None


def test_a_handler_that_produces_an_invalid_state_is_caught_at_that_step() -> None:
    """Invariants run on every production transition, not only in tests: a rule that takes
    a declared edge but skips its preconditions fails at the step that caused it."""

    def premature(state: RunState, _: RunAdmitted) -> Decision:
        return Decision(
            state.model_copy(update={"phase": Phase.FINAL_VERIFICATION}),
            (VerifyRelease(tree=BUILD),),
        )

    table = TransitionTable(
        (Rule(Phase.CREATED, RunAdmitted, frozenset({P.FINAL_VERIFICATION}), premature),)
    )
    d = Driver()
    event = d.answer(AdmitRun, RunAdmitted)
    with pytest.raises(InvariantViolation) as info:
        transition(d.state, event, table=table)
    assert any("without every gate passed" in v for v in info.value.violations)


@pytest.mark.parametrize(
    "accessor", ["require_head", "require_base", "require_plan", "require_release"]
)
def test_reading_facts_before_they_exist_is_an_invariant_violation(accessor: str) -> None:
    with pytest.raises(InvariantViolation, match="is not set"):
        getattr(Driver().state, accessor)()


def test_a_failed_non_llm_effect_fails_the_run_instead_of_stalling() -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.complete(ImplementPlan, ImplementationCompleted)
    d.complete(AuthorAcceptanceTests, AcceptanceTestsAuthored)
    d.complete(IntegrateBuild, EffectFailed, error="merge conflict in src/app.py")
    assert d.state.outcome is not None
    assert (d.state.outcome.reason, d.state.outcome.detail) == (
        OutcomeReason.EFFECT_FAILED,
        "merge conflict in src/app.py",
    )


def test_effect_failed_cannot_complete_an_llm_effect() -> None:
    """LLM effects report failure with AgentFailed, which carries spend."""
    d = Driver()
    d.intake()
    wrong = d.answer(RequestPlan, EffectFailed, error="x")
    assert d.reject(wrong).reason is Rejection.MISMATCHED_COMPLETION


def test_fold_resumes_from_any_intermediate_state() -> None:
    d = Driver()
    d.to_review()
    middle = d.steps[4].state
    assert fold(middle, d.events[5:]) == d.state


# ---------- the clock stops while a person is asked


HOUR = 3600  # longer than the whole 45-minute wall clock


def test_a_plan_gate_answered_after_the_deadline_moves_it_by_the_wait() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN))
    d.intake()
    d.plan()
    gate = d.state
    asked, original = gate.updated_at, gate.deadline
    assert gate.waiting_since == asked

    d.tick(HOUR)  # the person takes an hour
    d.complete(AwaitPlanApproval, PlanApproved, by=PERSON)
    d.expect(P.IMPLEMENTING)
    answered = d.state
    waited = answered.updated_at - asked
    assert (answered.deadline, answered.paused) == (original + waited, waited)
    assert answered.waiting_since is None

    # Work resumes with the time left when the person was asked, and no more.
    left = original - asked
    d.tick(left.total_seconds() - 1)
    d.complete(ImplementPlan, ImplementationCompleted)
    d.expect(P.IMPLEMENTING)
    d.tick(2)
    d.complete(AuthorAcceptanceTests, AcceptanceTestsAuthored)
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.DEADLINE_EXCEEDED


@pytest.mark.parametrize(
    ("answer", "fields", "phase"),
    [
        (PlanRejected, {"feedback": "split step 2"}, P.PLANNING),
        (HumanAborted, {"reason": "not now"}, P.CANCELLED),
    ],
)
def test_every_answer_to_a_plan_gate_moves_the_deadline(
    answer: type[Completion], fields: dict[str, str], phase: Phase
) -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN))
    d.intake()
    d.plan()
    asked, original = d.state.updated_at, d.state.deadline
    d.tick(HOUR)
    d.complete(AwaitPlanApproval, answer, by=PERSON, **fields)
    d.expect(phase)
    assert d.state.deadline == original + (d.state.updated_at - asked)
    assert d.state.waiting_since is None


def test_waits_for_a_ruling_and_an_approval_add_up_with_waits_at_the_plan_gate() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN))
    d.intake()
    d.plan()
    d.tick(HOUR)
    d.complete(AwaitPlanApproval, PlanApproved, by=PERSON)
    first = d.state.paused
    d.build()
    d.gates()
    d.review(finding("f1", Severity.BLOCKER))
    _to_human(d)
    asked = d.state.updated_at
    assert d.state.waiting_since == asked
    d.tick(2 * HOUR)
    d.complete(AwaitHumanRuling, HumanRuled, by=PERSON, resolutions=_verdicts(f1=Verdict.DISMISSED))
    d.expect(P.FINAL_VERIFICATION)
    second = d.state.paused
    assert second == first + (d.state.updated_at - asked)
    d.verify_release()
    asked = d.state.updated_at
    d.tick(3 * HOUR)
    d.approve_release()
    assert d.state.paused == second + (d.state.updated_at - asked)
    assert d.state.deadline == d.state.started_at + d.state.limits.wall_clock + d.state.paused
    d.publish()
    d.expect(P.COMPLETED)


def test_cancelling_while_a_person_is_asked_stops_waiting_without_moving_the_deadline() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN))
    d.intake()
    d.plan()
    original = d.state.deadline
    d.send(CancelRequested(at=d.tick(), reason="stop"))
    d.expect(P.CANCELLED)
    assert (d.state.waiting_since, d.state.deadline, d.state.paused) == (
        None,
        original,
        timedelta(0),
    )


def test_agent_work_never_stops_the_clock() -> None:
    d = Driver()
    d.to_review()
    assert d.state.waiting_since is None
    assert d.state.paused == timedelta(0)
