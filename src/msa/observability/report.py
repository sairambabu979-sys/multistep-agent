"""The run report (PLAN §9): what happened, what it cost, and what changed.

``timeline`` re-derives every transition from the journal's events by folding them through
the state machine again, so the report shows what the journal proves, not what an executor
claimed. Its lines hold no times, spend, or digests, so a scripted run's timeline is
byte-identical every time; I-01 compares it to a golden file.

Both functions are pure: the caller gathers the run's files and passes them in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from msa.core import RunState, start, transition
from msa.core.events import EventBase, RunStarted
from msa.core.table import Step


def steps(events: Iterable[EventBase]) -> list[Step]:
    iterator = iter(events)
    first = next(iterator, None)
    if not isinstance(first, RunStarted):
        raise ValueError("a journal begins with RunStarted")
    step = start(first)
    found = [step]
    for event in iterator:
        step = transition(step.state, event)
        found.append(step)
    return found


def timeline(events: Iterable[EventBase]) -> list[str]:
    """One line per event: sequence, transition, event, and the effects it issued."""
    lines = []
    for step in steps(events):
        edge = step.edge
        source = edge.source.name if edge.source else "START"
        issued = ", ".join(e.command.kind for e in step.effects) or "-"
        escape = " (escape)" if edge.escape else ""
        lines.append(
            f"{step.state.seq:>3}  {source} -> {edge.target.name}  {edge.event}{escape}  [{issued}]"
        )
    return lines


def _usd(micros: int) -> str:
    return f"${micros / 1_000_000:,.4f}"


def _fence(text: str, language: str = "") -> list[str]:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return [f"{ticks}{language}", text.rstrip("\n"), ticks]


def render(
    state: RunState,
    events: Sequence[EventBase],
    *,
    task: str,
    plan: str | None,
    invocations: Sequence[Mapping[str, Any]],
    verifications: Sequence[Mapping[str, Any]],
    summaries: Sequence[Mapping[str, Any]],
    stand_ins: Mapping[str, str],
    patch: str | None,
) -> str:
    outcome = state.outcome
    verdict = f"{state.phase.name}: {outcome.reason}" if outcome else f"{state.phase.name} (live)"
    ledger = state.ledger
    out = [f"# Run {state.run_id}", "", f"**Outcome:** {verdict}"]
    if outcome and outcome.detail:
        out.append(f"**Detail:** {outcome.detail}")
    out += [
        f"**Spend:** {_usd(ledger.spent.usd_micros)} of {_usd(ledger.limit_usd_micros)} "
        f"({ledger.spent.input_tokens:,} input / {ledger.spent.output_tokens:,} output tokens)",
        f"**Revisions:** {state.revisions} of {state.limits.max_revisions}",
        f"**Base tree:** `{state.base_tree or '-'}` **Head tree:** `{state.head_tree or '-'}`",
        "",
        "## Task",
        "",
        *_fence(task),
    ]

    issued = {e.command.kind for step in steps(events) for e in step.effects}
    used = [f"- `{kind}`: {why}" for kind, why in sorted(stand_ins.items()) if kind in issued]
    if used:
        out += ["", "## Stand-ins", "", "These stages are not built yet and passed through:", ""]
        out += used

    if plan:
        out += ["", plan.rstrip("\n").replace("## Plan", "## Plan", 1)]

    out += ["", "## Timeline", "", *_fence("\n".join(timeline(events)))]

    if invocations:
        out += [
            "",
            "## Agent invocations",
            "",
            "| effect | role | status | turns | replayed | spend | in | out | cache read |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for i in invocations:
            out.append(
                f"| `{i.get('effect')}` | {i.get('role')} | {i.get('status')} | {i.get('turns')} "
                f"| {i.get('replayed')} | {_usd(int(i.get('usd_micros', 0)))} "
                f"| {i.get('input_tokens')} | {i.get('output_tokens')} "
                f"| {i.get('cache_read_tokens')} |"
            )

    if summaries:
        out += ["", "## Coder summaries"]
        for s in summaries:
            out += ["", f"### `{s.get('effect')}` ({s.get('kind')})", "", str(s.get("summary", ""))]
            for label, key in (
                ("Files", "files_changed"),
                ("Checks", "checks_run"),
                ("Open issues", "open_issues"),
            ):
                values = s.get(key) or []
                if values:
                    out.append(f"- {label}: " + "; ".join(str(v) for v in values))

    if verifications:
        out += ["", "## Verifications", ""]
        for v in verifications:
            status = "passed" if v.get("passed") else "failed: " + ", ".join(v.get("failed", []))
            out.append(f"- `{v.get('tree')}`: {status} (report `{v.get('report')}`)")

    if patch is not None:
        out += ["", "## Patch", "", *(_fence(patch, "diff") if patch.strip() else ["(empty)"])]
    return "\n".join(out) + "\n"
