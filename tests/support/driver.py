"""A small DSL for scripting runs in example-based tests.

d = Driver()
d.intake(); d.plan(); d.build(); d.verify(); d.test()
d.review(finding("f1", Severity.BLOCKER))
d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=(...))
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from msa.core import RunState, Step, TransitionRejected, start, transition
from msa.core.effects import (
    AuthorAcceptanceTests,
    Command,
    Effect,
    ImplementPlan,
    IntegrateBuild,
    PrepareWorkspace,
    RequestPlan,
    RequestReview,
    RunTestSuite,
    RunVerification,
)
from msa.core.events import (
    AcceptanceTestsAuthored,
    BuildIntegrated,
    Completion,
    EventBase,
    ImplementationCompleted,
    IntakeCompleted,
    PlanProduced,
    ReviewProduced,
    TestsPassed,
    VerificationPassed,
)
from msa.core.ids import TreeHash
from msa.core.values import FindingRef, Limits, Phase
from tests.support.factory import BASE, BUILD, T0, plan_ref, run_started


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
        return self.complete(PrepareWorkspace, IntakeCompleted, base_tree=BASE)

    def plan(self) -> Step:
        attempt = self.effect(RequestPlan).command
        assert isinstance(attempt, RequestPlan)
        return self.complete(RequestPlan, PlanProduced, plan=plan_ref(attempt.attempt))

    def build(self, head: TreeHash = BUILD) -> Step:
        self.complete(ImplementPlan, ImplementationCompleted)
        self.complete(AuthorAcceptanceTests, AcceptanceTestsAuthored)
        return self.complete(IntegrateBuild, BuildIntegrated, head_tree=head)

    def verify(self) -> Step:
        return self.complete(RunVerification, VerificationPassed, tree=self.head)

    def test(self) -> Step:
        return self.complete(RunTestSuite, TestsPassed, tree=self.head)

    def review(self, *findings: FindingRef) -> Step:
        return self.complete(RequestReview, ReviewProduced, tree=self.head, findings=findings)

    def to_review(self) -> Step:
        self.intake()
        self.plan()
        self.build()
        self.verify()
        return self.test()
