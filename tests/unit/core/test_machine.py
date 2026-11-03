"""Example-based tests: one readable scenario per behavior of the state machine."""

from __future__ import annotations

from datetime import timedelta

import pytest

from msa.core import (
    InvariantViolation,
    Rejection,
    ReplayError,
    RunState,
    TransitionRejected,
    fold,
    replay,
    start,
    transition,
)
from msa.core.budget import Spend
from msa.core.effects import (
    AuthorAcceptanceTests,
    AwaitHumanRuling,
    AwaitPlanApproval,
    CancelEffects,
    Command,
    Finalize,
    ImplementPlan,
    IntegrateBuild,
    PrepareWorkspace,
    PublishOutcome,
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
    Completion,
    DeadlineExceeded,
    EffectFailed,
    Finalized,
    HumanAborted,
    HumanRuled,
    ImplementationCompleted,
    IntakeCompleted,
    IntakeRejected,
    NegotiationRoundCompleted,
    PlanApproved,
    PlanProduced,
    PlanRejected,
    RevisionCompleted,
    TestsFailed,
    TestsPassed,
    VerificationFailed,
    VerificationPassed,
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
    T0,
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


def test_happy_path_visits_every_phase_once_and_publishes_the_outcome() -> None:
    d = Driver()
    d.to_review()
    d.review()
    d.complete(Finalize, Finalized, tree=BUILD, artifact=digest("patch"))

    assert d.phases() == [
        P.INTAKE,
        P.PLANNING,
        P.BUILDING,  # plan produced; both regions start
        P.BUILDING,  # implementation done
        P.BUILDING,  # acceptance tests done -> integrate
        P.VERIFYING,
        P.TESTING,
        P.REVIEWING,
        P.FINALIZING,
        P.DONE,
    ]
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.COMPLETED
    assert [e.command.kind for e in d.steps[-1].effects] == ["publish_outcome"]
    assert d.state.pending == ()
    assert d.state.ledger.reservations == ()


def test_effects_carry_the_inputs_their_executors_need() -> None:
    d = Driver()
    d.intake()
    d.plan()
    implement = d.effect(ImplementPlan).command
    tests = d.effect(AuthorAcceptanceTests).command
    assert implement == ImplementPlan(plan=plan_ref(1), base_tree=BASE)
    assert tests == AuthorAcceptanceTests(plan=plan_ref(1), base_tree=BASE)


def test_effect_ids_are_deterministic_functions_of_the_journal() -> None:
    first, second = Driver(), Driver()
    for d in (first, second):
        d.to_review()
    assert [e.id for s in first.steps for e in s.effects] == [
        e.id for s in second.steps for e in s.effects
    ]
    assert first.steps[1].effects[0].id == EffectId("run-1:2:0")


# ---------- planning and the plan gate


def test_human_plan_gate_waits_for_approval() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN))
    d.intake()
    d.plan()
    d.expect(P.PLAN_GATE)
    assert d.effect(AwaitPlanApproval).command == AwaitPlanApproval(plan=plan_ref(1))
    d.complete(AwaitPlanApproval, PlanApproved)
    d.expect(P.BUILDING)


def test_rejected_plan_is_replanned_with_feedback_until_the_limit() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN, max_replans=1))
    d.intake()
    d.plan()
    d.complete(AwaitPlanApproval, PlanRejected, feedback="smaller steps")
    d.expect(P.PLANNING)
    assert d.state.plan is None
    assert d.effect(RequestPlan).command == RequestPlan(attempt=2, feedback="smaller steps")

    d.plan()
    assert d.steps[-1].state.plan == plan_ref(2)  # distinct expression: no stale narrowing
    d.complete(AwaitPlanApproval, PlanRejected, feedback="still too big")
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.REPLAN_LIMIT


def test_human_can_abort_at_the_plan_gate() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN))
    d.intake()
    d.plan()
    d.complete(AwaitPlanApproval, HumanAborted, reason="wrong task")
    d.expect(P.ABORTED)


def test_intake_rejection_fails_the_run() -> None:
    d = Driver()
    d.complete(PrepareWorkspace, IntakeRejected, reason="not a git repository")
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.INTAKE_REJECTED
    assert d.state.outcome.detail == "not a git repository"


# ---------- building: two parallel regions and a join


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
    d.expect(P.BUILDING)
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


# ---------- verification, testing, and evidence binding


