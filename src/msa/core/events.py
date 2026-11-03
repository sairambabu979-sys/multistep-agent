"""Events: facts reported to the state machine.

Most events are *completions*: they name the pending effect they answer, and declare
which command kinds they may complete. A completion for an effect that is not pending is
rejected. This makes redelivery idempotent and stops a stale answer from being applied to
the wrong question.

Time enters the core only through ``EventBase.at``. The core never reads a clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, ClassVar, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, field_validator

from msa.core._frozen import Frozen
from msa.core.budget import NO_SPEND, Spend
from msa.core.effects import SPENDING_KINDS
from msa.core.ids import (
    ValidDigest,
    ValidEffectId,
    ValidRunId,
    ValidTreeHash,
)
from msa.core.values import FindingRef, Limits, PlanRef, Resolution, Triage


class EventBase(Frozen):
    at: AwareDatetime


class Completion(EventBase):
    effect_id: ValidEffectId
    completes: ClassVar[frozenset[str]] = frozenset()


class SpendingCompletion(Completion):
    spend: Spend = NO_SPEND


def _unique_findings(values: Sequence[FindingRef]) -> None:
    ids = [v.id for v in values]
    if len(ids) != len(set(ids)):
        raise ValueError("finding ids must be unique")


def _unique_resolutions(values: Sequence[Resolution]) -> None:
    ids = [v.finding_id for v in values]
    if len(ids) != len(set(ids)):
        raise ValueError("a finding can be resolved at most once per event")


# ---------- lifecycle


class RunStarted(EventBase):
    kind: Literal["run_started"] = "run_started"
    run_id: ValidRunId
    task_digest: ValidDigest
    limits: Limits


class CancelRequested(EventBase):
    kind: Literal["cancel_requested"] = "cancel_requested"
    reason: str


class DeadlineExceeded(EventBase):
    kind: Literal["deadline_exceeded"] = "deadline_exceeded"


# ---------- intake and planning


class IntakeCompleted(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"prepare_workspace"})
    kind: Literal["intake_completed"] = "intake_completed"
    base_tree: ValidTreeHash


class IntakeRejected(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"prepare_workspace"})
    kind: Literal["intake_rejected"] = "intake_rejected"
    reason: str


class PlanProduced(SpendingCompletion):
    completes: ClassVar[frozenset[str]] = frozenset({"request_plan"})
    kind: Literal["plan_produced"] = "plan_produced"
    plan: PlanRef


class PlanApproved(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"await_plan_approval"})
    kind: Literal["plan_approved"] = "plan_approved"


class PlanRejected(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"await_plan_approval"})
    kind: Literal["plan_rejected"] = "plan_rejected"
    feedback: str


# ---------- building


class ImplementationCompleted(SpendingCompletion):
    completes: ClassVar[frozenset[str]] = frozenset({"implement_plan"})
    kind: Literal["implementation_completed"] = "implementation_completed"


class AcceptanceTestsAuthored(SpendingCompletion):
    completes: ClassVar[frozenset[str]] = frozenset({"author_acceptance_tests"})
    kind: Literal["acceptance_tests_authored"] = "acceptance_tests_authored"


class BuildIntegrated(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"integrate_build"})
    kind: Literal["build_integrated"] = "build_integrated"
    head_tree: ValidTreeHash


# ---------- verification and tests


class VerificationPassed(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"run_verification"})
    kind: Literal["verification_passed"] = "verification_passed"
    tree: ValidTreeHash


class VerificationFailed(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"run_verification"})
    kind: Literal["verification_failed"] = "verification_failed"
    tree: ValidTreeHash
    report: ValidDigest


class TestsPassed(SpendingCompletion):
    """All tests green on ``tree`` and fail-before/pass-after held against the base tree."""

    __test__ = False  # not a pytest test class
    completes: ClassVar[frozenset[str]] = frozenset({"run_test_suite"})
    kind: Literal["tests_passed"] = "tests_passed"
    tree: ValidTreeHash


class TestsFailed(SpendingCompletion):
    __test__ = False  # not a pytest test class
    completes: ClassVar[frozenset[str]] = frozenset({"run_test_suite"})
    kind: Literal["tests_failed"] = "tests_failed"
    tree: ValidTreeHash
    triage: Triage
    report: ValidDigest


# ---------- review and negotiation


class ReviewProduced(SpendingCompletion):
    completes: ClassVar[frozenset[str]] = frozenset({"request_review"})
    kind: Literal["review_produced"] = "review_produced"
    tree: ValidTreeHash
    findings: tuple[FindingRef, ...]

    @field_validator("findings")
    @classmethod
    def _unique(cls, value: tuple[FindingRef, ...]) -> tuple[FindingRef, ...]:
        _unique_findings(value)
        return value


class _Resolving(SpendingCompletion):
    resolutions: tuple[Resolution, ...]

    @field_validator("resolutions")
    @classmethod
    def _unique(cls, value: tuple[Resolution, ...]) -> tuple[Resolution, ...]:
        _unique_resolutions(value)
        return value


class NegotiationRoundCompleted(_Resolving):
    """One round of author moves and reviewer replies. Unresolved findings stay open."""

    completes: ClassVar[frozenset[str]] = frozenset({"run_negotiation_round"})
    kind: Literal["negotiation_round_completed"] = "negotiation_round_completed"


class ArbitrationRuled(_Resolving):
    """The Arbiter role's ruling. It must resolve every open finding."""

    completes: ClassVar[frozenset[str]] = frozenset({"request_arbitration"})
    kind: Literal["arbitration_ruled"] = "arbitration_ruled"


