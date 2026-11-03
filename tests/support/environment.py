"""A model of the outside world: every plausible answer to every effect the core can issue.

The exhaustive model checker and the Hypothesis tests share this model, so both explore the
same space of behaviors. Answers are grouped into *classes* (pass/fail, which verdicts, etc.)
rather than enumerating payload text, which the state machine never inspects.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import product
from typing import cast

from hypothesis import strategies as st

from msa.core import RunState
from msa.core.budget import Spend
from msa.core.effects import (
    AuthorAcceptanceTests,
    AwaitHumanRuling,
    AwaitPlanApproval,
    CancelEffects,
    Effect,
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
    AnyEvent,
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
    SpendingCompletion,
    TestsFailed,
    TestsPassed,
    VerificationFailed,
    VerificationPassed,
)
from msa.core.ids import FindingId
from msa.core.values import (
    Escalation,
    FailureKind,
    FindingRef,
    Limits,
    Owner,
    PlanGate,
    Resolution,
    Severity,
    Triage,
    Verdict,
)
from tests.support.factory import BASE, BUILD, digest, finding, plan_ref, tree

# ---------- deterministic answer classes


def _review_sets(cycle: int, *, rich: bool) -> list[tuple[FindingRef, ...]]:
    def f(n: int, severity: Severity) -> FindingRef:
        return finding(f"c{cycle}-f{n}", severity)

    sets: list[tuple[FindingRef, ...]] = [
        (),
        (f(1, Severity.NIT),),
        (f(1, Severity.BLOCKER),),
        (f(1, Severity.BLOCKER), f(2, Severity.MAJOR)),
        # Three blocking findings: the only way a strictly-progressing negotiation can
        # reach its third round.
        (f(1, Severity.BLOCKER), f(2, Severity.MAJOR), f(3, Severity.MAJOR), f(4, Severity.NIT)),
    ]
    if rich:
        sets.append((f(1, Severity.MAJOR), f(2, Severity.MINOR), f(3, Severity.NIT)))
    return sets


def _verdict_combinations(
    ids: tuple[FindingId, ...], verdicts: tuple[Verdict | None, ...]
) -> list[tuple[Resolution, ...]]:
    """Every assignment of a verdict (or None: still open) to each finding."""
    return [
        tuple(
            Resolution(finding_id=fid, verdict=v)
            for fid, v in zip(ids, combo, strict=True)
            if v is not None
        )
        for combo in product(verdicts, repeat=len(ids))
    ]


def outcomes(
    state: RunState, effect: Effect, at: datetime, *, rich: bool = False
) -> list[AnyEvent]:
    """Every class of answer to ``effect``.

    The default set has one representative per *behavioral* class. For example, the machine
    treats ACCEPTED and UPHELD identically (a change is required), and WITHDRAWN, DEFERRED
    and DISMISSED identically (no change). That makes it a sound basis for exhaustive
    exploration. ``rich`` adds the remaining variants (every verdict, flaky triage, a mixed
    review) for the randomized tests, which exercise payload variety rather than coverage.
    """
    eid = effect.id
    command = effect.command
    match command:
        case PrepareWorkspace():
            return [
                IntakeCompleted(at=at, effect_id=eid, base_tree=BASE),
                IntakeRejected(at=at, effect_id=eid, reason="not a git repository"),
            ]
        case RequestPlan(attempt=attempt):
            return [PlanProduced(at=at, effect_id=eid, plan=plan_ref(attempt))]
        case AwaitPlanApproval():
            return [
                PlanApproved(at=at, effect_id=eid),
                PlanRejected(at=at, effect_id=eid, feedback="split step 2"),
                HumanAborted(at=at, effect_id=eid, reason="not now"),
            ]
        case ImplementPlan():
            return [ImplementationCompleted(at=at, effect_id=eid)]
        case AuthorAcceptanceTests():
            return [AcceptanceTestsAuthored(at=at, effect_id=eid)]
        case IntegrateBuild():
            return [BuildIntegrated(at=at, effect_id=eid, head_tree=BUILD)]
        case RunVerification(tree=t):
            return [
                VerificationPassed(at=at, effect_id=eid, tree=t),
                VerificationFailed(at=at, effect_id=eid, tree=t, report=digest("lint")),
            ]
        case RunTestSuite(tree=t):
            triages = [
                Triage(kind=FailureKind.IMPL_BUG, owner=Owner.CODER),
                Triage(kind=FailureKind.TEST_BUG, owner=Owner.TESTER),
                Triage(kind=FailureKind.ENVIRONMENT, owner=None),
            ]
            if rich:
                triages.append(Triage(kind=FailureKind.FLAKY, owner=Owner.TESTER))
            report = digest("tests")
            return [
                TestsPassed(at=at, effect_id=eid, tree=t),
                *(
                    TestsFailed(at=at, effect_id=eid, tree=t, triage=tr, report=report)
                    for tr in triages
                ),
            ]
        case RequestReview(tree=t, cycle=cycle):
            return [
                ReviewProduced(at=at, effect_id=eid, tree=t, findings=fs)
                for fs in _review_sets(cycle, rich=rich)
            ]
        case RunNegotiationRound(findings=ids):
            verdicts: tuple[Verdict | None, ...] = (
                (None, *Verdict) if rich else (None, Verdict.ACCEPTED, Verdict.WITHDRAWN)
            )
            return [
                NegotiationRoundCompleted(at=at, effect_id=eid, resolutions=rs)
                for rs in _verdict_combinations(ids, verdicts)
            ]
        case RequestArbitration(findings=ids):
            return [
                ArbitrationRuled(at=at, effect_id=eid, resolutions=rs)
                for rs in _verdict_combinations(ids, _ruling_verdicts(rich=rich))
            ]
        case AwaitHumanRuling(findings=ids):
            return [
                *(
                    HumanRuled(at=at, effect_id=eid, resolutions=rs)
                    for rs in _verdict_combinations(ids, _ruling_verdicts(rich=rich))
                ),
                HumanAborted(at=at, effect_id=eid, reason="design call"),
            ]
        case ReviseChanges():
            # A unique tree per revision: the revision counter was bumped on entry.
            head = tree(f"revision-{state.revisions}")
            return [RevisionCompleted(at=at, effect_id=eid, head_tree=head)]
        case Finalize(tree=t):
            return [Finalized(at=at, effect_id=eid, tree=t, artifact=digest("patch"))]
        case CancelEffects() | PublishOutcome():  # pragma: no cover - never pending
            raise AssertionError(f"{command.kind} is fire-and-forget and is never pending")


def _ruling_verdicts(*, rich: bool) -> tuple[Verdict | None, ...]:
    if rich:
        return (Verdict.UPHELD, Verdict.DISMISSED, Verdict.DEFERRED)
    return (Verdict.UPHELD, Verdict.DISMISSED)


# ---------- Hypothesis strategies


def limits_strategy() -> st.SearchStrategy[Limits]:
    return st.builds(
        Limits,
        budget_usd_micros=st.one_of(st.integers(min_value=1, max_value=2_000_000), st.just(10**12)),
        wall_clock=st.integers(min_value=1, max_value=240).map(lambda m: timedelta(minutes=m)),
        max_replans=st.integers(min_value=0, max_value=2),
        max_revisions=st.integers(min_value=0, max_value=4),
        max_negotiation_rounds=st.integers(min_value=1, max_value=3),
        plan_gate=st.sampled_from(PlanGate),
        escalation=st.sampled_from(Escalation),
    )


def next_time(state: RunState) -> st.SearchStrategy[datetime]:
    return st.integers(min_value=0, max_value=600).map(
        lambda s: state.updated_at + timedelta(seconds=s)
    )


@st.composite
def answers(draw: st.DrawFn, state: RunState, effect: Effect, at: datetime) -> AnyEvent:
    """A plausible answer to ``effect``, with realistic spend and occasional agent failure."""
    failures: list[AnyEvent] = [
        AgentFailed(at=at, effect_id=effect.id, error="gateway retries exhausted"),
        AgentRefused(at=at, effect_id=effect.id, category="cyber"),
    ]
    if effect.command.spends and draw(st.integers(min_value=0, max_value=19)) == 0:
        event = draw(st.sampled_from(failures))
    else:
        event = draw(st.sampled_from(outcomes(state, effect, at, rich=True)))
    if not isinstance(event, SpendingCompletion):
        return event
    cap = effect.budget_usd_micros or 0
    overspend = draw(st.integers(min_value=0, max_value=9)) == 0
    usd = draw(
        st.integers(min_value=cap + 1, max_value=2 * cap + 1)
        if overspend
        else st.integers(min_value=0, max_value=cap)
    )
    spend = Spend(
        usd_micros=usd,
        input_tokens=draw(st.integers(min_value=0, max_value=200_000)),
        output_tokens=draw(st.integers(min_value=0, max_value=64_000)),
    )
    # model_copy preserves the concrete event class, so this is still an AnyEvent member.
    return cast(AnyEvent, event.model_copy(update={"spend": spend}))
