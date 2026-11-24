"""The PR description's review section says what happened to each finding, and only that.

live-6's PR said two advisory findings were "settled in negotiation". Neither had been
negotiated: a minor or nit finding is advisory from the moment it is admitted, and never
enters negotiation. The text was a fallback that assumed one.
"""

from __future__ import annotations

from pathlib import Path

from msa.runtime import release
from msa.runtime.artifacts import RunFiles
from msa.runtime.review import FINDINGS, ROUNDS, RULINGS


def finding(files: RunFiles, fid: str, severity: str, claim: str) -> None:
    files.record(
        FINDINGS, {"id": fid, "admitted": True, "severity": severity, "finding": {"claim": claim}}
    )


def test_each_finding_is_described_by_what_actually_settled_it(tmp_path: Path) -> None:
    files = RunFiles(tmp_path)
    finding(files, "c0-1", "minor", "Plan step 4's tests are missing")
    finding(files, "c0-2", "major", "A bad line leaves the store half updated")
    finding(files, "c0-3", "blocker", "eval() on the quantity")
    finding(files, "c0-4", "major", "Something closed with no record of how")
    files.record(ROUNDS, {"finding": "c0-2", "verdict": "accepted"})
    files.record(RULINGS, {"finding": "c0-3", "verdict": "upheld", "rung": "repro"})

    lines, deferred = release._review_lines(files)
    text = "\n".join(lines)
    assert "4 finding(s) raised, 4 admitted." in text
    assert (
        "`c0-1` [minor] Plan step 4's tests are missing: advisory: it does not block, so it "
        "was not negotiated" in text
    )
    assert (
        "`c0-2` [major] A bad line leaves the store half updated: accepted in negotiation" in text
    )
    assert "`c0-3` [blocker] eval() on the quantity: upheld (repro)" in text
    assert "`c0-4` [major] Something closed with no record of how: closed with no verdict" in text
    assert "settled in negotiation" not in text  # never claimed without a record of it
    assert deferred == []
