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
from msa.core.effects import NON_SPENDING_AWAITED_KINDS, SPENDING_KINDS
from msa.core.ids import (
    ValidDigest,
    ValidEffectId,
    ValidRunId,
    ValidTreeHash,
)
from msa.core.values import FindingRef, Limits, Person, PlanRef, Resolution, Triage


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


class PauseRequested(EventBase):
    """An operator pauses the run: in-flight work is cancelled, and it resumes where it
    was. Not while a person is already being asked something."""

    kind: Literal["pause_requested"] = "pause_requested"
    by: Person
    reason: str = ""


# ---------- admission, validation, context


class RunAdmitted(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"admit_run"})
    kind: Literal["run_admitted"] = "run_admitted"


class RunDenied(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"admit_run"})
    kind: Literal["run_denied"] = "run_denied"
    reason: str


class RequestValidated(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"validate_request"})
    kind: Literal["request_validated"] = "request_validated"


class RequestRejected(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"validate_request"})
    kind: Literal["request_rejected"] = "request_rejected"
    reason: str


class ContextLoaded(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"load_context"})
    kind: Literal["context_loaded"] = "context_loaded"
    base_tree: ValidTreeHash
    context: ValidDigest  # the repository context the Planner reads


# ---------- planning


class PlanProduced(SpendingCompletion):
    completes: ClassVar[frozenset[str]] = frozenset({"request_plan"})
    kind: Literal["plan_produced"] = "plan_produced"
    plan: PlanRef


class PlanApproved(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"await_plan_approval"})
    kind: Literal["plan_approved"] = "plan_approved"
    by: Person


class PlanRejected(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"await_plan_approval"})
    kind: Literal["plan_rejected"] = "plan_rejected"
    by: Person
    feedback: str


# ---------- implementing


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


# ---------- the gates: every one is evidence for exactly one tree


class StaticAnalysisPassed(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"run_static_analysis"})
    kind: Literal["static_analysis_passed"] = "static_analysis_passed"
    tree: ValidTreeHash


class StaticAnalysisFailed(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"run_static_analysis"})
    kind: Literal["static_analysis_failed"] = "static_analysis_failed"
    tree: ValidTreeHash
    report: ValidDigest


class UnitTestsPassed(SpendingCompletion):
    """Spending: a run that passes may still have paid to triage an earlier failure."""

    completes: ClassVar[frozenset[str]] = frozenset({"run_unit_tests"})
    kind: Literal["unit_tests_passed"] = "unit_tests_passed"
    tree: ValidTreeHash


class UnitTestsFailed(SpendingCompletion):
    __test__ = False  # not a pytest test class
    completes: ClassVar[frozenset[str]] = frozenset({"run_unit_tests"})
    kind: Literal["unit_tests_failed"] = "unit_tests_failed"
    tree: ValidTreeHash
    triage: Triage
    report: ValidDigest


class IntegrationTestsPassed(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"run_integration_tests"})
    kind: Literal["integration_tests_passed"] = "integration_tests_passed"
    tree: ValidTreeHash


class IntegrationTestsFailed(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"run_integration_tests"})
    kind: Literal["integration_tests_failed"] = "integration_tests_failed"
    tree: ValidTreeHash
    triage: Triage
    report: ValidDigest


class SecurityReviewPassed(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"run_security_review"})
    kind: Literal["security_review_passed"] = "security_review_passed"
    tree: ValidTreeHash
    report: ValidDigest


class SecurityReviewFailed(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"run_security_review"})
    kind: Literal["security_review_failed"] = "security_review_failed"
    tree: ValidTreeHash
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
    """The ladder's ruling: complete with the Arbiter role, partial without it."""

    completes: ClassVar[frozenset[str]] = frozenset({"request_arbitration"})
    kind: Literal["arbitration_ruled"] = "arbitration_ruled"


class HumanRuled(Completion):
    """A person's ruling. It must resolve every open finding."""

    completes: ClassVar[frozenset[str]] = frozenset({"await_human_ruling"})
    kind: Literal["human_ruled"] = "human_ruled"
    by: Person
    resolutions: tuple[Resolution, ...]

    @field_validator("resolutions")
    @classmethod
    def _unique(cls, value: tuple[Resolution, ...]) -> tuple[Resolution, ...]:
        _unique_resolutions(value)
        return value


# ---------- rework, release, people


class RevisionCompleted(SpendingCompletion):
    completes: ClassVar[frozenset[str]] = frozenset({"revise_changes"})
    kind: Literal["revision_completed"] = "revision_completed"
    head_tree: ValidTreeHash


