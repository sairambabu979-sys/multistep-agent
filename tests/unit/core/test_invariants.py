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
from msa.core.effects import ImplementPlan
from msa.core.events import ImplementationCompleted
from msa.core.ids import EffectId, FindingId
from msa.core.invariants import violations
from msa.core.state import Evidence, Finding, FindingStatus
from msa.core.values import Outcome, OutcomeReason, Phase, Severity
from tests.support.driver import Driver
from tests.support.factory import BUILD, finding, tree


@pytest.fixture
def negotiating() -> RunState:
    d = Driver()
    d.to_review()
    d.review(finding("f1", Severity.BLOCKER))
    assert d.phase is Phase.NEGOTIATING
    return d.state


@pytest.fixture
def finalizing() -> RunState:
    d = Driver()
    d.to_review()
    d.review()
    assert d.phase is Phase.FINALIZING
    return d.state


def corrupt(state: RunState, **updates: Any) -> tuple[str, ...]:
    return violations(state.model_copy(update=updates))


def test_valid_states_have_no_violations(negotiating: RunState, finalizing: RunState) -> None:
    assert violations(negotiating) == ()
    assert violations(finalizing) == ()


def test_terminal_shape(negotiating: RunState) -> None:
    done = Outcome(reason=OutcomeReason.COMPLETED)
    assert any("inconsistent with outcome" in v for v in corrupt(negotiating, outcome=done))
    assert any("belongs to" in v for v in corrupt(negotiating, phase=Phase.FAILED, outcome=done))
    assert any(
        "still has pending" in v for v in corrupt(negotiating, phase=Phase.DONE, outcome=done)
    )


def test_never_stuck(negotiating: RunState) -> None:
    assert any(
        "nothing pending" in v
        for v in corrupt(negotiating, pending=(), ledger=negotiating.ledger.release_all())
    )


def test_finalization_gate(finalizing: RunState) -> None:
    assert any("without verify+test" in v for v in corrupt(finalizing, evidence=Evidence()))
    open_finding = Finding(
        id=FindingId("x"), severity=Severity.BLOCKER, raised_on=BUILD, status=FindingStatus.OPEN
    )
    assert any("open blocking findings" in v for v in corrupt(finalizing, findings=(open_finding,)))


def test_evidence_must_match_the_head(negotiating: RunState) -> None:
    assert any(
        "without current verify+test" in v for v in corrupt(negotiating, head_tree=tree("other"))
    )
    assert any(
        "not verified" in v for v in corrupt(negotiating, phase=Phase.TESTING, evidence=Evidence())
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
        "counted revision" in v for v in corrupt(negotiating, phase=Phase.REVISING, revisions=0)
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
    done = {"phase": Phase.DONE, "outcome": Outcome(reason=OutcomeReason.COMPLETED)}
    only_pending = corrupt(negotiating, **done, ledger=negotiating.ledger.release_all())
    only_reserved = corrupt(negotiating, **done, pending=())
    assert PENDING_OR_RESERVED in only_pending
    assert PENDING_OR_RESERVED in only_reserved


def test_finalizing_with_only_one_kind_of_evidence_is_caught(finalizing: RunState) -> None:
    head = finalizing.require_head()
    message = f"finalizing without verify+test evidence for head {head}: "
    for evidence in (Evidence(verified=head), Evidence(tested=head)):
        assert message + str(evidence) in corrupt(finalizing, evidence=evidence)


def test_disputing_with_only_one_kind_of_evidence_is_caught(negotiating: RunState) -> None:
    head = negotiating.require_head()
    for evidence in (Evidence(verified=head), Evidence(tested=head)):
        assert "negotiating on a tree without current verify+test evidence" in corrupt(
            negotiating, evidence=evidence
        )


def test_static_messages_are_exact(negotiating: RunState) -> None:
    assert "testing a tree that was not verified" in corrupt(
        negotiating, phase=Phase.TESTING, evidence=Evidence()
    )
    assert "revising without a counted revision" in corrupt(
        negotiating, phase=Phase.REVISING, revisions=0
    )
    before_start = negotiating.started_at - timedelta(seconds=1)
    assert "updated_at precedes started_at" in corrupt(negotiating, updated_at=before_start)
    drifted = negotiating.deadline + timedelta(seconds=1)
    assert "deadline drifted from started_at + wall_clock" in corrupt(negotiating, deadline=drifted)
    ledger = negotiating.ledger
    over = ledger.model_copy(update={"spent": Spend(usd_micros=ledger.limit_usd_micros)})
    committed = ledger.limit_usd_micros + ledger.reserved_usd_micros
    assert f"committed {committed} > limit {ledger.limit_usd_micros} in a live run" in corrupt(
        negotiating, ledger=over
    )


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
