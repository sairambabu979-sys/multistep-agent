"""The orchestrator's transition rules. This module *is* the state machine's specification.

The workflow is AEGIS's (ADR-015): CREATED → VALIDATING → CONTEXT → PLANNING →
PLAN_APPROVAL → IMPLEMENTING → STATIC_ANALYSIS → UNIT_TESTING → INTEGRATION_TESTING →
SECURITY_REVIEW → CODE_REVIEW → NEGOTIATION → FINAL_VERIFICATION → HUMAN_APPROVAL →
COMPLETED, with REWORK, BLOCKED and PAUSED on the side.

Each handler is a pure function ``(state, event) -> Decision``. Handlers may assume:

* the event completes an effect that was pending, of a kind the event may complete;
* the effect has already been removed from ``state.pending`` and its spend settled;
* global guards (cancel, pause, deadline, budget, agent failure) have already been applied.

Handlers must set the next phase explicitly. The dispatcher rejects any phase the rule
did not declare in its targets.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from msa.core.effects import (
    AnyCommand,
    AuthorAcceptanceTests,
    AwaitHumanRuling,
    AwaitPlanApproval,
    AwaitReleaseApproval,
    AwaitUnblock,
    ImplementPlan,
    IntegrateBuild,
    LoadContext,
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
from msa.core.errors import Rejection, TransitionRejected
from msa.core.events import (
    AcceptanceTestsAuthored,
    ArbitrationRuled,
    BuildIntegrated,
    ContextLoaded,
    HumanAborted,
    HumanRuled,
    ImplementationCompleted,
    IntegrationTestsFailed,
    IntegrationTestsPassed,
    NegotiationRoundCompleted,
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
    ReviewProduced,
    RevisionCompleted,
    RunAdmitted,
    RunDenied,
    SecurityReviewFailed,
    SecurityReviewPassed,
    StaticAnalysisFailed,
    StaticAnalysisPassed,
    Unblocked,
    UnitTestsFailed,
    UnitTestsPassed,
)
from msa.core.ids import Digest, FindingId, TreeHash
from msa.core.state import BuildProgress, Finding, FindingStatus, RunState
from msa.core.table import Decision, Rule, TransitionTable
from msa.core.values import (
    NEGOTIATION_VERDICTS,
    RULING_VERDICTS,
    Escalation,
    Outcome,
    OutcomeReason,
    Owner,
    Phase,
    PlanGate,
    Resolution,
    Triage,
    Verdict,
)

# ---------- helpers


def _go(state: RunState, phase: Phase, *commands: AnyCommand, **updates: Any) -> Decision:
    return Decision(state.model_copy(update={"phase": phase, **updates}), commands)


def _stop(state: RunState, reason: OutcomeReason, detail: str, **updates: Any) -> Decision:
    outcome = Outcome(reason=reason, detail=detail)
    return _go(state, outcome.phase, outcome=outcome, **updates)


def _require_current(state: RunState, tree: TreeHash, what: str) -> None:
    """Evidence is only admissible for the tree it was produced against."""
    head = state.require_head()
    if tree != head:
        raise TransitionRejected(
            Rejection.STALE_EVIDENCE, f"{what} is for tree {tree}, head is {head}"
        )


def _passed(state: RunState, gate: str, tree: TreeHash) -> RunState:
    return state.model_copy(update={"evidence": state.evidence.model_copy(update={gate: tree})})


def _begin_build(state: RunState) -> Decision:
    plan, base = state.require_plan(), state.require_base()
    return _go(
        state,
        Phase.IMPLEMENTING,
        ImplementPlan(plan=plan, base_tree=base),
        AuthorAcceptanceTests(plan=plan, base_tree=base),
        build=BuildProgress(),  # pragma: no mutate (defensive: IMPLEMENTING is entered once)
    )


def _advance_build(state: RunState, build: BuildProgress) -> Decision:
    if build.complete:
        return _go(
            state, Phase.IMPLEMENTING, IntegrateBuild(base_tree=state.require_base()), build=build
        )
    return _go(state, Phase.IMPLEMENTING, build=build)


def _static_analysis(state: RunState) -> Decision:
    return _go(state, Phase.STATIC_ANALYSIS, RunStaticAnalysis(tree=state.require_head()))


def _revisions_exhausted(state: RunState) -> bool:
    return state.revisions >= state.limits.max_revisions


def _rework(
    state: RunState,
    owner: Owner,
    *,
    findings: tuple[FindingId, ...] = (),
    failure_report: Digest | None = None,
    feedback: str | None = None,
) -> Decision:
    """Every failed gate and every agreed change comes here, bounded by the revision cap."""
    if _revisions_exhausted(state):
        return _stop(
            state,
            OutcomeReason.REVISION_LIMIT,
            f"revision {state.revisions + 1} needed; limit is {state.limits.max_revisions}",
        )
    command = ReviseChanges(
        owner=owner,
        tree=state.require_head(),
        findings=findings,
        failure_report=failure_report,
        feedback=feedback,
    )
    # A release bundle describes one tree; the revision makes another.
    return _go(state, Phase.REWORK, command, revisions=state.revisions + 1, release=None)


def _block(state: RunState, phase: Phase, retry: AnyCommand, reason: str) -> Decision:
    """An environment failure blocks the stage instead of failing the run: a person fixes
    the environment and unblocks, and the stage runs again."""
    return _go(
        state,
        Phase.BLOCKED,
        AwaitUnblock(reason=reason),
        resume_phase=phase,
        resume_commands=(retry,),
    )


def _failed_tests(
    state: RunState, phase: Phase, retry: AnyCommand, triage: Triage, report: Digest
) -> Decision:
    if triage.owner is None:  # exactly the environment failures, by Triage's validator
        return _block(state, phase, retry, f"the {phase.value} stage could not run: {report}")
    return _rework(state, triage.owner, failure_report=report)


def _final_verification(state: RunState) -> Decision:
    return _go(state, Phase.FINAL_VERIFICATION, VerifyRelease(tree=state.require_head()))


def _resolve(
    state: RunState, resolutions: Iterable[Resolution], *, allowed: frozenset[Verdict]
) -> RunState:
    """Apply verdicts to open findings. Findings without a verdict stay open."""
    open_ids = set(state.open_finding_ids)
    verdicts: dict[FindingId, Verdict] = {}
    for resolution in resolutions:
        if resolution.finding_id not in open_ids:
            raise TransitionRejected(
                Rejection.UNKNOWN_FINDING, f"{resolution.finding_id} is not an open finding"
            )
        if resolution.verdict not in allowed:
            raise TransitionRejected(
                Rejection.ILLEGAL_VERDICT, f"{resolution.verdict} is not allowed here"
            )
        verdicts[resolution.finding_id] = resolution.verdict
    findings = tuple(
        f.model_copy(update={"status": FindingStatus.from_verdict(verdicts[f.id])})
        if f.id in verdicts
        else f
        for f in state.findings
    )
    return state.model_copy(update={"findings": findings})


def _rule(state: RunState, resolutions: Iterable[Resolution]) -> RunState:
    """Apply a complete ruling (the Arbiter's, or a person's). It must close every open
    finding, so it can never leave the run waiting on findings nobody will rule on again."""
    ruled = _resolve(state, resolutions, allowed=RULING_VERDICTS)
    if missing := ruled.open_finding_ids:
        raise TransitionRejected(Rejection.INCOMPLETE_RULING, f"no verdict for {sorted(missing)}")
    return ruled


def _settle(state: RunState) -> Decision:
    """All blocking findings are closed: rework if any closure demands a change."""
    if changes := state.change_requests:
        return _rework(state, Owner.CODER, findings=changes)
    return _final_verification(state)


def _decided(state: RunState) -> bool:
    """With no revisions left, the first demanded change already dooms the run, so more
    rounds (or a person's time) would be wasted. Found by the exhaustive model checker
    (EF COMPLETED failed for these states)."""
    return bool(state.change_requests) and _revisions_exhausted(state)


def _escalate(state: RunState) -> Decision:
    """The arbitration ladder, cheapest and most objective rung first (PLAN §4.2,
    ADR-004). Only what a repro test, the preference store and the style rule leave open
    reaches the Arbiter role, or a person if escalation goes to one (ADR-014)."""
    arbiter = state.limits.escalation is Escalation.ARBITER
    command = RequestArbitration(
        tree=state.require_head(), findings=state.open_finding_ids, arbiter=arbiter
    )
    return _go(state, Phase.NEGOTIATION, command)


# ---------- CREATED / VALIDATING / CONTEXT


def _run_admitted(state: RunState, _: RunAdmitted) -> Decision:
    return _go(state, Phase.VALIDATING, ValidateRequest())


def _run_denied(state: RunState, event: RunDenied) -> Decision:
    return _stop(state, OutcomeReason.ADMISSION_DENIED, event.reason)


def _request_validated(state: RunState, _: RequestValidated) -> Decision:
    return _go(state, Phase.CONTEXT, LoadContext())


def _request_rejected(state: RunState, event: RequestRejected) -> Decision:
    return _stop(state, OutcomeReason.REQUEST_REJECTED, event.reason)


def _context_loaded(state: RunState, event: ContextLoaded) -> Decision:
    return _go(state, Phase.PLANNING, RequestPlan(attempt=1), base_tree=event.base_tree)


# ---------- PLANNING / PLAN_APPROVAL


def _plan_produced(state: RunState, event: PlanProduced) -> Decision:
    state = state.model_copy(update={"plan": event.plan})
    if state.limits.plan_gate is PlanGate.HUMAN:
        return _go(state, Phase.PLAN_APPROVAL, AwaitPlanApproval(plan=event.plan))
    return _begin_build(state)


def _plan_approved(state: RunState, _: PlanApproved) -> Decision:
    return _begin_build(state)


def _plan_rejected(state: RunState, event: PlanRejected) -> Decision:
    if state.replans >= state.limits.max_replans:
        return _stop(
            state,
            OutcomeReason.REPLAN_LIMIT,
            f"plan rejected {state.replans + 1} time(s); last feedback: {event.feedback}",
        )
    replans = state.replans + 1
    command = RequestPlan(attempt=replans + 1, feedback=event.feedback)
    return _go(state, Phase.PLANNING, command, replans=replans, plan=None)


def _human_aborted(state: RunState, event: HumanAborted) -> Decision:
    return _stop(state, OutcomeReason.HUMAN_ABORTED, f"{event.by}: {event.reason}")


# ---------- IMPLEMENTING


def _implementation_completed(state: RunState, _: ImplementationCompleted) -> Decision:
    return _advance_build(state, state.build.model_copy(update={"implementation_done": True}))


def _acceptance_tests_authored(state: RunState, _: AcceptanceTestsAuthored) -> Decision:
    return _advance_build(state, state.build.model_copy(update={"acceptance_tests_done": True}))


def _build_integrated(state: RunState, event: BuildIntegrated) -> Decision:
    return _static_analysis(state.model_copy(update={"head_tree": event.head_tree}))


# ---------- the gates


def _static_analysis_passed(state: RunState, event: StaticAnalysisPassed) -> Decision:
    _require_current(state, event.tree, "static analysis")
    command = RunUnitTests(tree=event.tree, base_tree=state.require_base())
    return _go(_passed(state, "static", event.tree), Phase.UNIT_TESTING, command)


def _static_analysis_failed(state: RunState, event: StaticAnalysisFailed) -> Decision:
    _require_current(state, event.tree, "static analysis")
    return _rework(state, Owner.CODER, failure_report=event.report)


def _unit_tests_passed(state: RunState, event: UnitTestsPassed) -> Decision:
    _require_current(state, event.tree, "unit test result")
    command = RunIntegrationTests(tree=event.tree)
    return _go(_passed(state, "unit", event.tree), Phase.INTEGRATION_TESTING, command)


def _unit_tests_failed(state: RunState, event: UnitTestsFailed) -> Decision:
    _require_current(state, event.tree, "unit test result")
    retry = RunUnitTests(tree=event.tree, base_tree=state.require_base())
    return _failed_tests(state, Phase.UNIT_TESTING, retry, event.triage, event.report)


def _integration_tests_passed(state: RunState, event: IntegrationTestsPassed) -> Decision:
    _require_current(state, event.tree, "integration test result")
    command = RunSecurityReview(tree=event.tree)
    return _go(_passed(state, "integration", event.tree), Phase.SECURITY_REVIEW, command)


def _integration_tests_failed(state: RunState, event: IntegrationTestsFailed) -> Decision:
    _require_current(state, event.tree, "integration test result")
    retry = RunIntegrationTests(tree=event.tree)
    return _failed_tests(state, Phase.INTEGRATION_TESTING, retry, event.triage, event.report)


def _security_review_passed(state: RunState, event: SecurityReviewPassed) -> Decision:
    _require_current(state, event.tree, "security review")
    review = RequestReview(tree=event.tree, cycle=state.revisions)
    return _go(
        _passed(state, "security", event.tree), Phase.CODE_REVIEW, review, negotiation_round=0
    )


def _security_review_failed(state: RunState, event: SecurityReviewFailed) -> Decision:
    """A security defect is a gate, not a discussion: it goes straight to rework."""
    _require_current(state, event.tree, "security review")
    return _rework(state, event.owner, failure_report=event.report)


# ---------- CODE_REVIEW / NEGOTIATION / BLOCKED on a ruling


def _review_produced(state: RunState, event: ReviewProduced) -> Decision:
    _require_current(state, event.tree, "review")
    findings = tuple(
        Finding(
            id=ref.id,
            severity=ref.severity,
            raised_on=event.tree,
            status=FindingStatus.OPEN if ref.severity.is_blocking else FindingStatus.ADVISORY,
        )
        for ref in event.findings
    )
    state = state.model_copy(update={"findings": findings})
    if not (open_ids := state.open_finding_ids):
        return _final_verification(state)
    first_round = RunNegotiationRound(tree=event.tree, round=1, findings=open_ids)
    return _go(state, Phase.NEGOTIATION, first_round, negotiation_round=1)


def _negotiation_round_completed(state: RunState, event: NegotiationRoundCompleted) -> Decision:
    before = len(state.open_finding_ids)
    state = _resolve(state, event.resolutions, allowed=NEGOTIATION_VERDICTS)
    remaining = state.open_finding_ids
    if not remaining or _decided(state):
        return _settle(state)
    made_progress = len(remaining) < before
    if made_progress and state.negotiation_round < state.limits.max_negotiation_rounds:
        next_round = state.negotiation_round + 1
        command = RunNegotiationRound(
            tree=state.require_head(), round=next_round, findings=remaining
        )
        return _go(state, Phase.NEGOTIATION, command, negotiation_round=next_round)
    return _escalate(state)


def _arbitration_ruled(state: RunState, event: ArbitrationRuled) -> Decision:
    if state.limits.escalation is Escalation.ARBITER:
        return _settle(_rule(state, event.resolutions))  # the Arbiter rules on everything
    state = _resolve(state, event.resolutions, allowed=RULING_VERDICTS)
    remaining = state.open_finding_ids
    if not remaining or _decided(state):
        return _settle(state)
    ruling = AwaitHumanRuling(tree=state.require_head(), findings=remaining)
    return _go(state, Phase.BLOCKED, ruling)


def _human_ruled(state: RunState, event: HumanRuled) -> Decision:
    return _settle(_rule(state, event.resolutions))


# ---------- BLOCKED on the environment, PAUSED


def _unblocked(state: RunState, _: Unblocked) -> Decision:
    return _resume(state)


def _resumed(state: RunState, _: Resumed) -> Decision:
    return _resume(state)


def _resume(state: RunState) -> Decision:
    """Back to the phase that was interrupted, with its work issued again (as new effects:
    whatever was in flight was cancelled, and effects are idempotent by design)."""
    phase, commands = state.resume_phase, state.resume_commands
    if phase is None:  # pragma: no cover - set whenever AwaitUnblock or AwaitResume is issued
        detail = "nothing to resume"  # pragma: no mutate (unreachable, see above)
        raise TransitionRejected(Rejection.NOT_ACCEPTED_IN_PHASE, detail)  # pragma: no mutate
    return _go(state, phase, *commands, resume_phase=None, resume_commands=())


# ---------- REWORK / FINAL_VERIFICATION / HUMAN_APPROVAL


def _revision_completed(state: RunState, event: RevisionCompleted) -> Decision:
    return _static_analysis(state.model_copy(update={"head_tree": event.head_tree}))


def _release_verified(state: RunState, event: ReleaseVerified) -> Decision:
    _require_current(state, event.tree, "release verification")
    approval = AwaitReleaseApproval(tree=event.tree, bundle=event.bundle)
    return _go(state, Phase.HUMAN_APPROVAL, approval, release=event.bundle)


def _release_verification_failed(state: RunState, event: ReleaseVerificationFailed) -> Decision:
    _require_current(state, event.tree, "release verification")
    return _rework(state, Owner.CODER, failure_report=event.report)


def _release_approved(state: RunState, event: ReleaseApproved) -> Decision:
    """Only now, with a person's approval on record, is the release published."""
    publish = PublishRelease(
        tree=state.require_head(),
        bundle=state.require_release(),
        approved_by=event.by,
        publish=event.publish,
    )
    return _go(state, Phase.HUMAN_APPROVAL, publish, approved_by=event.by)


def _release_rejected(state: RunState, event: ReleaseRejected) -> Decision:
    return _rework(state, Owner.CODER, feedback=f"{event.by}: {event.feedback}")


def _release_published(state: RunState, event: ReleasePublished) -> Decision:
    _require_current(state, event.tree, "published release")
    return _stop(state, OutcomeReason.COMPLETED, event.reference, publication=event.publication)


# ---------- the table

P = Phase
_GATE_FAILS = frozenset({P.REWORK, P.FAILED})
_TEST_FAILS = _GATE_FAILS | {P.BLOCKED}
_SETTLE = frozenset({P.REWORK, P.FINAL_VERIFICATION, P.FAILED})
# The stages that can block on the environment, and so be unblocked back into.
UNBLOCKABLE = frozenset({P.UNIT_TESTING, P.INTEGRATION_TESTING})
# Where a paused run can go back to: any phase that waits on work, not on a person.
RESUMABLE = frozenset(
    {
        P.CREATED,
        P.VALIDATING,
        P.CONTEXT,
        P.PLANNING,
        P.IMPLEMENTING,
        P.STATIC_ANALYSIS,
        P.UNIT_TESTING,
        P.INTEGRATION_TESTING,
        P.SECURITY_REVIEW,
        P.CODE_REVIEW,
        P.NEGOTIATION,
        P.REWORK,
        P.FINAL_VERIFICATION,
        P.HUMAN_APPROVAL,
    }
)

TABLE = TransitionTable(
    (
        Rule(P.CREATED, RunAdmitted, frozenset({P.VALIDATING}), _run_admitted),
        Rule(P.CREATED, RunDenied, frozenset({P.FAILED}), _run_denied),
        Rule(P.VALIDATING, RequestValidated, frozenset({P.CONTEXT}), _request_validated),
        Rule(P.VALIDATING, RequestRejected, frozenset({P.FAILED}), _request_rejected),
        Rule(P.CONTEXT, ContextLoaded, frozenset({P.PLANNING}), _context_loaded),
        Rule(
            P.PLANNING, PlanProduced, frozenset({P.PLAN_APPROVAL, P.IMPLEMENTING}), _plan_produced
        ),
        Rule(P.PLAN_APPROVAL, PlanApproved, frozenset({P.IMPLEMENTING}), _plan_approved),
        Rule(P.PLAN_APPROVAL, PlanRejected, frozenset({P.PLANNING, P.FAILED}), _plan_rejected),
        Rule(P.PLAN_APPROVAL, HumanAborted, frozenset({P.CANCELLED}), _human_aborted),
        Rule(
            P.IMPLEMENTING,
            ImplementationCompleted,
            frozenset({P.IMPLEMENTING}),
            _implementation_completed,
        ),
        Rule(
            P.IMPLEMENTING,
            AcceptanceTestsAuthored,
            frozenset({P.IMPLEMENTING}),
            _acceptance_tests_authored,
        ),
        Rule(P.IMPLEMENTING, BuildIntegrated, frozenset({P.STATIC_ANALYSIS}), _build_integrated),
        Rule(
            P.STATIC_ANALYSIS,
            StaticAnalysisPassed,
            frozenset({P.UNIT_TESTING}),
            _static_analysis_passed,
        ),
        Rule(P.STATIC_ANALYSIS, StaticAnalysisFailed, _GATE_FAILS, _static_analysis_failed),
        Rule(
            P.UNIT_TESTING,
            UnitTestsPassed,
            frozenset({P.INTEGRATION_TESTING}),
            _unit_tests_passed,
        ),
        Rule(P.UNIT_TESTING, UnitTestsFailed, _TEST_FAILS, _unit_tests_failed),
        Rule(
            P.INTEGRATION_TESTING,
            IntegrationTestsPassed,
            frozenset({P.SECURITY_REVIEW}),
            _integration_tests_passed,
        ),
        Rule(P.INTEGRATION_TESTING, IntegrationTestsFailed, _TEST_FAILS, _integration_tests_failed),
        Rule(
            P.SECURITY_REVIEW,
            SecurityReviewPassed,
            frozenset({P.CODE_REVIEW}),
            _security_review_passed,
        ),
        Rule(P.SECURITY_REVIEW, SecurityReviewFailed, _GATE_FAILS, _security_review_failed),
        Rule(
            P.CODE_REVIEW,
            ReviewProduced,
            frozenset({P.NEGOTIATION, P.FINAL_VERIFICATION}),
            _review_produced,
        ),
        Rule(
            P.NEGOTIATION,
            NegotiationRoundCompleted,
            _SETTLE | {P.NEGOTIATION},
            _negotiation_round_completed,
        ),
        Rule(P.NEGOTIATION, ArbitrationRuled, _SETTLE | {P.BLOCKED}, _arbitration_ruled),
        Rule(P.BLOCKED, HumanRuled, _SETTLE, _human_ruled),
        Rule(P.BLOCKED, Unblocked, UNBLOCKABLE, _unblocked),
        Rule(P.BLOCKED, HumanAborted, frozenset({P.CANCELLED}), _human_aborted),
        Rule(P.PAUSED, Resumed, RESUMABLE, _resumed),
        Rule(P.PAUSED, HumanAborted, frozenset({P.CANCELLED}), _human_aborted),
        Rule(P.REWORK, RevisionCompleted, frozenset({P.STATIC_ANALYSIS}), _revision_completed),
        Rule(
            P.FINAL_VERIFICATION,
            ReleaseVerified,
            frozenset({P.HUMAN_APPROVAL}),
            _release_verified,
        ),
        Rule(
            P.FINAL_VERIFICATION,
            ReleaseVerificationFailed,
            _GATE_FAILS,
            _release_verification_failed,
        ),
        Rule(P.HUMAN_APPROVAL, ReleaseApproved, frozenset({P.HUMAN_APPROVAL}), _release_approved),
        Rule(P.HUMAN_APPROVAL, ReleaseRejected, _GATE_FAILS, _release_rejected),
        Rule(P.HUMAN_APPROVAL, HumanAborted, frozenset({P.CANCELLED}), _human_aborted),
        Rule(P.HUMAN_APPROVAL, ReleasePublished, frozenset({P.COMPLETED}), _release_published),
    )
)
