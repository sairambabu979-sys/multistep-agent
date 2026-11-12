"""The run state: everything the orchestrator knows, as one immutable value."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, NonNegativeInt, PositiveInt

from msa.core._frozen import Frozen
from msa.core.budget import Ledger
from msa.core.effects import Effect
from msa.core.errors import InvariantViolation
from msa.core.ids import (
    EffectId,
    FindingId,
    TreeHash,
    ValidDigest,
    ValidFindingId,
    ValidRunId,
    ValidTreeHash,
)
from msa.core.values import Limits, Outcome, Phase, PlanRef, Severity, Verdict


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
    """Which tree each kind of evidence was produced for.

    Evidence is valid only for the tree it names. A revision moves ``head_tree`` and so
    invalidates all earlier evidence automatically. Nothing needs to be cleared.
    """

    verified: ValidTreeHash | None = None
    tested: ValidTreeHash | None = None


class RunState(Frozen):
    run_id: ValidRunId
    seq: PositiveInt  # number of events applied; the journal sequence number
    phase: Phase
    started_at: AwareDatetime
    updated_at: AwareDatetime
    deadline: AwareDatetime
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
        """The head tree. Only valid after BUILDING, which is enforced as an invariant."""
        if self.head_tree is None:
            raise InvariantViolation(("head_tree is not set before BUILDING completes",))
        return self.head_tree

    def require_base(self) -> TreeHash:
        if self.base_tree is None:
            raise InvariantViolation(("base_tree is not set before INTAKE completes",))
        return self.base_tree

    def require_plan(self) -> PlanRef:
        if self.plan is None:
            raise InvariantViolation(("plan is not set before PLANNING completes",))
        return self.plan
