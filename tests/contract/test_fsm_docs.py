"""docs/fsm.md is generated from the transition table and must never drift from it."""

from __future__ import annotations

from pathlib import Path

from msa.core.graph import render_markdown
from msa.core.transitions import TABLE

DOC = Path(__file__).resolve().parents[2] / "docs" / "fsm.md"


def test_committed_state_machine_doc_matches_the_table() -> None:
    assert DOC.read_text(encoding="utf-8") == render_markdown(TABLE), (
        "docs/fsm.md is stale. Regenerate it with: uv run msa fsm diagram --output docs/fsm.md"
    )
