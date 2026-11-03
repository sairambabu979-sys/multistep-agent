"""Validation at the edges: malformed values are unrepresentable, not merely unexpected."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from msa.core.events import HumanRuled, ReviewProduced, RunStarted
from msa.core.ids import EffectId, FindingId
from msa.core.values import (
    FailureKind,
    Outcome,
    OutcomeReason,
    Owner,
    Phase,
    Resolution,
    Severity,
    Triage,
    Verdict,
)
from tests.support.factory import BUILD, T0, digest, finding, limits


def test_triage_requires_an_owner_unless_the_environment_is_at_fault() -> None:
    Triage(kind=FailureKind.ENVIRONMENT, owner=None)
    Triage(kind=FailureKind.IMPL_BUG, owner=Owner.CODER)
    with pytest.raises(ValidationError, match="must name the owner"):
        Triage(kind=FailureKind.IMPL_BUG, owner=None)
    with pytest.raises(ValidationError, match="no agent can fix them"):
        Triage(kind=FailureKind.ENVIRONMENT, owner=Owner.CODER)


@pytest.mark.parametrize(
    ("reason", "phase"),
    [
        (OutcomeReason.COMPLETED, Phase.DONE),
        (OutcomeReason.CANCELLED, Phase.ABORTED),
        (OutcomeReason.HUMAN_ABORTED, Phase.ABORTED),
        (OutcomeReason.BUDGET_EXHAUSTED, Phase.FAILED),
        (OutcomeReason.REVISION_LIMIT, Phase.FAILED),
    ],
)
def test_an_outcome_determines_its_terminal_phase(reason: OutcomeReason, phase: Phase) -> None:
    assert Outcome(reason=reason).phase is phase
    assert phase.is_terminal


@pytest.mark.parametrize(
    "overrides",
    [
        {"wall_clock": timedelta(0)},
        {"budget_usd_micros": 0},
        {"max_negotiation_rounds": 0},
        {"max_revisions": -1},
        {"surprise": 1},
    ],
)
def test_invalid_limits_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        limits(**overrides)


def test_only_blocker_and_major_findings_block() -> None:
    assert {s for s in Severity if s.is_blocking} == {Severity.BLOCKER, Severity.MAJOR}


def test_only_accepted_and_upheld_verdicts_require_a_change() -> None:
    assert {v for v in Verdict if v.requires_change} == {Verdict.ACCEPTED, Verdict.UPHELD}


def test_a_review_cannot_report_the_same_finding_twice() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ReviewProduced(
            at=T0,
            effect_id=EffectId("r:1:0"),
            tree=BUILD,
            findings=(finding("f", Severity.NIT), finding("f", Severity.MAJOR)),
        )


def test_a_ruling_cannot_resolve_the_same_finding_twice() -> None:
    twice = Resolution(finding_id=FindingId("f"), verdict=Verdict.UPHELD)
    with pytest.raises(ValidationError, match="at most once"):
        HumanRuled(at=T0, effect_id=EffectId("r:1:0"), resolutions=(twice, twice))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "has:colon"),  # would make effect ids ambiguous
        ("run_id", ""),
        ("task_digest", "md5:abc"),
    ],
)
def test_identifiers_are_validated(field: str, value: str) -> None:
    payload: dict[str, Any] = {
        "at": T0,
        "run_id": "run-1",
        "task_digest": digest("t"),
        "limits": limits(),
    }
    with pytest.raises(ValidationError):
        RunStarted.model_validate({**payload, field: value})


def test_events_require_timezone_aware_timestamps() -> None:
    with pytest.raises(ValidationError):
        RunStarted(
            at=T0.replace(tzinfo=None),
            run_id="run-1",  # type: ignore[arg-type]
            task_digest=digest("t"),
            limits=limits(),
        )


def test_tree_hashes_must_look_like_git_object_ids() -> None:
    with pytest.raises(ValidationError):
        ReviewProduced(at=T0, effect_id=EffectId("r:1:0"), tree="HEAD", findings=())  # type: ignore[arg-type]
