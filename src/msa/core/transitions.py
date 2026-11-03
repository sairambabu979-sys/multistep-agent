"""The orchestrator's transition rules. This module *is* the state machine's specification.

Each handler is a pure function ``(state, event) -> Decision``. Handlers may assume:

* the event completes an effect that was pending, of a kind the event may complete;
* the effect has already been removed from ``state.pending`` and its spend settled;
* global guards (cancel, deadline, budget, agent failure) have already been applied.

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
    Finalize,
    ImplementPlan,
    IntegrateBuild,
    RequestArbitration,
    RequestPlan,
    RequestReview,
    ReviseChanges,
    RunNegotiationRound,
    RunTestSuite,
    RunVerification,
)
from msa.core.errors import Rejection, TransitionRejected
from msa.core.events import (
    AcceptanceTestsAuthored,
    ArbitrationRuled,
    BuildIntegrated,
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
    ReviewProduced,
    RevisionCompleted,
    TestsFailed,
    TestsPassed,
    VerificationFailed,
    VerificationPassed,
)
from msa.core.ids import Digest, FindingId, TreeHash
from msa.core.state import BuildProgress, Evidence, Finding, FindingStatus, RunState
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
    Verdict,
)

# ---------- helpers


def _go(state: RunState, phase: Phase, *commands: AnyCommand, **updates: Any) -> Decision:
    return Decision(state.model_copy(update={"phase": phase, **updates}), commands)


def _stop(state: RunState, reason: OutcomeReason, detail: str) -> Decision:
    outcome = Outcome(reason=reason, detail=detail)
    return _go(state, outcome.phase, outcome=outcome)


def _require_current(state: RunState, tree: TreeHash, what: str) -> None:
    """Evidence is only admissible for the tree it was produced against."""
    head = state.require_head()
    if tree != head:
        raise TransitionRejected(
            Rejection.STALE_EVIDENCE, f"{what} is for tree {tree}, head is {head}"
        )


def _begin_build(state: RunState) -> Decision:
    plan, base = state.require_plan(), state.require_base()
    return _go(
        state,
        Phase.BUILDING,
        ImplementPlan(plan=plan, base_tree=base),
        AuthorAcceptanceTests(plan=plan, base_tree=base),
        build=BuildProgress(),  # pragma: no mutate (defensive: BUILDING is entered once)
    )


def _advance_build(state: RunState, build: BuildProgress) -> Decision:
    if build.complete:
        return _go(
            state, Phase.BUILDING, IntegrateBuild(base_tree=state.require_base()), build=build
        )
    return _go(state, Phase.BUILDING, build=build)


def _revisions_exhausted(state: RunState) -> bool:
    return state.revisions >= state.limits.max_revisions


def _begin_revision(
    state: RunState,
    owner: Owner,
    *,
    findings: tuple[FindingId, ...] = (),
    failure_report: Digest | None = None,
) -> Decision:
    if _revisions_exhausted(state):
        return _stop(
            state,
            OutcomeReason.REVISION_LIMIT,
            f"revision {state.revisions + 1} needed; limit is {state.limits.max_revisions}",
        )
    command = ReviseChanges(
        owner=owner, tree=state.require_head(), findings=findings, failure_report=failure_report
    )
    return _go(state, Phase.REVISING, command, revisions=state.revisions + 1)


def _finalize(state: RunState) -> Decision:
    return _go(state, Phase.FINALIZING, Finalize(tree=state.require_head()))


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
    """Apply a ruling (arbiter or human). A ruling must close every open finding, so it can
    never leave the run waiting on findings nobody will rule on again."""
    ruled = _resolve(state, resolutions, allowed=RULING_VERDICTS)
    if missing := ruled.open_finding_ids:
        raise TransitionRejected(Rejection.INCOMPLETE_RULING, f"no verdict for {sorted(missing)}")
    return ruled


def _settle(state: RunState) -> Decision:
    """All blocking findings are closed: revise if any closure demands a change."""
    if changes := state.change_requests:
        return _begin_revision(state, Owner.CODER, findings=changes)
    return _finalize(state)


def _escalate(state: RunState) -> Decision:
    tree, open_ids = state.require_head(), state.open_finding_ids
    if state.limits.escalation is Escalation.HUMAN:
        return _go(state, Phase.AWAITING_HUMAN, AwaitHumanRuling(tree=tree, findings=open_ids))
    return _go(state, Phase.NEGOTIATING, RequestArbitration(tree=tree, findings=open_ids))


# ---------- INTAKE


def _intake_completed(state: RunState, event: IntakeCompleted) -> Decision:
    return _go(state, Phase.PLANNING, RequestPlan(attempt=1), base_tree=event.base_tree)


def _intake_rejected(state: RunState, event: IntakeRejected) -> Decision:
    return _stop(state, OutcomeReason.INTAKE_REJECTED, event.reason)


# ---------- PLANNING / PLAN_GATE


def _plan_produced(state: RunState, event: PlanProduced) -> Decision:
    state = state.model_copy(update={"plan": event.plan})
    if state.limits.plan_gate is PlanGate.HUMAN:
        return _go(state, Phase.PLAN_GATE, AwaitPlanApproval(plan=event.plan))
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
    return _stop(state, OutcomeReason.HUMAN_ABORTED, event.reason)


# ---------- BUILDING


def _implementation_completed(state: RunState, _: ImplementationCompleted) -> Decision:
    return _advance_build(state, state.build.model_copy(update={"implementation_done": True}))


def _acceptance_tests_authored(state: RunState, _: AcceptanceTestsAuthored) -> Decision:
    return _advance_build(state, state.build.model_copy(update={"acceptance_tests_done": True}))


def _build_integrated(state: RunState, event: BuildIntegrated) -> Decision:
    return _go(
        state, Phase.VERIFYING, RunVerification(tree=event.head_tree), head_tree=event.head_tree
    )


# ---------- VERIFYING / TESTING


def _verification_passed(state: RunState, event: VerificationPassed) -> Decision:
    _require_current(state, event.tree, "verification result")
    return _go(
        state,
        Phase.TESTING,
        RunTestSuite(tree=event.tree, base_tree=state.require_base()),
        evidence=state.evidence.model_copy(update={"verified": event.tree}),
    )


def _verification_failed(state: RunState, event: VerificationFailed) -> Decision:
    _require_current(state, event.tree, "verification result")
    return _begin_revision(state, Owner.CODER, failure_report=event.report)


def _tests_passed(state: RunState, event: TestsPassed) -> Decision:
    _require_current(state, event.tree, "test result")
    evidence = Evidence(verified=state.evidence.verified, tested=event.tree)
    review = RequestReview(tree=event.tree, cycle=state.revisions)
    return _go(state, Phase.REVIEWING, review, evidence=evidence, negotiation_round=0)


def _tests_failed(state: RunState, event: TestsFailed) -> Decision:
    _require_current(state, event.tree, "test result")
    owner = event.triage.owner
    if owner is None:  # exactly the environment failures, by Triage's validator
        return _stop(state, OutcomeReason.ENVIRONMENT_FAILURE, f"test report {event.report}")
    return _begin_revision(state, owner, failure_report=event.report)


# ---------- REVIEWING / NEGOTIATING


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
        return _finalize(state)
    first_round = RunNegotiationRound(tree=event.tree, round=1, findings=open_ids)
    return _go(state, Phase.NEGOTIATING, first_round, negotiation_round=1)


def _negotiation_round_completed(state: RunState, event: NegotiationRoundCompleted) -> Decision:
    before = len(state.open_finding_ids)
    state = _resolve(state, event.resolutions, allowed=NEGOTIATION_VERDICTS)
    remaining = state.open_finding_ids
    # Settle as soon as the outcome is decided. With no revisions left, the first demanded
    # change already dooms the run, so more rounds (or a human's time) would be wasted.
    # Found by the exhaustive model checker (EF DONE failed for these states).
    if not remaining or (state.change_requests and _revisions_exhausted(state)):
        return _settle(state)
    made_progress = len(remaining) < before
    if made_progress and state.negotiation_round < state.limits.max_negotiation_rounds:
        next_round = state.negotiation_round + 1
        command = RunNegotiationRound(
            tree=state.require_head(), round=next_round, findings=remaining
        )
        return _go(state, Phase.NEGOTIATING, command, negotiation_round=next_round)
    return _escalate(state)


def _arbitration_ruled(state: RunState, event: ArbitrationRuled) -> Decision:
    return _settle(_rule(state, event.resolutions))


def _human_ruled(state: RunState, event: HumanRuled) -> Decision:
    return _settle(_rule(state, event.resolutions))


# ---------- REVISING / FINALIZING


def _revision_completed(state: RunState, event: RevisionCompleted) -> Decision:
    return _go(
        state, Phase.VERIFYING, RunVerification(tree=event.head_tree), head_tree=event.head_tree
    )


def _finalized(state: RunState, event: Finalized) -> Decision:
    _require_current(state, event.tree, "finalized artifact")
    return _stop(state, OutcomeReason.COMPLETED, f"artifact {event.artifact}")


# ---------- the table

P = Phase

TABLE = TransitionTable(
    (
        Rule(P.INTAKE, IntakeCompleted, frozenset({P.PLANNING}), _intake_completed),
        Rule(P.INTAKE, IntakeRejected, frozenset({P.FAILED}), _intake_rejected),
        Rule(P.PLANNING, PlanProduced, frozenset({P.PLAN_GATE, P.BUILDING}), _plan_produced),
        Rule(P.PLAN_GATE, PlanApproved, frozenset({P.BUILDING}), _plan_approved),
        Rule(P.PLAN_GATE, PlanRejected, frozenset({P.PLANNING, P.FAILED}), _plan_rejected),
        Rule(P.PLAN_GATE, HumanAborted, frozenset({P.ABORTED}), _human_aborted),
        Rule(
            P.BUILDING,
            ImplementationCompleted,
            frozenset({P.BUILDING}),
            _implementation_completed,
        ),
        Rule(
            P.BUILDING,
            AcceptanceTestsAuthored,
            frozenset({P.BUILDING}),
            _acceptance_tests_authored,
        ),
        Rule(P.BUILDING, BuildIntegrated, frozenset({P.VERIFYING}), _build_integrated),
        Rule(P.VERIFYING, VerificationPassed, frozenset({P.TESTING}), _verification_passed),
        Rule(
            P.VERIFYING,
            VerificationFailed,
            frozenset({P.REVISING, P.FAILED}),
            _verification_failed,
        ),
        Rule(P.TESTING, TestsPassed, frozenset({P.REVIEWING}), _tests_passed),
        Rule(P.TESTING, TestsFailed, frozenset({P.REVISING, P.FAILED}), _tests_failed),
        Rule(
            P.REVIEWING,
            ReviewProduced,
            frozenset({P.NEGOTIATING, P.FINALIZING}),
            _review_produced,
        ),
        Rule(
            P.NEGOTIATING,
            NegotiationRoundCompleted,
            frozenset({P.NEGOTIATING, P.AWAITING_HUMAN, P.REVISING, P.FINALIZING, P.FAILED}),
            _negotiation_round_completed,
        ),
        Rule(
            P.NEGOTIATING,
            ArbitrationRuled,
            frozenset({P.REVISING, P.FINALIZING, P.FAILED}),
            _arbitration_ruled,
        ),
        Rule(
            P.AWAITING_HUMAN,
            HumanRuled,
            frozenset({P.REVISING, P.FINALIZING, P.FAILED}),
            _human_ruled,
        ),
        Rule(P.AWAITING_HUMAN, HumanAborted, frozenset({P.ABORTED}), _human_aborted),
        Rule(P.REVISING, RevisionCompleted, frozenset({P.VERIFYING}), _revision_completed),
        Rule(P.FINALIZING, Finalized, frozenset({P.DONE}), _finalized),
    )
)