class ReleaseVerified(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"verify_release"})
    kind: Literal["release_verified"] = "release_verified"
    tree: ValidTreeHash
    bundle: ValidDigest  # the PR: title, description, release notes, audit trail, patch


class ReleaseVerificationFailed(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"verify_release"})
    kind: Literal["release_verification_failed"] = "release_verification_failed"
    tree: ValidTreeHash
    report: ValidDigest


class ReleaseApproved(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"await_release_approval"})
    kind: Literal["release_approved"] = "release_approved"
    by: Person


class ReleaseRejected(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"await_release_approval"})
    kind: Literal["release_rejected"] = "release_rejected"
    by: Person
    feedback: str


class ReleasePublished(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"publish_release"})
    kind: Literal["release_published"] = "release_published"
    tree: ValidTreeHash
    reference: str  # the branch, commit and PR that hold it


class Unblocked(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"await_unblock"})
    kind: Literal["unblocked"] = "unblocked"
    by: Person
    note: str = ""


class Resumed(Completion):
    completes: ClassVar[frozenset[str]] = frozenset({"await_resume"})
    kind: Literal["resumed"] = "resumed"
    by: Person


class HumanAborted(Completion):
    """A person, asked something, chose to stop the run."""

    completes: ClassVar[frozenset[str]] = frozenset(
        {
            "await_plan_approval",
            "await_human_ruling",
            "await_release_approval",
            "await_unblock",
            "await_resume",
        }
    )
    kind: Literal["human_aborted"] = "human_aborted"
    by: Person
    reason: str = ""


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


class EffectFailed(Completion):
    """A non-LLM effect (a stage, a merge, publishing) failed in its executor.

    Without it, an executor crash would leave its effect pending forever and the run would
    stall until the deadline. With it, the run fails fast and says why.
    """

    completes: ClassVar[frozenset[str]] = NON_SPENDING_AWAITED_KINDS
    kind: Literal["effect_failed"] = "effect_failed"
    error: str


AnyEvent = Annotated[
    RunStarted
    | CancelRequested
    | DeadlineExceeded
    | PauseRequested
    | RunAdmitted
    | RunDenied
    | RequestValidated
    | RequestRejected
    | ContextLoaded
    | PlanProduced
    | PlanApproved
    | PlanRejected
    | ImplementationCompleted
    | AcceptanceTestsAuthored
    | BuildIntegrated
    | StaticAnalysisPassed
    | StaticAnalysisFailed
    | UnitTestsPassed
    | UnitTestsFailed
    | IntegrationTestsPassed
    | IntegrationTestsFailed
    | SecurityReviewPassed
    | SecurityReviewFailed
    | ReviewProduced
    | NegotiationRoundCompleted
    | ArbitrationRuled
    | HumanRuled
    | RevisionCompleted
    | ReleaseVerified
    | ReleaseVerificationFailed
    | ReleaseApproved
    | ReleaseRejected
    | ReleasePublished
    | Unblocked
    | Resumed
    | HumanAborted
    | AgentFailed
    | AgentRefused
    | EffectFailed,
    Field(discriminator="kind"),
]

EVENT_TYPES: tuple[type[EventBase], ...] = (
    RunStarted,
    CancelRequested,
    DeadlineExceeded,
    PauseRequested,
    RunAdmitted,
    RunDenied,
    RequestValidated,
    RequestRejected,
    ContextLoaded,
    PlanProduced,
    PlanApproved,
    PlanRejected,
    ImplementationCompleted,
    AcceptanceTestsAuthored,
    BuildIntegrated,
    StaticAnalysisPassed,
    StaticAnalysisFailed,
    UnitTestsPassed,
    UnitTestsFailed,
    IntegrationTestsPassed,
    IntegrationTestsFailed,
    SecurityReviewPassed,
    SecurityReviewFailed,
    ReviewProduced,
    NegotiationRoundCompleted,
    ArbitrationRuled,
    HumanRuled,
    RevisionCompleted,
    ReleaseVerified,
    ReleaseVerificationFailed,
    ReleaseApproved,
    ReleaseRejected,
    ReleasePublished,
    Unblocked,
    Resumed,
    HumanAborted,
    AgentFailed,
    AgentRefused,
    EffectFailed,
)

EVENT_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)


def spend_of(event: Completion) -> Spend:
    return event.spend if isinstance(event, SpendingCompletion) else NO_SPEND
