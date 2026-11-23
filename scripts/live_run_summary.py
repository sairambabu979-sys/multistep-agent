"""Summarize one run for a person: its outcome, its spend, and its PR description.

Used by the `live run` workflow (.github/workflows/live-run.yml), and usable on any journal:

    uv run python scripts/live_run_summary.py --db .msa/runs.db --run live-1 [--summary FILE]

It prints the summary, and appends it to ``--summary`` (GitHub's job summary) when given.
The exit status says whether the run reached the human gate honestly: 0 when it is waiting
for a person to approve its release, or was approved and published; 1 otherwise (failed,
cancelled, blocked, or still mid-run). Nothing here approves anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from msa.core.ids import RunId
from msa.core.values import Phase
from msa.observability.report import timeline
from msa.runtime.journal import open_journal

REACHED = {Phase.HUMAN_APPROVAL, Phase.COMPLETED}


def summarize(db: Path, run_id: str, run_dir: Path) -> tuple[str, bool]:
    with open_journal(db) as journal:
        state = journal.load(RunId(run_id))
        lines = timeline(journal.events(RunId(run_id)))
    spent = state.ledger.spent
    outcome = f"{state.outcome.reason.value}: {state.outcome.detail}" if state.outcome else "-"
    out = [
        f"# Live run `{run_id}`",
        "",
        "| | |",
        "|---|---|",
        f"| phase | **{state.phase.value}** |",
        f"| outcome | {outcome} |",
        f"| spent | ${spent.usd_micros / 1_000_000:.4f} of "
        f"${state.ledger.limit_usd_micros / 1_000_000:.2f} |",
        f"| tokens | {spent.input_tokens} in / {spent.output_tokens} out |",
        f"| revisions | {state.revisions} |",
        f"| events | {state.seq} |",
        "",
    ]
    if state.phase is Phase.HUMAN_APPROVAL:
        out += [
            "The run is waiting for a person to approve its release. Nothing was published, "
            "and no workflow approves it.",
            "",
        ]
    pr = run_dir / "pr.md"
    if pr.exists():
        out += ["## The PR description, for approval", "", pr.read_text(encoding="utf-8"), ""]
    out += ["## Timeline", "", "```", *lines, "```", ""]
    return "\n".join(out), state.phase in REACHED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    text, reached = summarize(args.db, args.run, args.db.parent / "runs" / args.run)
    sys.stdout.write(text + "\n")
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write(text + "\n")
    return 0 if reached else 1


if __name__ == "__main__":
    sys.exit(main())
