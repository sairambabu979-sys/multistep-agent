"""SECURITY.md is M10's security checklist: every control it claims names the test that proves
it. This keeps those names honest, so a renamed or deleted test cannot leave a control
pointing at nothing, and every gap the checklist admits stays labelled as one."""

from __future__ import annotations

import re

from tests.support.scripts import ROOT

CHECKLIST = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
CITED = re.compile(r"`(tests/[\w/]+\.py)::(test_\w+)`")


def test_the_checklist_cites_tests_for_its_controls() -> None:
    assert len(CITED.findall(CHECKLIST)) >= 40


def test_every_test_the_checklist_cites_exists() -> None:
    missing = []
    for path, name in CITED.findall(CHECKLIST):
        source = ROOT / path
        if not source.is_file() or not re.search(
            rf"^(async )?def {name}\(", source.read_text(encoding="utf-8"), re.MULTILINE
        ):
            missing.append(f"{path}::{name}")
    assert missing == []


def test_every_row_without_a_test_is_labelled_a_gap_or_an_accepted_risk() -> None:
    rows = [line for line in CHECKLIST.splitlines() if line.startswith("| ") and "---" not in line]
    untested = [
        row
        for row in rows
        if not CITED.search(row) and "CI:" not in row and "(above)" not in row
        and not row.startswith("| Control |")
    ]  # fmt: skip
    unlabelled = [row for row in untested if not re.search(r"\*\*(Open|Accepted):\*\*", row)]
    assert unlabelled == []
