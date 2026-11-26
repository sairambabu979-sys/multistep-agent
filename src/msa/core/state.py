"""The run state: everything the orchestrator knows, as one immutable value."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

from pydantic import AwareDatetime, NonNegativeInt, PositiveInt

from msa.core._frozen import Frozen
from msa.core.budget import Ledger
from msa.core.effects import AnyCommand, Effect
from msa.core.errors import InvariantViolation
from msa.core.ids import (
    Digest,
    EffectId,
    FindingId,
    TreeHash,
    ValidDigest,
    ValidFindingId,
    ValidRunId,
    ValidTreeHash,
)
from msa.core.values import Limits, Outcome, Phase, PlanRef, Publication, Severity, Verdict


class FindingStatus(StrEnum):
    OPEN = "open"  # blocking and unresolved
    ADVISORY = "advisory"  # non-blocking (minor, nit); reported, never negotiated
    ACCEPTED = Verdict.ACCEPTED.value
    WITHDRAWN = Verdict.WITHDRAWN.value
    DEFERRED = Verdict.DEFERRED.value
    UPHELD = Verdict.UPHELD.value
    DISMISSED = Verdict.DISMISSED.value

    @classmethod
    def from_verdict(cls, verdict: Verdict) -> FindingStatus:
        return cls(verdict.value)

    @property
    def requires_change(self) -> bool:
        return self in {FindingStatus.ACCEPTED, FindingStatus.UPHELD}


class Finding(Frozen):
    id: ValidFindingId
    severity: Severity
    raised_on: ValidTreeHash
    status: FindingStatus


class BuildProgress(Frozen):
    """Completion of the two parallel BUILDING regions."""

    implementation_done: bool = False
    acceptance_tests_done: bool = False

    @property
    def complete(self) -> bool:
        return self.implementation_done and self.acceptance_tests_done


class Evidence(Frozen):
    """Which tree each gate passed for.

    Evidence is valid only for the tree it names. A revision moves ``head_tree`` and so
    invalidates all earlier evidence automatically. Nothing needs to be cleared.
    """

    static: ValidTreeHash | None = None
    unit: ValidTreeHash | None = None
    integration: ValidTreeHash | None = None
    security: ValidTreeHash | None = None

    def holds_through(self, gate: str, head: TreeHash | None) -> bool:
        """Every gate up to and including ``gate`` passed for ``head``."""
        gates = GATES[: GATES.index(gate) + 1]
        return head is not None and all(getattr(self, g) == head for g in gates)


GATES = ("static", "unit", "integration", "security")  # in the order they run


class RunState(Frozen):
    run_id: ValidRunId
    seq: PositiveInt  # number of events applied; the journal sequence number
    phase: Phase
    started_at: AwareDatetime
    updated_at: AwareDatetime
    deadline: AwareDatetime  # started_at + wall_clock + paused
    # The clock stops while a person is asked something (a plan gate, a ruling): since
    # when, and for how long in all. Each answer moves the deadline by its wait.
    waiting_since: AwareDatetime | None = None
    paused: timedelta = timedelta(0)
    task_digest: ValidDigest
    limits: Limits
    ledger: Ledger

    base_tree: ValidTreeHash | None = None
    head_tree: ValidTreeHash | None = None
    plan: PlanRef | None = None

    replans: NonNegativeInt = 0
    revisions: NonNegativeInt = 0
    negotiation_round: NonNegativeInt = 0

    build: BuildProgress = BuildProgress()
    evidence: Evidence = Evidence()
    findings: tuple[Finding, ...] = ()
    pending: tuple[Effect, ...] = ()
    outcome: Outcome | None = None

    # PAUSED and BLOCKED (on the environment) resume a phase by issuing its work again.
    resume_phase: Phase | None = None
    resume_commands: tuple[AnyCommand, ...] = ()
    release: ValidDigest | None = None  # the verified PR bundle for the head tree
    approved_by: str | None = None  # the person who approved the release
    publication: Publication | None = None  # where the approved release went (ADR-018)

    @property
    def is_terminal(self) -> bool:
        return self.phase.is_terminal

    @property
    def open_finding_ids(self) -> tuple[FindingId, ...]:
        return tuple(f.id for f in self.findings if f.status is FindingStatus.OPEN)

    @property
    def change_requests(self) -> tuple[FindingId, ...]:
        return tuple(f.id for f in self.findings if f.status.requires_change)

    def pending_effect(self, effect_id: EffectId) -> Effect | None:
        return next((e for e in self.pending if e.id == effect_id), None)

    def require_head(self) -> TreeHash:
        """The head tree. Only valid after IMPLEMENTING, which is enforced as an invariant."""
        if self.head_tree is None:
            raise InvariantViolation(("head_tree is not set before IMPLEMENTING completes",))
        return self.head_tree

    def require_base(self) -> TreeHash:
        if self.base_tree is None:
            raise InvariantViolation(("base_tree is not set before CONTEXT completes",))
        return self.base_tree

    def require_release(self) -> Digest:
        if self.release is None:
            raise InvariantViolation(("release is not set before FINAL_VERIFICATION passes",))
        return self.release

    def require_plan(self) -> PlanRef:
        if self.plan is None:
            raise InvariantViolation(("plan is not set before PLANNING completes",))
        return self.plan
