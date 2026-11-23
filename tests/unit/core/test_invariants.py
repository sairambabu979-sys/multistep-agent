"""Each invariant check must actually detect the corruption it guards against.

A check that never fires is indistinguishable from no check. Here every check is shown
a deliberately corrupted state and must complain about it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from msa.core import RunState
from msa.core.budget import Reservation, Spend
from msa.core.effects import (
    AwaitResume,
    ImplementPlan,
    RequestArbitration,
    RunNegotiationRound,
)
from msa.core.events import (
    ArbitrationRuled,
    ImplementationCompleted,
    NegotiationRoundCompleted,
    PauseRequested,
)
from msa.core.ids import EffectId, FindingId
from msa.core.invariants import violations
from msa.core.state import Evidence, Finding, FindingStatus
from msa.core.values import Outcome, OutcomeReason, Phase, PlanGate, Severity
from tests.support.driver import Driver
from tests.support.factory import BUILD, PERSON, bundle, finding, limits, tree


@pytest.fixture
def negotiating() -> RunState:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    assert d.phase is Phase.NEGOTIATION
    return d.state


@pytest.fixture
def finalizing() -> RunState:
    d = Driver()
    d.to_review()
    d.review()
    assert d.phase is Phase.FINAL_VERIFICATION
    return d.state


@pytest.fixture
def approving() -> RunState:
    d = Driver()
    d.to_review()
    d.review()
    d.verify_release()
    assert d.phase is Phase.HUMAN_APPROVAL
    return d.state


@pytest.fixture
def paused() -> RunState:
    d = Driver()
    d.to_review()
    d.send(PauseRequested(at=d.tick(), by=PERSON))
    assert d.phase is Phase.PAUSED
    return d.state


def corrupt(state: RunState, **updates: Any) -> tuple[str, ...]:
    return violations(state.model_copy(update=updates))


def test_valid_states_have_no_violations(
    negotiating: RunState, finalizing: RunState, approving: RunState, paused: RunState
) -> None:
    for state in (negotiating, finalizing, approving, paused):
        assert violations(state) == ()


def test_terminal_shape(negotiating: RunState) -> None:
    done = Outcome(reason=OutcomeReason.COMPLETED)
    assert any("inconsistent with outcome" in v for v in corrupt(negotiating, outcome=done))
    assert any("belongs to" in v for v in corrupt(negotiating, phase=Phase.FAILED, outcome=done))
    assert any(
        "still has pending" in v for v in corrupt(negotiating, phase=Phase.COMPLETED, outcome=done)
    )


def test_never_stuck(negotiating: RunState) -> None:
    assert any(
        "nothing pending" in v
        for v in corrupt(negotiating, pending=(), ledger=negotiating.ledger.release_all())
    )


def test_finalization_gate(finalizing: RunState, approving: RunState) -> None:
    assert any("without every gate passed" in v for v in corrupt(finalizing, evidence=Evidence()))
    assert any("without every gate passed" in v for v in corrupt(approving, evidence=Evidence()))
    assert "human_approval without a verified release bundle" in corrupt(approving, release=None)
    assert "an approval recorded before the release was verified" in corrupt(
        finalizing, approved_by=PERSON
    )
    open_finding = Finding(
        id=FindingId("x"), severity=Severity.BLOCKER, raised_on=BUILD, status=FindingStatus.OPEN
    )
    assert any("open blocking findings" in v for v in corrupt(finalizing, findings=(open_finding,)))


def test_a_release_bundle_and_an_approval_live_only_in_the_approval_stage(
    negotiating: RunState,
) -> None:
    assert "negotiation still holds a release bundle for an earlier tree" in corrupt(
        negotiating, release=bundle(BUILD)
    )
    assert "negotiation holds an approval outside the approval stage" in corrupt(
        negotiating, approved_by=PERSON
    )


def test_evidence_must_match_the_head(negotiating: RunState) -> None:
    assert any(
        "without security evidence" in v for v in corrupt(negotiating, head_tree=tree("other"))
    )


@pytest.mark.parametrize(
    ("phase", "gate", "missing"),
    [
        (Phase.UNIT_TESTING, "static", "static"),
        (Phase.INTEGRATION_TESTING, "unit", "static"),
        (Phase.INTEGRATION_TESTING, "unit", "unit"),
        (Phase.SECURITY_REVIEW, "integration", "integration"),
        (Phase.CODE_REVIEW, "security", "static"),
        (Phase.CODE_REVIEW, "security", "security"),
    ],
)
def test_each_gate_needs_every_earlier_gate_on_the_head(
    negotiating: RunState, phase: Phase, gate: str, missing: str
) -> None:
    """Dropping any one earlier gate's evidence is caught, not only dropping all of it."""
    head = negotiating.require_head()
    evidence = negotiating.evidence.model_copy(update={missing: None})
    assert f"{phase} on tree {head} without {gate} evidence for it: {evidence}" in corrupt(
        negotiating, phase=phase, evidence=evidence
    )