class HumanRuled(Completion):
    """A human's ruling. It must resolve every open finding."""

    completes: ClassVar[frozenset[str]] = frozenset({"await_human_ruling"})
    kind: Literal["human_ruled"] = "human_ruled"
    resolutions: tuple[Resolution, ...]

    @field_validator("resolutions")
    @classmethod
    def _unique(cls, value: tuple[Resolution, ...]) -> tuple[Resolution, ...]:
        _unique_resolutions(value)
        return value


class HumanAborted(Completion):
    """A human, asked to approve a plan or rule on a dispute, chose to stop the run."""

    completes: ClassVar[frozenset[str]] = frozenset({"await_plan_approval", "await_human_ruling"})
    kind: Literal["human_aborted"] = "human_aborted"
    reason: str = ""


# ---------- revision and finalization


class RevisionCompleted(SpendingCompletion):
    completes: ClassVar[frozenset[str]] = frozenset({"revise_changes"})
    kind: Literal["revision_completed"] = "revision_completed"
    head_tree: ValidTreeHash


class Finalized(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"finalize"})
    kind: Literal["finalized"] = "finalized"
    tree: ValidTreeHash
    artifact: ValidDigest


# ---------- agent failures (any spending effect)


class AgentFailed(SpendingCompletion):
    """An agent invocation failed after the gateway exhausted its retries."""

    completes: ClassVar[frozenset[str]] = SPENDING_KINDS
    kind: Literal["agent_failed"] = "agent_failed"
    error: str


class AgentRefused(SpendingCompletion):
    """The model declined, and so did every configured fallback model."""

    completes: ClassVar[frozenset[str]] = SPENDING_KINDS
    kind: Literal["agent_refused"] = "agent_refused"
    category: str | None = None


AnyEvent = Annotated[
    RunStarted
    | CancelRequested
    | DeadlineExceeded
    | IntakeCompleted
    | IntakeRejected
    | PlanProduced
    | PlanApproved
    | PlanRejected
    | ImplementationCompleted
    | AcceptanceTestsAuthored
    | BuildIntegrated
    | VerificationPassed
    | VerificationFailed
    | TestsPassed
    | TestsFailed
    | ReviewProduced
    | NegotiationRoundCompleted
    | ArbitrationRuled
    | HumanRuled
    | HumanAborted
    | RevisionCompleted
    | Finalized
    | AgentFailed
    | AgentRefused,
    Field(discriminator="kind"),
]

EVENT_TYPES: tuple[type[EventBase], ...] = (
    RunStarted,
    CancelRequested,
    DeadlineExceeded,
    IntakeCompleted,
    IntakeRejected,
    PlanProduced,
    PlanApproved,
    PlanRejected,
    ImplementationCompleted,
    AcceptanceTestsAuthored,
    BuildIntegrated,
    VerificationPassed,
    VerificationFailed,
    TestsPassed,
    TestsFailed,
    ReviewProduced,
    NegotiationRoundCompleted,
    ArbitrationRuled,
    HumanRuled,
    HumanAborted,
    RevisionCompleted,
    Finalized,
    AgentFailed,
    AgentRefused,
)

EVENT_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)


def spend_of(event: Completion) -> Spend:
    return event.spend if isinstance(event, SpendingCompletion) else NO_SPEND
