"""Effects: what the state machine asks the outside world to do.

The core never performs I/O. A transition returns ``Effect``s, and the interpreter (M2)
executes them and reports back with events. Each effect has a deterministic id, and spending
effects carry a budget reservation (see ``msa.core.budget``).

Two class-level flags describe each command:

* ``spends``: the command may invoke an LLM and so needs a budget reservation.
* ``awaits``: the state machine waits for a completion event. Fire-and-forget commands
  (cancellation, publishing the outcome) do not stay pending.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import Field, NonNegativeInt, PositiveInt

from msa.core._frozen import Frozen
from msa.core.ids import ValidDigest, ValidEffectId, ValidFindingId, ValidTreeHash
from msa.core.values import Outcome, Owner, PlanRef


class Command(Frozen):
    spends: ClassVar[bool] = False
    awaits: ClassVar[bool] = True


class PrepareWorkspace(Command):
    """Validate the task, snapshot the base tree, resolve preferences."""

    kind: Literal["prepare_workspace"] = "prepare_workspace"


class RequestPlan(Command):
    spends: ClassVar[bool] = True
    kind: Literal["request_plan"] = "request_plan"
    attempt: PositiveInt
    feedback: str | None = None


class AwaitPlanApproval(Command):
    kind: Literal["await_plan_approval"] = "await_plan_approval"
    plan: PlanRef


class ImplementPlan(Command):
    """Coder region of BUILDING."""

    spends: ClassVar[bool] = True
    kind: Literal["implement_plan"] = "implement_plan"
    plan: PlanRef
    base_tree: ValidTreeHash


class AuthorAcceptanceTests(Command):
    """Tester region of BUILDING. Written from the plan, not from the implementation."""

    spends: ClassVar[bool] = True
    kind: Literal["author_acceptance_tests"] = "author_acceptance_tests"
    plan: PlanRef
    base_tree: ValidTreeHash


class IntegrateBuild(Command):
    """Join the two BUILDING worktrees. Path ownership makes the merge conflict-free."""

    kind: Literal["integrate_build"] = "integrate_build"
    base_tree: ValidTreeHash


class RunVerification(Command):
    """Deterministic checks only: format, lint, types, preference checkers, unit tests."""

    kind: Literal["run_verification"] = "run_verification"
    tree: ValidTreeHash


class RunTestSuite(Command):
    """Full suite plus fail-before/pass-after against the base tree. Triage may use an LLM."""

    spends: ClassVar[bool] = True
    kind: Literal["run_test_suite"] = "run_test_suite"
    tree: ValidTreeHash
    base_tree: ValidTreeHash


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
    spends: ClassVar[bool] = True
    kind: Literal["request_arbitration"] = "request_arbitration"
    tree: ValidTreeHash
    findings: tuple[ValidFindingId, ...]


class AwaitHumanRuling(Command):
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


class Finalize(Command):
    kind: Literal["finalize"] = "finalize"
    tree: ValidTreeHash


class CancelEffects(Command):
    awaits: ClassVar[bool] = False
    kind: Literal["cancel_effects"] = "cancel_effects"
    effect_ids: tuple[ValidEffectId, ...]


class PublishOutcome(Command):
    awaits: ClassVar[bool] = False
    kind: Literal["publish_outcome"] = "publish_outcome"
    outcome: Outcome


AnyCommand = Annotated[
    PrepareWorkspace
    | RequestPlan
    | AwaitPlanApproval
    | ImplementPlan
    | AuthorAcceptanceTests
    | IntegrateBuild
    | RunVerification
    | RunTestSuite
    | RequestReview
    | RunNegotiationRound
    | RequestArbitration
    | AwaitHumanRuling
    | ReviseChanges
    | Finalize
    | CancelEffects
    | PublishOutcome,
    Field(discriminator="kind"),
]

COMMAND_TYPES: tuple[type[Command], ...] = (
    PrepareWorkspace,
    RequestPlan,
    AwaitPlanApproval,
    ImplementPlan,
    AuthorAcceptanceTests,
    IntegrateBuild,
    RunVerification,
    RunTestSuite,
    RequestReview,
    RunNegotiationRound,
    RequestArbitration,
    AwaitHumanRuling,
    ReviseChanges,
    Finalize,
    CancelEffects,
    PublishOutcome,
)


def command_kind(command_type: type[Command]) -> str:
    """The discriminator value of a command class."""
    default = command_type.model_fields["kind"].default
    if not isinstance(default, str):  # pragma: no cover - guarded by a contract test
        raise TypeError(f"{command_type.__name__} has no literal 'kind'")
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
