from __future__ import annotations

import pytest

from msa.core import RunState
from msa.core.events import ReleasePublished, RunAdmitted
from msa.core.graph import analyze, render_markdown, render_mermaid
from msa.core.table import Decision, Rule, TransitionTable
from msa.core.transitions import RESUMABLE, TABLE
from msa.core.values import Phase


def _noop(state: RunState, _: object) -> Decision:  # pragma: no cover - never dispatched
    return Decision(state)


def test_the_production_table_is_sound() -> None:
    assert analyze(TABLE) == []


def test_analysis_reports_unreachable_and_doomed_phases() -> None:
    table = TransitionTable((Rule(Phase.CREATED, RunAdmitted, frozenset({Phase.FAILED}), _noop),))
    problems = analyze(table)
    assert "PLANNING is unreachable from CREATED" in problems
    assert "CREATED cannot reach COMPLETED through rules" in problems
    assert "live PLANNING has no outgoing rules" in problems


def test_analysis_reports_terminal_phases_with_outgoing_rules() -> None:
    table = TransitionTable(
        (Rule(Phase.COMPLETED, ReleasePublished, frozenset({Phase.FAILED}), _noop),)
    )
    assert "terminal COMPLETED has outgoing rules" in analyze(table)


def test_analysis_checks_every_terminal_phase_not_just_the_first() -> None:
    table = TransitionTable(
        (Rule(Phase.CANCELLED, ReleasePublished, frozenset({Phase.FAILED}), _noop),)
    )
    problems = analyze(table)
    assert "terminal CANCELLED has outgoing rules" in problems
    assert "FAILED is unreachable from CREATED" in problems


def test_table_rejects_duplicate_and_targetless_rules() -> None:
    rule = Rule(Phase.CREATED, RunAdmitted, frozenset({Phase.VALIDATING}), _noop)
    with pytest.raises(ValueError, match="duplicate rule"):
        TransitionTable((rule, rule))
    with pytest.raises(ValueError, match="no targets"):
        TransitionTable((Rule(Phase.CREATED, RunAdmitted, frozenset(), _noop),))


def test_mermaid_names_every_phase_and_omits_self_loops() -> None:
    diagram = render_mermaid(TABLE)
    assert diagram.startswith("stateDiagram-v2\n")
    for phase in Phase:
        assert phase.name in diagram
    assert "IMPLEMENTING --> IMPLEMENTING" not in diagram
    assert "[*] --> CREATED" in diagram


def test_pause_and_cancel_are_listed_as_escapes_and_a_pause_resumes_where_it_was() -> None:
    page = render_markdown(TABLE)
    assert "| `PauseRequested` | `PAUSED` |" in page
    assert "| `CancelRequested` | `CANCELLED` |" in page
    diagram = render_mermaid(TABLE)
    for phase in RESUMABLE:
        assert f"PAUSED --> {phase.name}: Resumed" in diagram


def test_rendering_is_deterministic() -> None:
    assert render_markdown(TABLE) == render_markdown(TABLE)


def test_markdown_lists_every_rule_including_self_loops() -> None:
    page = render_markdown(TABLE)
    for rule in TABLE:
        assert f"| `{rule.source.name}` | `{rule.event.__name__}` |" in page
