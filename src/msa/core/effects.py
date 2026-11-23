"""Effects: what the state machine asks the outside world to do.

The core never performs I/O. A transition returns ``Effect``s, and the interpreter (M2)
executes them and reports back with events. Each effect has a deterministic id, and spending
effects carry a budget reservation (see ``msa.core.budget``).

Three class-level flags describe each command:

* ``spends``: the command may invoke an LLM and so needs a budget reservation.
* ``awaits``: the state machine waits for a completion event. Fire-and-forget commands
  (cancellation, publishing the outcome) do not stay pending.
* ``human``: a person answers it. The run's clock stops while one is pending: time spent
  waiting for a person is not work, and does not count against the wall-clock limit.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import Field, NonNegativeInt, PositiveInt

from msa.core._frozen import Frozen
from msa.core.ids import ValidDigest, ValidEffectId, ValidFindingId, ValidTreeHash
from msa.core.values import Outcome, Owner, Person, PlanRef


class Command(Frozen):
    spends: ClassVar[bool] = False
    awaits: ClassVar[bool] = True
    human: ClassVar[bool] = False


class AdmitRun(Command):
    """CREATED: admission control. Policy, quotas and the submitter's permissions."""

    kind: Literal["admit_run"] = "admit_run"


class ValidateRequest(Command):
    """VALIDATING: the repository and the task are usable; the preferences resolve."""

    kind: Literal["validate_request"] = "validate_request"


class LoadContext(Command):
    """CONTEXT: snapshot the base tree, build the repository's context, make worktrees."""

    kind: Literal["load_context"] = "load_context"


class RequestPlan(Command):
    spends: ClassVar[bool] = True
    kind: Literal["request_plan"] = "request_plan"
    attempt: PositiveInt
    feedback: str | None = None


class AwaitPlanApproval(Command):
    human: ClassVar[bool] = True
    kind: Literal["await_plan_approval"] = "await_plan_approval"
    plan: PlanRef


class ImplementPlan(Command):
    """Coder region of IMPLEMENTING."""

    spends: ClassVar[bool] = True
    kind: Literal["implement_plan"] = "implement_plan"
    plan: PlanRef
    base_tree: ValidTreeHash


class AuthorAcceptanceTests(Command):
    """Tester region of IMPLEMENTING. Written from the plan, not from the implementation."""

    spends: ClassVar[bool] = True
    kind: Literal["author_acceptance_tests"] = "author_acceptance_tests"
    plan: PlanRef
    base_tree: ValidTreeHash


class IntegrateBuild(Command):
    """Join the two IMPLEMENTING worktrees. Path ownership makes the merge conflict-free."""

    kind: Literal["integrate_build"] = "integrate_build"
    base_tree: ValidTreeHash


class RunStaticAnalysis(Command):
    """Deterministic checks only: format, lint, types, preference checkers."""

    kind: Literal["run_static_analysis"] = "run_static_analysis"
    tree: ValidTreeHash


class RunUnitTests(Command):
    """The repository's tests plus fail-before/pass-after acceptance tests. Triage may
    use an LLM."""

    spends: ClassVar[bool] = True
    kind: Literal["run_unit_tests"] = "run_unit_tests"
    tree: ValidTreeHash
    base_tree: ValidTreeHash


class RunIntegrationTests(Command):
    """The integration suite, in an isolated environment. Deterministic."""

    kind: Literal["run_integration_tests"] = "run_integration_tests"
    tree: ValidTreeHash


class RunSecurityReview(Command):
    """Secret scan, insecure-code rules, dependency audit. A gate: never negotiated."""

    kind: Literal["run_security_review"] = "run_security_review"
    tree: ValidTreeHash


class RequestReview(Command):
    spends: ClassVar[bool] = True
    kind: Literal["request_review"] = "request_review"
    tree: ValidTreeHash
    cycle: NonNegativeInt


class RunNegotiationRound(Command):
    spends: ClassVar[bool] = True
    kind: Literal["run_negotiation_round"] = "run_negotiation_round"
    tree: ValidTreeHash
    round: PositiveInt
    findings: tuple[ValidFindingId, ...]


class RequestArbitration(Command):
    """The arbitration ladder (PLAN §4.2), cheapest and most objective rung first: a repro
    test for correctness and security, the preference store for preferences, the author
    for uncovered style. With ``arbiter``, the Arbiter role rules on whatever those leave
    open, so the ruling covers every finding. Without it, a person does (BLOCKED)."""

    spends: ClassVar[bool] = True
    kind: Literal["request_arbitration"] = "request_arbitration"
    tree: ValidTreeHash
    findings: tuple[ValidFindingId, ...]
    arbiter: bool = True


