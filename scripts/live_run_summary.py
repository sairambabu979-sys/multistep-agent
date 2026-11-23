"""Summarize one run for a person: its outcome, its spend, and its PR description.

Used by the `live run` workflow (.github/workflows/live-run.yml), and usable on any journal:

    uv run python scripts/live_run_summary.py --db .msa/runs.db --run live-1 [--summary FILE]

It prints the summary, and appends it to ``--summary`` (GitHub's job summary) when given.
The exit status says whether the run reached the human gate honestly: 0 when it is waiting
for a person to approve its release, or was approved and published; 1 otherwise (failed,
cancelled, blocked, or still mid-run). Nothing here approves anything.

With ``--crash`` (the evidence ``scripts/live_run_crash.py`` wrote when it SIGKILLed the
run), it also checks the resumed run against the crash: an effect whose answer the journal
held at the crash must never have been invoked again (ADR-007). One that was fails the run
here too.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from msa.core.ids import RunId
from msa.core.values import Phase
from msa.observability.report import timeline
from msa.runtime.journal import open_journal

REACHED = {Phase.HUMAN_APPROVAL, Phase.COMPLETED}


def records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def crash_section(
    crash: dict[str, Any], events: list[Any], invocations: list[dict[str, Any]], head: int
) -> tuple[list[str], bool]:
    """What the crash interrupted, and whether the resume honoured the journal."""
    seq = int(crash["seq"])
    answered = {e.effect_id for e in events[:seq] if getattr(e, "effect_id", None)}
    after = invocations[int(crash["invocations"]) :]
    again = sorted({r["effect"] for r in after} & answered)
    pending = {p["effect"] for p in crash["pending"]}
    rerun = [r for r in after if r["effect"] in pending]
    listed = ", ".join(f"`{p['effect']}` ({p['kind']})" for p in crash["pending"]) or "none"
    out = [
        "## Crash and resume",
        "",
        f"The run's process tree ({crash['processes']} processes, its sandboxed commands "
        f"included) was SIGKILLed once the journal held seq {seq}, after "
        f"`{crash['event']}`. Nothing was cleaned up. `msa resume` then continued it from "
        f"the journal, to seq {head}.",
        "",
        f"- Pending at the crash: {listed}.",
    ]
    if again:
        named = ", ".join(f"`{e}`" for e in again)
        out.append(
            f"- **Invoked again after the crash, though the journal held their answers: "
            f"{named}.** ADR-007 says this never happens."
        )
    else:
        out.append(
            f"- Effects the journal had answered before the crash ({len(answered)}): none "
            "was invoked again."
        )
    if rerun:
        turns = sum(int(r["turns"]) for r in rerun)
        replayed = sum(int(r["replayed"]) for r in rerun)
        out.append(
            f"- The pending effects ran again: {turns} turn(s), {replayed} of them replayed "
            "from the response store rather than bought again (ADR-008)."
        )
    out += ["", "| effect | role | status | turns | replayed | spent |", "|" + "---|" * 6]
    out += [
        f"| `{r['effect']}` | {r['role']} | {r['status']} | {r['turns']} | {r['replayed']} | "
        f"${int(r['usd_micros']) / 1_000_000:.4f} |"
        for r in after
    ]
    return [*out, ""], not again


def summarize(
    db: Path, run_id: str, run_dir: Path, crash: dict[str, Any] | None = None
) -> tuple[str, bool]:
    with open_journal(db) as journal:
        state = journal.load(RunId(run_id))
        events = journal.events(RunId(run_id))
    lines = timeline(events)
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
    honoured = True
    if crash is not None:
        section, honoured = crash_section(
            crash, events, records(run_dir / "invocations.jsonl"), state.seq
        )
        out += section
        lines.insert(int(crash["seq"]), "---  SIGKILL; `msa resume` continued from here")
    pr = run_dir / "pr.md"
    if pr.exists():
        out += ["## The PR description, for approval", "", pr.read_text(encoding="utf-8"), ""]
    out += ["## Timeline", "", "```", *lines, "```", ""]
    return "\n".join(out), state.phase in REACHED and honoured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--crash", type=Path, help="the evidence live_run_crash.py wrote")
    args = parser.parse_args(argv)
    crash = json.loads(args.crash.read_text(encoding="utf-8")) if args.crash else None
    text, reached = summarize(args.db, args.run, args.db.parent / "runs" / args.run, crash)
    sys.stdout.write(text + "\n")
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write(text + "\n")
    return 0 if reached else 1


if __name__ == "__main__":
    sys.exit(main())
