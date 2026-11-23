"""A small DSL for scripting runs in example-based tests.

d = Driver()
d.intake(); d.plan(); d.build(); d.gates()
d.review(finding("f1", Severity.BLOCKER))
d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=(...))
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from msa.core import RunState, Step, TransitionRejected, start, transition
from msa.core.effects import (
    AdmitRun,
    AuthorAcceptanceTests,
    AwaitReleaseApproval,
    Command,
    Effect,
    ImplementPlan,
    IntegrateBuild,
    LoadContext,
    PublishRelease,
    RequestPlan,
    RequestReview,
    RunIntegrationTests,
    RunSecurityReview,
    RunStaticAnalysis,
    RunUnitTests,
    ValidateRequest,
    VerifyRelease,
)
from msa.core.events import (
    AcceptanceTestsAuthored,
    BuildIntegrated,
    Completion,
    ContextLoaded,
    EventBase,
    ImplementationCompleted,
    IntegrationTestsPassed,
    PlanProduced,
    ReleaseApproved,
    ReleasePublished,
    ReleaseVerified,
    RequestValidated,
    ReviewProduced,
    RunAdmitted,
    SecurityReviewPassed,
    StaticAnalysisPassed,
    UnitTestsPassed,
)
from msa.core.ids import TreeHash
from msa.core.values import FindingRef, Limits, Phase
from tests.support.factory import BASE, BUILD, PERSON, T0, bundle, digest, plan_ref, run_started


class Driver:
    def __init__(self, run_limits: Limits | None = None, *, run_id: str = "run-1") -> None:
        self.clock = T0
        first = run_started(run_limits, run_id=run_id, at=self.clock)
        self.events: list[EventBase] = [first]
        self.steps: list[Step] = [start(first)]

    # ---------- inspection

    @property
    def state(self) -> RunState:
        return self.steps[-1].state

    @property
    def phase(self) -> Phase:
        return self.state.phase

    @property
    def head(self) -> TreeHash:
        return self.state.require_head()

    def expect(self, phase: Phase) -> None:
        """Assert the current phase. (A method, so mypy does not narrow ``phase`` across
        consecutive checks the way it would with ``assert d.phase is ...``.)"""
        assert self.phase is phase, f"expected {phase}, run is in {self.phase}"

    def phases(self) -> list[Phase]:
        return [s.state.phase for s in self.steps]

    def effect(self, command_type: type[Command]) -> Effect:
        matches = [e for e in self.state.pending if isinstance(e.command, command_type)]
        assert len(matches) == 1, (
            f"expected one pending {command_type.__name__}, "
            f"pending: {[e.command.kind for e in self.state.pending]}"
        )
        return matches[0]

    # ---------- delivering events

    def tick(self, seconds: float = 1) -> datetime:
        self.clock += timedelta(seconds=seconds)
        return self.clock

    def send(self, event: EventBase) -> Step:
        step = transition(self.state, event)
        self.events.append(event)
        self.steps.append(step)
        return step

    def answer(
        self, command_type: type[Command], event_type: type[Completion], **fields: Any
    ) -> Completion:
        """Build (without sending) the completion of the pending ``command_type`` effect."""
        effect = self.effect(command_type)
        return event_type.model_validate({"at": self.tick(), "effect_id": effect.id, **fields})

    def complete(
        self, command_type: type[Command], event_type: type[Completion], **fields: Any
    ) -> Step:
        return self.send(self.answer(command_type, event_type, **fields))

    def reject(self, event: EventBase) -> TransitionRejected:
        before = self.state
        with pytest.raises(TransitionRejected) as info:
            transition(before, event)
        assert self.state is before
        return info.value

    # ---------- canned stages

    def intake(self) -> Step:
        """CREATED through CONTEXT: admitted, validated, context loaded."""
        self.complete(AdmitRun, RunAdmitted)
        self.complete(ValidateRequest, RequestValidated)
        return self.complete(LoadContext, ContextLoaded, base_tree=BASE, context=digest("ctx"))

    def plan(self) -> Step:
        attempt = self.effect(RequestPlan).command
        assert isinstance(attempt, RequestPlan)
        return self.complete(RequestPlan, PlanProduced, plan=plan_ref(attempt.attempt))

    def build(self, head: TreeHash = BUILD) -> Step:
        self.complete(ImplementPlan, ImplementationCompleted)
        self.complete(AuthorAcceptanceTests, AcceptanceTestsAuthored)
        return self.complete(IntegrateBuild, BuildIntegrated, head_tree=head)

    def static(self) -> Step:
        return self.complete(RunStaticAnalysis, StaticAnalysisPassed, tree=self.head)

    def unit(self) -> Step:
        return self.complete(RunUnitTests, UnitTestsPassed, tree=self.head)

    def integration(self) -> Step:
        return self.complete(RunIntegrationTests, IntegrationTestsPassed, tree=self.head)

    def security(self) -> Step:
        return self.complete(
            RunSecurityReview, SecurityReviewPassed, tree=self.head, report=digest("sec")
        )

    def gates(self) -> Step:
        """The four mandatory gates, all passing on the head tree."""
        self.static()
        self.unit()
        self.integration()
        return self.security()

    def review(self, *findings: FindingRef) -> Step:
        return self.complete(RequestReview, ReviewProduced, tree=self.head, findings=findings)

    def to_review(self) -> Step:
        self.intake()
        self.plan()
        self.build()
        return self.gates()

    def verify_release(self) -> Step:
        return self.complete(
            VerifyRelease, ReleaseVerified, tree=self.head, bundle=bundle(self.head)
        )

    def approve_release(self, by: str = PERSON) -> Step:
        return self.complete(AwaitReleaseApproval, ReleaseApproved, by=by)

    def publish(self) -> Step:
        return self.complete(
            PublishRelease, ReleasePublished, tree=self.head, reference="aegis/run-1"
        )

    def release(self) -> Step:
        """FINAL_VERIFICATION through COMPLETED."""
        self.verify_release()
        self.approve_release()
        return self.publish()
