"""Value objects shared by state, events, and effects."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, NonNegativeInt, PositiveInt, model_validator

from msa.core._frozen import Frozen
from msa.core.ids import ValidDigest, ValidFindingId, ValidPlanId


class Phase(StrEnum):
    """Lifecycle phase of a run: the AEGIS workflow (ADR-015)."""

    CREATED = "created"  # submitted; waits for admission
    VALIDATING = "validating"  # the repository and the request are checked
    CONTEXT = "context"  # the base tree and the repository's context are loaded
    PLANNING = "planning"
    PLAN_APPROVAL = "plan_approval"  # a person approves the plan (plan_gate = human)
    IMPLEMENTING = "implementing"  # the Coder and the blind Tester, in parallel
    STATIC_ANALYSIS = "static_analysis"
    UNIT_TESTING = "unit_testing"
    INTEGRATION_TESTING = "integration_testing"
    SECURITY_REVIEW = "security_review"
    CODE_REVIEW = "code_review"
    NEGOTIATION = "negotiation"
    REWORK = "rework"  # the owner revises; every gate runs again after
    FINAL_VERIFICATION = "final_verification"  # the release verifier
    HUMAN_APPROVAL = "human_approval"  # a person approves the release; then it is published
    COMPLETED = "completed"
    PAUSED = "paused"  # an operator paused the run; it resumes where it was
    BLOCKED = "blocked"  # a person must rule on a dispute, or fix the environment
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_PHASES


TERMINAL_PHASES: frozenset[Phase] = frozenset({Phase.COMPLETED, Phase.FAILED, Phase.CANCELLED})


class PlanGate(StrEnum):
    """Whether a produced plan needs a human's approval before building starts."""

    AUTO = "auto"
    HUMAN = "human"


class Escalation(StrEnum):
    """Who rules on findings the negotiation could not settle."""

    HUMAN = "human"
    ARBITER = "arbiter"


def _positive_duration(value: timedelta) -> timedelta:
    if value <= timedelta(0):
        raise ValueError("wall_clock must be a positive duration")
    return value


class Limits(Frozen):
    """Hard bounds that guarantee every run terminates. See docs/PLAN.md §2.4 (invariant 3)."""

    budget_usd_micros: PositiveInt
    wall_clock: Annotated[timedelta, AfterValidator(_positive_duration)]
    max_replans: NonNegativeInt = 2
    max_revisions: NonNegativeInt = 4
    max_negotiation_rounds: PositiveInt = 3
    plan_gate: PlanGate = PlanGate.AUTO
    escalation: Escalation = Escalation.HUMAN


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"

    @property
    def is_blocking(self) -> bool:
        """Only blocking findings are negotiated; the rest are advisory and never block."""
        return self in {Severity.BLOCKER, Severity.MAJOR}


class Verdict(StrEnum):
    """How a finding was closed. See docs/PLAN.md §4.2."""

    ACCEPTED = "accepted"  # author agrees and will change the code
    WITHDRAWN = "withdrawn"  # reviewer retracts the finding
    DEFERRED = "deferred"  # out of scope; becomes a follow-up item
    UPHELD = "upheld"  # an arbiter (test, preference, LLM, or human) sided with the reviewer
    DISMISSED = "dismissed"  # an arbiter sided with the author

    @property
    def requires_change(self) -> bool:
        return self in {Verdict.ACCEPTED, Verdict.UPHELD}


NEGOTIATION_VERDICTS: frozenset[Verdict] = frozenset(Verdict)
RULING_VERDICTS: frozenset[Verdict] = frozenset(
    {Verdict.UPHELD, Verdict.DISMISSED, Verdict.DEFERRED}
)


class PlanRef(Frozen):
    id: ValidPlanId
    digest: ValidDigest


class FindingRef(Frozen):
    """The part of a reviewer finding the state machine needs. The full finding lives in
    the agent layer (M8); the orchestrator only cares about identity and severity."""

    id: ValidFindingId
    severity: Severity


class Resolution(Frozen):
    finding_id: ValidFindingId
    verdict: Verdict


class Owner(StrEnum):
    """Which agent role a revision is assigned to. Ownership is also a write capability."""

    CODER = "coder"
    TESTER = "tester"


class FailureKind(StrEnum):
    IMPL_BUG = "impl_bug"
    TEST_BUG = "test_bug"
    FLAKY = "flaky"
    ENVIRONMENT = "environment"


class Triage(Frozen):
    """The Tester's classification of a failing test run."""

    kind: FailureKind
    owner: Owner | None

    @model_validator(mode="after")
    def _owner_matches_kind(self) -> Self:
        if self.kind is FailureKind.ENVIRONMENT and self.owner is not None:
            raise ValueError("environment failures have no owner: no agent can fix them")
        if self.kind is not FailureKind.ENVIRONMENT and self.owner is None:
            raise ValueError(f"{self.kind} failures must name the owner who fixes them")
        return self


class OutcomeReason(StrEnum):
    COMPLETED = "completed"
    ADMISSION_DENIED = "admission_denied"
    REQUEST_REJECTED = "request_rejected"
    REPLAN_LIMIT = "replan_limit"
    REVISION_LIMIT = "revision_limit"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    AGENT_FAILED = "agent_failed"
    AGENT_REFUSED = "agent_refused"
    EFFECT_FAILED = "effect_failed"
    CANCELLED = "cancelled"
    HUMAN_ABORTED = "human_aborted"

    @property
    def phase(self) -> Phase:
        """The only terminal phase this reason is compatible with."""
        if self is OutcomeReason.COMPLETED:
            return Phase.COMPLETED
        if self in {OutcomeReason.CANCELLED, OutcomeReason.HUMAN_ABORTED}:
            return Phase.CANCELLED
        return Phase.FAILED


class Outcome(Frozen):
    reason: OutcomeReason
    detail: str = ""

    @property
    def phase(self) -> Phase:
        return self.reason.phase


AGENT_PREFIX = "agent:"


def _person(value: str) -> str:
    """A principal that may approve: a person, never an agent (ADR-015 §4)."""
    if not value.strip():
        raise ValueError("an approval names who gave it")
    if value.startswith(AGENT_PREFIX):
        raise ValueError(f"{value} is an agent: no agent may approve")
    return value


# Who approved, ruled or unblocked: always a person. Agents act as "agent:<role>".
Person = Annotated[str, AfterValidator(_person)]


class PublishMode(StrEnum):
    """What happens to an approved release once it is committed to its local branch
    (ADR-018). Chosen by the person who approves, and recorded with the approval."""

    LOCAL = "local"  # the local branch only: the person pushes it
    PUSH = "push"  # also push the branch to the remote
    PR = "pr"  # push it and open a pull request


class Publication(Frozen):
    """Where an approved release went (ADR-018). A push or a pull request that failed is
    recorded with its error, not raised: the release is committed either way."""

    mode: PublishMode
    branch: str  # the local branch holding the approved commit
    commit: str
    remote: str | None = None  # the remote it was pushed to
    remote_branch: str | None = None
    pr_url: str | None = None
    error: str | None = None  # why the push or the pull request did not happen

    @model_validator(mode="after")
    def _local_stays_local(self) -> Self:
        if self.mode is PublishMode.LOCAL and (self.remote or self.remote_branch or self.pr_url):
            raise ValueError("a local release names no remote, remote branch or pull request")
        return self