def test_bounded_work(negotiating: RunState) -> None:
    lim = negotiating.limits
    assert any("replans" in v for v in corrupt(negotiating, replans=lim.max_replans + 1))
    assert any("revisions" in v for v in corrupt(negotiating, revisions=lim.max_revisions + 1))
    assert any(
        "negotiation round" in v
        for v in corrupt(negotiating, negotiation_round=lim.max_negotiation_rounds + 1)
    )


def test_budget_safety(negotiating: RunState) -> None:
    ledger = negotiating.ledger
    overspent = ledger.model_copy(update={"spent": Spend(usd_micros=ledger.limit_usd_micros)})
    assert any("committed" in v for v in corrupt(negotiating, ledger=overspent))

    orphan = Reservation(effect_id=EffectId("run-1:999:0"), usd_micros=1)
    extra = ledger.model_copy(update={"reservations": (*ledger.reservations, orphan)})
    assert any("do not match" in v for v in corrupt(negotiating, ledger=extra))

    effect = negotiating.pending[0]
    wrong_cap = effect.model_copy(update={"budget_usd_micros": 1})
    assert any("cap" in v for v in corrupt(negotiating, pending=(wrong_cap,)))


def test_phase_prerequisites(negotiating: RunState) -> None:
    assert any("base tree" in v for v in corrupt(negotiating, base_tree=None))
    assert any("without a plan" in v for v in corrupt(negotiating, plan=None))
    assert any("head tree" in v for v in corrupt(negotiating, head_tree=None))
    assert any("no open findings" in v for v in corrupt(negotiating, findings=()))
    assert any(
        "counted revision" in v for v in corrupt(negotiating, phase=Phase.REWORK, revisions=0)
    )


def test_identities(negotiating: RunState) -> None:
    effect = negotiating.pending[0]
    assert any("duplicate effect" in v for v in corrupt(negotiating, pending=(effect, effect)))
    f = negotiating.findings[0]
    assert any("duplicate finding" in v for v in corrupt(negotiating, findings=(f, f)))
    nit_open = f.model_copy(update={"severity": Severity.NIT})
    assert any("non-blocking finding" in v for v in corrupt(negotiating, findings=(nit_open,)))
    blocker_advisory = f.model_copy(update={"status": FindingStatus.ADVISORY})
    assert any("is advisory" in v for v in corrupt(negotiating, findings=(blocker_advisory,)))


def test_clock(negotiating: RunState) -> None:
    before_start = negotiating.started_at - timedelta(seconds=1)
    assert any("precedes" in v for v in corrupt(negotiating, updated_at=before_start))
    assert any(
        "deadline drifted" in v
        for v in corrupt(negotiating, deadline=negotiating.deadline + timedelta(seconds=1))
    )


# ---------- one-sided corruptions and exact messages (found by mutation testing)

PENDING_OR_RESERVED = "terminal run still has pending effects or reservations"


def test_a_terminal_run_with_only_pending_or_only_reservations_is_caught(
    negotiating: RunState,
) -> None:
    done = {"phase": Phase.COMPLETED, "outcome": Outcome(reason=OutcomeReason.COMPLETED)}
    only_pending = corrupt(negotiating, **done, ledger=negotiating.ledger.release_all())
    only_reserved = corrupt(negotiating, **done, pending=())
    assert PENDING_OR_RESERVED in only_pending
    assert PENDING_OR_RESERVED in only_reserved


@pytest.mark.parametrize("missing", ["static", "unit", "integration", "security"])
def test_releasing_without_any_one_gate_is_caught(finalizing: RunState, missing: str) -> None:
    head = finalizing.require_head()
    evidence = finalizing.evidence.model_copy(update={missing: None})
    message = f"final_verification without every gate passed for head {head}: {evidence}"
    assert message in corrupt(finalizing, evidence=evidence)