def test_verification_failure_sends_the_coder_back_and_invalidates_evidence() -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.build()
    d.complete(RunVerification, VerificationFailed, tree=BUILD, report=digest("lint"))
    d.expect(P.REVISING)
    assert d.effect(ReviseChanges).command == ReviseChanges(
        owner=Owner.CODER, tree=BUILD, failure_report=digest("lint")
    )

    revised = tree("revised")
    d.complete(ReviseChanges, RevisionCompleted, head_tree=revised)
    d.expect(P.VERIFYING)
    assert d.effect(RunVerification).command == RunVerification(tree=revised)

    stale = d.answer(RunVerification, VerificationPassed, tree=BUILD)
    assert d.reject(stale).reason is Rejection.STALE_EVIDENCE


@pytest.mark.parametrize(
    ("kind", "owner"),
    [
        (FailureKind.IMPL_BUG, Owner.CODER),
        (FailureKind.TEST_BUG, Owner.TESTER),
        (FailureKind.FLAKY, Owner.TESTER),
    ],
)
def test_test_failures_are_routed_to_the_triaged_owner(kind: FailureKind, owner: Owner) -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.build()
    d.verify()
    d.complete(
        RunTestSuite,
        TestsFailed,
        tree=BUILD,
        triage=Triage(kind=kind, owner=owner),
        report=digest("tests"),
    )
    revise = d.effect(ReviseChanges).command
    assert isinstance(revise, ReviseChanges)
    assert revise.owner is owner


def test_environment_failures_end_the_run_because_no_agent_can_fix_them() -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.build()
    d.verify()
    d.complete(
        RunTestSuite,
        TestsFailed,
        tree=BUILD,
        triage=Triage(kind=FailureKind.ENVIRONMENT, owner=None),
        report=digest("tests"),
    )
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.ENVIRONMENT_FAILURE


def test_a_test_result_for_another_tree_is_rejected() -> None:
    d = Driver()
    d.intake()
    d.plan()
    d.build()
    d.verify()
    assert d.reject(d.answer(RunTestSuite, TestsPassed, tree=BASE)).reason is (
        Rejection.STALE_EVIDENCE
    )


def test_revisions_are_capped() -> None:
    d = Driver(limits(max_revisions=1))
    d.intake()
    d.plan()
    d.build()
    d.complete(RunVerification, VerificationFailed, tree=BUILD, report=digest("lint"))
    d.complete(ReviseChanges, RevisionCompleted, head_tree=tree("r1"))
    d.complete(RunVerification, VerificationFailed, tree=tree("r1"), report=digest("lint"))
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.REVISION_LIMIT
    assert d.state.revisions == 1


# ---------- review and negotiation


def test_advisory_findings_never_block_finalization() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("style", Severity.NIT), finding("naming", Severity.MINOR))
    d.expect(P.FINALIZING)
    assert {f.status for f in d.state.findings} == {FindingStatus.ADVISORY}


def test_accepted_findings_lead_to_a_revision_and_a_fresh_review() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER), finding("f2", Severity.NIT))
    d.expect(P.NEGOTIATING)
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
    d.verify()
    d.test()
    assert d.effect(RequestReview).command == RequestReview(tree=tree("fixed"), cycle=1)
    assert d.state.negotiation_round == 0


def test_withdrawn_findings_finalize_without_a_revision() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.MAJOR))
    d.complete(
        RunNegotiationRound,
        NegotiationRoundCompleted,
        resolutions=_verdicts(f1=Verdict.WITHDRAWN),
    )
    d.expect(P.FINALIZING)


def test_a_round_without_progress_escalates_to_a_human() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.expect(P.AWAITING_HUMAN)
    assert d.effect(AwaitHumanRuling).command == AwaitHumanRuling(
        tree=BUILD, findings=(FindingId("f1"),)
    )


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
    d.expect(P.AWAITING_HUMAN)
    assert d.state.negotiation_round == 2


def test_human_ruling_upholding_a_finding_triggers_a_revision() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.complete(AwaitHumanRuling, HumanRuled, resolutions=_verdicts(f1=Verdict.UPHELD))
    d.expect(P.REVISING)


def test_human_ruling_dismissing_every_finding_finalizes() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.complete(AwaitHumanRuling, HumanRuled, resolutions=_verdicts(f1=Verdict.DISMISSED))
    d.expect(P.FINALIZING)