class AwaitHumanRuling(Command):
    human: ClassVar[bool] = True
    kind: Literal["await_human_ruling"] = "await_human_ruling"
    tree: ValidTreeHash
    findings: tuple[ValidFindingId, ...]


class ReviseChanges(Command):
    spends: ClassVar[bool] = True
    kind: Literal["revise_changes"] = "revise_changes"
    owner: Owner
    tree: ValidTreeHash
    findings: tuple[ValidFindingId, ...] = ()
    failure_report: ValidDigest | None = None
    feedback: str | None = None  # a person's reason for sending the release back


class VerifyRelease(Command):
    """FINAL_VERIFICATION: every gate on this tree, no open finding, the scope, no secret;
    then the PR bundle (title, description, release notes, audit trail)."""

    kind: Literal["verify_release"] = "verify_release"
    tree: ValidTreeHash


class AwaitReleaseApproval(Command):
    human: ClassVar[bool] = True
    kind: Literal["await_release_approval"] = "await_release_approval"
    tree: ValidTreeHash
    bundle: ValidDigest


class PublishRelease(Command):
    """Commit the tree to a branch, with the bundle as its message. Only after a person
    approved, and in their name: the commit records who approved it."""

    kind: Literal["publish_release"] = "publish_release"
    tree: ValidTreeHash
    bundle: ValidDigest
    approved_by: Person


class AwaitUnblock(Command):
    """BLOCKED on the environment: a stage could not run (a missing tool or service). A
    person fixes it and unblocks, and the stage runs again."""

    human: ClassVar[bool] = True
    kind: Literal["await_unblock"] = "await_unblock"
    reason: str


class AwaitResume(Command):
    """PAUSED: an operator paused the run; it resumes where it was."""

    human: ClassVar[bool] = True
    kind: Literal["await_resume"] = "await_resume"


class CancelEffects(Command):
    awaits: ClassVar[bool] = False
    kind: Literal["cancel_effects"] = "cancel_effects"
    effect_ids: tuple[ValidEffectId, ...]


class PublishOutcome(Command):
    awaits: ClassVar[bool] = False
    kind: Literal["publish_outcome"] = "publish_outcome"
    outcome: Outcome


AnyCommand = Annotated[
    AdmitRun
    | ValidateRequest
    | LoadContext
    | RequestPlan
    | AwaitPlanApproval
    | ImplementPlan
    | AuthorAcceptanceTests
    | IntegrateBuild
    | RunStaticAnalysis
    | RunUnitTests
    | RunIntegrationTests
    | RunSecurityReview
    | RequestReview
    | RunNegotiationRound
    | RequestArbitration
    | AwaitHumanRuling
    | ReviseChanges
    | VerifyRelease
    | AwaitReleaseApproval
    | PublishRelease
    | AwaitUnblock
    | AwaitResume
    | CancelEffects
    | PublishOutcome,
    Field(discriminator="kind"),
]

COMMAND_TYPES: tuple[type[Command], ...] = (
    AdmitRun,
    ValidateRequest,
    LoadContext,
    RequestPlan,
    AwaitPlanApproval,
    ImplementPlan,
    AuthorAcceptanceTests,
    IntegrateBuild,
    RunStaticAnalysis,
    RunUnitTests,
    RunIntegrationTests,
    RunSecurityReview,
    RequestReview,
    RunNegotiationRound,
    RequestArbitration,
    AwaitHumanRuling,
    ReviseChanges,
    VerifyRelease,
    AwaitReleaseApproval,
    PublishRelease,
    AwaitUnblock,
    AwaitResume,
    CancelEffects,
    PublishOutcome,
)


def command_kind(command_type: type[Command]) -> str:
    """The discriminator value of a command class."""
    default = command_type.model_fields["kind"].default
    if not isinstance(default, str):  # pragma: no cover - guarded by a contract test
        raise TypeError(f"{command_type.__name__} has no literal 'kind'")  # pragma: no mutate
    return default


SPENDING_KINDS: frozenset[str] = frozenset(command_kind(c) for c in COMMAND_TYPES if c.spends)
NON_SPENDING_AWAITED_KINDS: frozenset[str] = frozenset(
    command_kind(c) for c in COMMAND_TYPES if c.awaits and not c.spends
)


class Effect(Frozen):
    """An addressed command: what to do, its idempotency key, and its spending cap."""

    id: ValidEffectId
    budget_usd_micros: PositiveInt | None
    command: AnyCommand
