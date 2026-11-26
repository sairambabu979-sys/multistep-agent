"""A simulated outside world: deterministic stub executors for every effect.

Used by ``msa simulate``, by the crash-recovery (chaos) tests, and, through ``outcomes``,
by the exhaustive model checker. Every answer is a pure function of ``(seed, effect)``, so
re-executing an effect after a crash yields the same answer. That is why a crashed and
resumed simulation must end in exactly the same state as an uninterrupted one.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from itertools import product
from typing import Literal, assert_never, cast

from pydantic import NonNegativeInt

from msa.core._frozen import Frozen
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
    Effect,
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
    AnyEvent,
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
    SpendingCompletion,
    StaticAnalysisFailed,
    StaticAnalysisPassed,
    Unblocked,
    UnitTestsFailed,
    UnitTestsPassed,
)
from msa.core.ids import Digest, FindingId, PlanId, TreeHash
from msa.core.values import (
    FailureKind,
    FindingRef,
    Owner,
    PlanRef,
    Publication,
    PublishMode,
    Resolution,
    Severity,
    Triage,
    Verdict,
)

# ---------- deterministic identifiers


def tree(label: object) -> TreeHash:
    """A fake but well-formed git tree hash."""
    return TreeHash(hashlib.sha1(f"tree:{label}".encode(), usedforsecurity=False).hexdigest())


def digest(label: object) -> Digest:
    return Digest("sha256:" + hashlib.sha256(f"digest:{label}".encode()).hexdigest())


PERSON = "ada"  # who answers every human gate in a simulation
BASE = tree("base")
BUILD = tree("build")


def plan_ref(attempt: int = 1) -> PlanRef:
    return PlanRef(id=PlanId(f"plan-{attempt}"), digest=digest(f"plan-{attempt}"))


def finding(name: str, severity: Severity) -> FindingRef:
    return FindingRef(id=FindingId(name), severity=severity)


def bundle(head: TreeHash) -> Digest:
    """The release bundle verified for ``head``."""
    return digest(f"bundle-of-{head}")


def revised(previous: TreeHash) -> TreeHash:
    """The tree a revision produces. Deriving it from the revised tree (rather than from a
    counter) keeps answers a function of the effect alone, and still gives every revision
    count one distinct tree."""
    return tree(f"revision-of-{previous}")


# ---------- the answer model


def _review_sets(cycle: int, *, rich: bool) -> list[tuple[FindingRef, ...]]:
    def f(n: int, severity: Severity) -> FindingRef:
        return finding(f"c{cycle}-f{n}", severity)

    sets: list[tuple[FindingRef, ...]] = [
        (),
        (f(1, Severity.NIT),),
        (f(1, Severity.BLOCKER),),
        (f(1, Severity.BLOCKER), f(2, Severity.MAJOR)),
        # Three blocking findings: the only way a strictly progressing negotiation can
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


def _ruling_verdicts(*, rich: bool) -> tuple[Verdict | None, ...]:
    if rich:
        return (Verdict.UPHELD, Verdict.DISMISSED, Verdict.DEFERRED)
    return (Verdict.UPHELD, Verdict.DISMISSED)


def _triages(*, rich: bool) -> list[Triage]:
    triages = [
        Triage(kind=FailureKind.IMPL_BUG, owner=Owner.CODER),
        Triage(kind=FailureKind.TEST_BUG, owner=Owner.TESTER),
        Triage(kind=FailureKind.ENVIRONMENT, owner=None),
    ]
    if rich:
        triages.append(Triage(kind=FailureKind.FLAKY, owner=Owner.TESTER))
    return triages


def outcomes(effect: Effect, at: datetime, *, rich: bool = False) -> list[AnyEvent]:  # noqa: PLR0912 (one case per command kind)
    """Every class of answer to ``effect``. The first answer is always the success path.

    The default set has one representative per *behavioral* class. For example, the machine
    treats ACCEPTED and UPHELD identically (a change is required), and WITHDRAWN, DEFERRED
    and DISMISSED identically (no change). That makes it a sound basis for exhaustive
    exploration. ``rich`` adds the remaining variants (every verdict, flaky triage, a mixed
    review) for randomized tests, which exercise payload variety rather than coverage.

    People answer as ``PERSON``. Pausing is not an answer to any effect: it arrives from
    outside, and the model checker injects it separately.
    """
    eid = effect.id
    command = effect.command
    match command:
        case AdmitRun():
            return [
                RunAdmitted(at=at, effect_id=eid),
                RunDenied(at=at, effect_id=eid, reason="concurrent run quota reached"),
            ]
        case ValidateRequest():
            return [
                RequestValidated(at=at, effect_id=eid),
                RequestRejected(at=at, effect_id=eid, reason="not a git repository"),
            ]
        case LoadContext():
            return [ContextLoaded(at=at, effect_id=eid, base_tree=BASE, context=digest("context"))]
        case RequestPlan(attempt=attempt):
            return [PlanProduced(at=at, effect_id=eid, plan=plan_ref(attempt))]
        case AwaitPlanApproval():
            return [
                PlanApproved(at=at, effect_id=eid, by=PERSON),
                PlanRejected(at=at, effect_id=eid, by=PERSON, feedback="split step 2"),
                HumanAborted(at=at, effect_id=eid, by=PERSON, reason="not now"),
            ]
        case ImplementPlan():
            return [ImplementationCompleted(at=at, effect_id=eid)]
        case AuthorAcceptanceTests():
            return [AcceptanceTestsAuthored(at=at, effect_id=eid)]
        case IntegrateBuild():
            return [BuildIntegrated(at=at, effect_id=eid, head_tree=BUILD)]
        case RunStaticAnalysis(tree=t):
            return [
                StaticAnalysisPassed(at=at, effect_id=eid, tree=t),
                StaticAnalysisFailed(at=at, effect_id=eid, tree=t, report=digest("lint")),
            ]
        case RunUnitTests(tree=t):
            report = digest("unit")
            return [
                UnitTestsPassed(at=at, effect_id=eid, tree=t),
                *(
                    UnitTestsFailed(at=at, effect_id=eid, tree=t, triage=tr, report=report)
                    for tr in _triages(rich=rich)
                ),
            ]
        case RunIntegrationTests(tree=t):
            report = digest("integration")
            return [
                IntegrationTestsPassed(at=at, effect_id=eid, tree=t),
                *(
                    IntegrationTestsFailed(at=at, effect_id=eid, tree=t, triage=tr, report=report)
                    for tr in _triages(rich=rich)
                ),
            ]
        case RunSecurityReview(tree=t):
            return [
                SecurityReviewPassed(at=at, effect_id=eid, tree=t, report=digest("security")),
                *(
                    SecurityReviewFailed(
                        at=at, effect_id=eid, tree=t, report=digest("secret"), owner=owner
                    )
                    for owner in Owner
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
        case RequestArbitration(findings=ids, arbiter=arbiter):
            # Without the Arbiter role, the objective rungs may leave findings to a person.
            rulings = _ruling_verdicts(rich=rich)
            return [
                ArbitrationRuled(at=at, effect_id=eid, resolutions=rs)
                for rs in _verdict_combinations(ids, rulings if arbiter else (*rulings, None))
            ]
        case AwaitHumanRuling(findings=ids):
            return [
                *(
                    HumanRuled(at=at, effect_id=eid, by=PERSON, resolutions=rs)
                    for rs in _verdict_combinations(ids, _ruling_verdicts(rich=rich))
                ),
                HumanAborted(at=at, effect_id=eid, by=PERSON, reason="design call"),
            ]
        case ReviseChanges(tree=t):
            return [RevisionCompleted(at=at, effect_id=eid, head_tree=revised(t))]
        case VerifyRelease(tree=t):
            return [
                ReleaseVerified(at=at, effect_id=eid, tree=t, bundle=bundle(t)),
                ReleaseVerificationFailed(at=at, effect_id=eid, tree=t, report=digest("scope")),
            ]
        case AwaitReleaseApproval():
            return [
                ReleaseApproved(at=at, effect_id=eid, by=PERSON),
                ReleaseRejected(at=at, effect_id=eid, by=PERSON, feedback="rename the flag"),
                HumanAborted(at=at, effect_id=eid, by=PERSON, reason="superseded"),
            ]
        case PublishRelease(tree=t, publish=mode):
            published = _publication(mode, f"aegis/{t[:12]}", t)
            return [
                ReleasePublished(
                    at=at, effect_id=eid, tree=t, reference=published.branch, publication=published
                )
            ]
        case AwaitUnblock():
            return [
                Unblocked(at=at, effect_id=eid, by=PERSON, note="installed the tool"),
                HumanAborted(at=at, effect_id=eid, by=PERSON, reason="cannot fix it"),
            ]
        case AwaitResume():
            return [
                Resumed(at=at, effect_id=eid, by=PERSON),
                HumanAborted(at=at, effect_id=eid, by=PERSON, reason="stopped while paused"),
            ]
        case CancelEffects() | PublishOutcome():
            raise ValueError(f"{command.kind} is fire-and-forget and has no answer")
        case _:  # pragma: no cover - exhaustive over AnyCommand; mypy proves it
            assert_never(command)


# ---------- the stub executor


class Profile(StrEnum):
    HAPPY = "happy"  # always the success path
    MIXED = "mixed"  # weighted towards success, every other answer possible


class SimulationConfig(Frozen):
    """Everything needed to re-create the environment on resume. It is stored in the journal."""

    kind: Literal["simulation"] = "simulation"
    seed: int
    profile: Profile = Profile.MIXED
    max_call_usd_micros: NonNegativeInt = 50_000


SUCCESS_WEIGHT = 4


class SimulatedEnvironment:
    """Stub executors whose answers are a pure function of ``(seed, effect)``."""

    def __init__(
        self,
        config: SimulationConfig,
        *,
        on_execute: Callable[[Effect], None] | None = None,
    ) -> None:
        self.config = config
        self._on_execute = on_execute

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        if self._on_execute is not None:
            self._on_execute(effect)
        # Yield once, so concurrent effects interleave the way real I/O would.
        await asyncio.sleep(0)
        return self.answer(effect, now)

    def answer(self, effect: Effect, now: datetime) -> AnyEvent:
        rng = random.Random(f"{self.config.seed}:{effect.id}")
        options = outcomes(effect, now)
        if self.config.profile is Profile.HAPPY:
            event = options[0]
        else:
            weights = [SUCCESS_WEIGHT] + [1] * (len(options) - 1)
            event = rng.choices(options, weights=weights)[0]
        if isinstance(event, SpendingCompletion):
            cap = min(effect.budget_usd_micros or 0, self.config.max_call_usd_micros)
            spend = Spend(
                usd_micros=rng.randint(0, cap),
                input_tokens=rng.randint(1_000, 60_000),
                output_tokens=rng.randint(100, 8_000),
            )
            # model_copy preserves the concrete event class: still an AnyEvent member.
            return cast(AnyEvent, event.model_copy(update={"spend": spend}))
        return event


def _publication(mode: PublishMode, branch: str, commit: str) -> Publication:
    """Where a simulated release went: nowhere outside, whatever the mode asked for, but
    with the fields that mode records, so the API and the workbench can be exercised."""
    if mode is PublishMode.LOCAL:
        return Publication(mode=mode, branch=branch, commit=commit)
    pushed = Publication(
        mode=mode, branch=branch, commit=commit, remote="origin", remote_branch=branch
    )
    if mode is PublishMode.PUSH:
        return pushed
    return pushed.model_copy(
        update={"pr_url": f"https://github.invalid/simulated/pull/{commit[:7]}"}
    )