def test_rulings_must_cover_every_open_finding_and_use_ruling_verdicts() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("a", Severity.BLOCKER), finding("b", Severity.MAJOR))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())

    partial = d.answer(AwaitHumanRuling, HumanRuled, resolutions=_verdicts(a=Verdict.UPHELD))
    assert d.reject(partial).reason is Rejection.INCOMPLETE_RULING

    accepted = d.answer(
        AwaitHumanRuling,
        HumanRuled,
        resolutions=_verdicts(a=Verdict.ACCEPTED, b=Verdict.DISMISSED),
    )
    assert d.reject(accepted).reason is Rejection.ILLEGAL_VERDICT

    unknown = d.answer(
        AwaitHumanRuling,
        HumanRuled,
        resolutions=_verdicts(a=Verdict.UPHELD, b=Verdict.UPHELD, zzz=Verdict.UPHELD),
    )
    assert d.reject(unknown).reason is Rejection.UNKNOWN_FINDING


def test_arbiter_escalation_stays_in_negotiation_until_the_ruling() -> None:
    d = Driver(limits(escalation=Escalation.ARBITER))
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.expect(P.NEGOTIATING)
    assert d.effect(RequestArbitration).command == RequestArbitration(
        tree=BUILD, findings=(FindingId("f1"),)
    )
    d.complete(RequestArbitration, ArbitrationRuled, resolutions=_verdicts(f1=Verdict.DISMISSED))
    d.expect(P.FINALIZING)


def test_human_can_abort_instead_of_ruling() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.complete(AwaitHumanRuling, HumanAborted, reason="rethink the approach")
    assert d.state.outcome is not None
    assert d.state.outcome.reason is OutcomeReason.HUMAN_ABORTED


def test_a_demanded_change_with_no_revisions_left_fails_fast() -> None:
    """Without this, the run would keep negotiating (and page a human) towards an outcome
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


# ---------- global escapes


def test_cancellation_cancels_in_flight_effects_and_releases_their_budget() -> None:
    d = Driver()
    d.intake()
    d.plan()
    in_flight = tuple(e.id for e in d.state.pending)
    step = d.send(CancelRequested(at=d.tick(), reason="operator"))
    d.expect(P.ABORTED)
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
    # 1 micro-USD left cannot fund the two BUILDING regions.
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
    event = d.answer(PrepareWorkspace, IntakeCompleted, base_tree=BASE)
    with pytest.raises(TransitionRejected) as info:
        transition(d.state, event, table=TransitionTable(()))
    assert info.value.reason is Rejection.NOT_ACCEPTED_IN_PHASE


def test_a_handler_that_takes_an_undeclared_edge_is_an_invariant_violation() -> None:
    def rogue(state: RunState, _: IntakeCompleted) -> Decision:
        return Decision(state.model_copy(update={"phase": Phase.TESTING}))

    table = TransitionTable((Rule(Phase.INTAKE, IntakeCompleted, frozenset({P.PLANNING}), rogue),))
    d = Driver()
    event = d.answer(PrepareWorkspace, IntakeCompleted, base_tree=BASE)
    with pytest.raises(InvariantViolation, match="undeclared phase"):
        transition(d.state, event, table=table)


# ---------- replay


def test_replay_rebuilds_the_exact_state() -> None:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    assert replay(d.events) == d.state


def test_replay_rejects_a_journal_that_does_not_begin_with_run_started() -> None:
    d = Driver()
    d.intake()
    with pytest.raises(ReplayError, match="must begin with RunStarted"):
        replay(d.events[1:])
    with pytest.raises(ReplayError, match="must begin with RunStarted"):
        replay([])


def test_replay_reports_the_first_corrupt_event() -> None:
    d = Driver()
    d.intake()
    with pytest.raises(ReplayError, match="event #3 rejected"):
        replay([*d.events, d.events[-1]])


def test_start_produces_the_intake_effect() -> None:
    step = start(run_started())
    assert [e.command for e in step.effects] == [PrepareWorkspace()]
    assert step.edge.source is None


def test_a_handler_that_produces_an_invalid_state_is_caught_at_that_step() -> None:
    """Invariants run on every production transition, not only in tests: a rule that takes
    a declared edge but skips its preconditions fails at the step that caused it."""

    def premature(state: RunState, _: IntakeCompleted) -> Decision:
        return Decision(state.model_copy(update={"phase": Phase.FINALIZING}))

    table = TransitionTable(
        (Rule(Phase.INTAKE, IntakeCompleted, frozenset({P.FINALIZING}), premature),)
    )
    d = Driver()
    event = d.answer(PrepareWorkspace, IntakeCompleted, base_tree=BASE)
    with pytest.raises(InvariantViolation) as info:
        transition(d.state, event, table=table)
    assert any("without verify+test evidence" in v for v in info.value.violations)


@pytest.mark.parametrize("accessor", ["require_head", "require_base", "require_plan"])
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
