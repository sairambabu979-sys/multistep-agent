from __future__ import annotations

import pytest

from msa.core import RunState
from msa.core.events import Finalized, IntakeCompleted
from msa.core.graph import analyze, render_markdown, render_mermaid
from msa.core.table import Decision, Rule, TransitionTable
from msa.core.transitions import TABLE
from msa.core.values import Phase


def _noop(state: RunState, _: object) -> Decision:  # pragma: no cover - never dispatched
    return Decision(state)


def test_the_production_table_is_sound() -> None:
    assert analyze(TABLE) == []


def test_analysis_reports_unreachable_and_doomed_phases() -> None:
    table = TransitionTable(
        (Rule(Phase.INTAKE, IntakeCompleted, frozenset({Phase.FAILED}), _noop),)
    )
    problems = analyze(table)
    assert "PLANNING is unreachable from INTAKE" in problems
    assert "INTAKE cannot reach DONE through rules" in problems
    assert "live PLANNING has no outgoing rules" in problems


def test_analysis_reports_terminal_phases_with_outgoing_rules() -> None:
    table = TransitionTable((Rule(Phase.DONE, Finalized, frozenset({Phase.FAILED}), _noop),))
    assert "terminal DONE has outgoing rules" in analyze(table)


def test_analysis_checks_every_terminal_phase_not_just_the_first() -> None:
    table = TransitionTable((Rule(Phase.ABORTED, Finalized, frozenset({Phase.FAILED}), _noop),))
    problems = analyze(table)
    assert "terminal ABORTED has outgoing rules" in problems
    assert "FAILED is unreachable from INTAKE" in problems


def test_table_rejects_duplicate_and_targetless_rules() -> None:
    rule = Rule(Phase.INTAKE, IntakeCompleted, frozenset({Phase.PLANNING}), _noop)
    with pytest.raises(ValueError, match="duplicate rule"):
        TransitionTable((rule, rule))
    with pytest.raises(ValueError, match="no targets"):
        TransitionTable((Rule(Phase.INTAKE, IntakeCompleted, frozenset(), _noop),))


def test_mermaid_names_every_phase_and_omits_self_loops() -> None:
    diagram = render_mermaid(TABLE)
    assert diagram.startswith("stateDiagram-v2\n")
    for phase in Phase:
        assert phase.name in diagram
    assert "BUILDING --> BUILDING" not in diagram


def test_rendering_is_deterministic() -> None:
    assert render_markdown(TABLE) == render_markdown(TABLE)


def test_markdown_lists_every_rule_including_self_loops() -> None:
    page = render_markdown(TABLE)
    for rule in TABLE:
        assert f"| `{rule.source.name}` | `{rule.event.__name__}` |" in page