def test_static_messages_are_exact(negotiating: RunState) -> None:
    assert "reworking without a counted revision" in corrupt(
        negotiating, phase=Phase.REWORK, revisions=0
    )
    assert "negotiation with no open findings to dispute" in corrupt(negotiating, findings=())
    before_start = negotiating.started_at - timedelta(seconds=1)
    assert "updated_at precedes started_at" in corrupt(negotiating, updated_at=before_start)
    drifted = negotiating.deadline + timedelta(seconds=1)
    assert "deadline drifted from started_at + wall_clock + paused" in corrupt(
        negotiating, deadline=drifted
    )
    assert "deadline drifted from started_at + wall_clock + paused" in corrupt(
        negotiating, paused=timedelta(seconds=1)
    )
    minus = timedelta(seconds=-1)
    assert "paused -1 day, 23:59:59 is negative" in corrupt(
        negotiating, paused=minus, deadline=negotiating.deadline + minus
    )
    since = negotiating.updated_at
    assert f"waiting_since is {since} while 0 human effect(s) are pending" in corrupt(
        negotiating, waiting_since=since
    )
    ledger = negotiating.ledger
    over = ledger.model_copy(update={"spent": Spend(usd_micros=ledger.limit_usd_micros)})
    committed = ledger.limit_usd_micros + ledger.reserved_usd_micros
    assert f"committed {committed} > limit {ledger.limit_usd_micros} in a live run" in corrupt(
        negotiating, ledger=over
    )


def test_a_person_is_asked_one_thing_at_a_time_and_the_wait_is_tracked() -> None:
    d = Driver(limits(plan_gate=PlanGate.HUMAN))
    d.intake()
    d.plan()
    gate = d.state
    assert violations(gate) == ()
    (asking,) = gate.pending
    twice = (asking, asking.model_copy(update={"id": EffectId(f"{asking.id}-again")}))
    assert "2 people asked at once" in corrupt(gate, pending=twice)
    assert "waiting_since is None while 1 human effect(s) are pending" in corrupt(
        gate, waiting_since=None
    )
    later = gate.updated_at + timedelta(seconds=1)
    assert "waiting_since is later than updated_at" in corrupt(gate, waiting_since=later)


def test_build_progress_must_match_pending_work() -> None:
    d = Driver()
    d.intake()
    d.plan()
    building = d.state
    assert violations(building) == ()
    claims_done = building.build.model_copy(update={"implementation_done": True})
    assert "build progress for implement_plan is True while it is pending" in corrupt(
        building, build=claims_done
    )
    d.complete(ImplementPlan, ImplementationCompleted)
    forgot = d.state.build.model_copy(update={"implementation_done": False})
    assert "build progress for implement_plan is False while it is not pending" in corrupt(
        d.state, build=forgot
    )


# ---------- interruptions: PAUSED and BLOCKED


def test_a_paused_run_knows_what_it_resumes(paused: RunState) -> None:
    pending = sorted(e.command.kind for e in paused.pending)
    assert f"paused with resume_phase None (pending: {pending})" in corrupt(
        paused, resume_phase=None
    )
    assert "paused has nothing to resume" in corrupt(paused, resume_commands=())


def test_only_an_interrupted_run_carries_resume_state(negotiating: RunState) -> None:
    pending = sorted(e.command.kind for e in negotiating.pending)
    assert f"negotiation with resume_phase code_review (pending: {pending})" in corrupt(
        negotiating, resume_phase=Phase.CODE_REVIEW
    )
    assert "negotiation carries resume commands it will never issue" in corrupt(
        negotiating, resume_commands=(AwaitResume(),)
    )


def test_a_paused_run_waits_on_its_operator_and_nothing_else(
    paused: RunState, negotiating: RunState
) -> None:
    extra = (*paused.pending, *negotiating.pending)
    kinds = sorted(e.command.kind for e in extra)
    assert f"paused but waiting on {kinds}" in corrupt(
        paused, pending=extra, ledger=negotiating.ledger
    )


def test_a_blocked_run_asks_one_question(negotiating: RunState) -> None:
    assert "blocked on []: exactly one question at a time" in corrupt(
        negotiating, phase=Phase.BLOCKED, pending=(), ledger=negotiating.ledger.release_all()
    )


def test_a_paused_run_is_held_to_the_rules_of_the_stage_it_paused_in(paused: RunState) -> None:
    """Evidence and prerequisites are judged by the stage a paused run will resume."""
    assert any("without security evidence" in v for v in corrupt(paused, evidence=Evidence()))
    assert "code_review without a head tree" in corrupt(paused, head_tree=None)


@pytest.fixture
def ruling() -> RunState:
    """BLOCKED on a person's ruling: still a dispute about the head tree."""
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.complete(RequestArbitration, ArbitrationRuled, resolutions=())
    assert d.phase is Phase.BLOCKED
    return d.state


def test_a_ruling_is_held_to_the_rules_of_a_dispute(ruling: RunState) -> None:
    assert violations(ruling) == ()
    head = ruling.require_head()
    evidence = ruling.evidence.model_copy(update={"security": None})
    assert f"blocked on tree {head} without security evidence for it: {evidence}" in corrupt(
        ruling, evidence=evidence
    )
    assert "blocked with no open findings to dispute" in corrupt(ruling, findings=())
