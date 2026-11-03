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
from msa.core.ids import Digest, FindingId, PlanId, TreeHash
from msa.core.values import (
    FailureKind,
    FindingRef,
    Owner,
    PlanRef,
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


BASE = tree("base")
BUILD = tree("build")


def plan_ref(attempt: int = 1) -> PlanRef:
    return PlanRef(id=PlanId(f"plan-{attempt}"), digest=digest(f"plan-{attempt}"))


def finding(name: str, severity: Severity) -> FindingRef:
    return FindingRef(id=FindingId(name), severity=severity)


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


def outcomes(effect: Effect, at: datetime, *, rich: bool = False) -> list[AnyEvent]:  # noqa: PLR0912 (one case per command kind)
    """Every class of answer to ``effect``. The first answer is always the success path.

    The default set has one representative per *behavioral* class. For example, the machine
    treats ACCEPTED and UPHELD identically (a change is required), and WITHDRAWN, DEFERRED
    and DISMISSED identically (no change). That makes it a sound basis for exhaustive
    exploration. ``rich`` adds the remaining variants (every verdict, flaky triage, a mixed
    review) for randomized tests, which exercise payload variety rather than coverage.
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
        case ReviseChanges(tree=t):
            return [RevisionCompleted(at=at, effect_id=eid, head_tree=revised(t))]
        case Finalize(tree=t):
            return [Finalized(at=at, effect_id=eid, tree=t, artifact=digest("patch"))]
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
