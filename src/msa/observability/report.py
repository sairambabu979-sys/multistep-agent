"""The run report (PLAN §9): what happened, what it cost, and what changed.

``timeline`` re-derives every transition from the journal's events by folding them through
the state machine again, so the report shows what the journal proves, not what an executor
claimed. Its lines hold no times, spend, or digests, so a scripted run's timeline is
byte-identical every time; I-01 compares it to a golden file.

Both functions are pure: the caller gathers the run's files and passes them in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
    tests: Sequence[Mapping[str, Any]] = (),
    last_test_report: str | None = None,
    review: Review | None = None,
    gates: Gates | None = None,
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
        f"**Waiting on people:** {state.paused} (not counted against the wall clock)",
    ]
    if state.approved_by:
        out.append(f"**Release approved by:** {state.approved_by}")
    out += [
        "",
        "## Task",
        "",
        *_fence(task),
    ]

    issued = {e.command.kind for step in steps(events) for e in step.effects}
    used = [f"- `{kind}`: {why}" for kind, why in sorted(stand_ins.items()) if kind in issued]
    if used:
        out += ["", "## Stand-ins", "", "These stages passed through, doing nothing:", ""]
        out += used

    if plan:
        out += ["", plan.rstrip("\n")]

    out += ["", "## Timeline", "", *_fence("\n".join(timeline(events)))]

    out += _invocations(invocations)
    out += _summaries(summaries)
    out += _checks(verifications, tests, last_test_report)
    if gates is not None:
        out += _gates(gates)
    if review is not None:
        out += _review(review)
    if patch is not None:
        out += ["", "## Patch", "", *(_fence(patch, "diff") if patch.strip() else ["(empty)"])]
    return "\n".join(out) + "\n"


def _invocations(invocations: Sequence[Mapping[str, Any]]) -> list[str]:
    if not invocations:
        return []
    out = [
        "",
        "## Agent invocations",
        "",
        "| effect | role | effort | status | turns | replayed | spend | in | out | cache read |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    out.extend(
        f"| `{i.get('effect')}` | {i.get('role')} | {i.get('effort', '-')} | {i.get('status')} "
        f"| {i.get('turns')} "
        f"| {i.get('replayed')} | {_usd(int(i.get('usd_micros', 0)))} "
        f"| {i.get('input_tokens')} | {i.get('output_tokens')} "
        f"| {i.get('cache_read_tokens')} |"
        for i in invocations
    )
    return out


SUMMARY_LISTS = (
    ("Files", "files_changed"),
    ("Tests", "tests"),
    ("Criteria covered", "criteria_covered"),
    ("Checks", "checks_run"),
    ("Open issues", "open_issues"),
)


def _summaries(summaries: Sequence[Mapping[str, Any]]) -> list[str]:
    if not summaries:
        return []
    out = ["", "## Agent summaries"]
    for s in summaries:
        out += ["", f"### `{s.get('effect')}` ({s.get('kind')})", "", str(s.get("summary", ""))]
        out += [
            f"- {label}: " + "; ".join(str(v) for v in values)
            for label, key in SUMMARY_LISTS
            if (values := s.get(key) or [])
        ]
    return out


def _checks(
    verifications: Sequence[Mapping[str, Any]],
    tests: Sequence[Mapping[str, Any]],
    last_test_report: str | None,
) -> list[str]:
    out: list[str] = []
    if verifications:
        out += ["", "## Static analysis", ""]
        for v in verifications:
            status = "passed" if v.get("passed") else "failed: " + ", ".join(v.get("failed", []))
            out.append(f"- `{v.get('tree')}`: {status} (report `{v.get('report')}`)")
    if tests:
        out += ["", "## Unit tests", ""]
        for t in tests:
            status = "passed" if t.get("verdict") == "passed" else f"failed: {t.get('kind')}"
            out.append(f"- `{t.get('tree')}`: {status} (report `{t.get('report')}`)")
    if last_test_report:
        # The latest run's matrix (fail before / pass after) and problems, one level down.
        body = last_test_report.rstrip("\n").replace("\n## ", "\n#### ").replace("# ", "### ", 1)
        out += ["", body]
    return out


@dataclass(frozen=True, slots=True)
class Gates:
    """The later gates' records: integration, security, release (ADR-015)."""

    integrations: Sequence[Mapping[str, Any]] = ()
    security: Sequence[Mapping[str, Any]] = ()
    releases: Sequence[Mapping[str, Any]] = ()


def _integration(record: Mapping[str, Any]) -> str:
    if record.get("verdict") == "passed":
        if not record.get("ran", True):
            return "nothing to run: no integration suite"
        return "passed, " + ("without network" if record.get("isolated") else "with network")
    return f"failed: {record.get('kind')}"


def _gates(gates: Gates) -> list[str]:
    out: list[str] = []
    if gates.integrations:
        out += ["", "## Integration tests", ""]
        out += [
            f"- `{r.get('tree')}`: {_integration(r)} (report `{r.get('report')}`)"
            for r in gates.integrations
        ]
    if gates.security:
        out += ["", "## Security review", ""]
        for r in gates.security:
            status = (
                f"failed: {r.get('blocking')} blocking (to the {r.get('owner')})"
                if r.get("verdict") == "failed"
                else "passed"
            )
            extra = f", {r.get('advisory')} advisory" if r.get("advisory") else ""
            deps = r.get("manifests") or []
            listed = f"; manifests changed: {', '.join(deps)}" if deps else ""
            out.append(f"- `{r.get('tree')}`: {status}{extra}{listed} (report `{r.get('report')}`)")
    if gates.releases:
        out += ["", "## Release", ""]
        for r in gates.releases:
            match r.get("verdict"):
                case "passed":
                    line = f"verified; bundle `{r.get('bundle')}` (pr.md)"
                case "published":
                    line = (
                        f"published to branch `{r.get('branch')}` at `{r.get('commit')}`, "
                        f"approved by {r.get('approved_by')}. Nothing was pushed."
                    )
                case _:
                    line = f"verification failed (report `{r.get('report')}`)"
            out.append(f"- `{r.get('tree')}`: {line}")
    return out


# ---------- review and negotiation (PLAN §4)

# PLAN §4.2: a negotiation is flagged as degenerate when the author accepts more than 95%
# of the findings, or the reviewer withdraws more than 80% of what was contested. A run
# with few moves says little either way: it is flagged only from this many moves on, and
# the M9 eval applies the thresholds across runs.
ACCEPT_THRESHOLD, WITHDRAW_THRESHOLD, FLAG_MIN_MOVES = 0.95, 0.80, 5


@dataclass(frozen=True, slots=True)
class Review:
    """What the review left in the run's files, as plain records."""

    reviews: Sequence[Mapping[str, Any]]
    findings: Sequence[Mapping[str, Any]]
    rounds: Sequence[Mapping[str, Any]]
    rulings: Sequence[Mapping[str, Any]]


def _review(review: Review) -> list[str]:
    if not review.reviews:
        return []
    out = ["", "## Review", ""]
    out.extend(
        f"- cycle {r.get('cycle')} on `{r.get('tree')}`: {r.get('raised')} finding(s) "
        f"raised, {r.get('admitted')} admitted. {r.get('summary', '')}"
        for r in review.reviews
    )
    latest: dict[str, tuple[str, str]] = {}
    for row in [*review.rounds, *review.rulings]:
        if row.get("verdict"):
            how = row.get("rung") or f"round {row.get('round')}"
            latest[str(row["finding"])] = (str(row["verdict"]), str(how))
    if review.findings:
        out += ["", "| finding | severity | kind | where | claim | outcome | decided by |"]
        out.append("|---|---|---|---|---|---|---|")
        for f in review.findings:
            fid, body = str(f["id"]), f["finding"]
            where = body["location"]
            place = f"{where['path']}:{where['start_line']}-{where['end_line']}"
            if not f.get("admitted"):
                outcome, how = "not admitted", str(f.get("reason", ""))
            elif f.get("severity") in {"minor", "nit"}:
                outcome, how = "reported", str(f.get("reason") or "not negotiated")
            else:
                outcome, how = latest.get(fid, ("open", "-"))
            claim = str(body["claim"]).replace("|", "\\|").replace("\n", " ")
            out.append(
                f"| `{fid}` | {f.get('severity')} | {body['kind']} | `{place}` | {claim} "
                f"| {outcome} | {how} |"
            )
    deferred = [fid for fid, (verdict, _) in latest.items() if verdict == "deferred"]
    if deferred:
        out += ["", "**Follow-ups** (deferred): " + ", ".join(f"`{d}`" for d in deferred)]
    out += _health(review.rounds)
    return out


def _health(rounds: Sequence[Mapping[str, Any]]) -> list[str]:
    moves = [r for r in rounds if r.get("author")]
    replies = [r for r in rounds if r.get("reviewer")]
    if not moves:
        return []
    accepted = sum(r["author"] == "accept" for r in moves) / len(moves)
    withdrawn = sum(r["reviewer"] == "withdraw" for r in replies) / len(replies) if replies else 0.0
    out = [
        "",
        f"**Negotiation:** the author accepted {accepted:.0%} of {len(moves)} move(s); the "
        f"reviewer withdrew {withdrawn:.0%} of {len(replies)} contested.",
    ]
    if len(moves) >= FLAG_MIN_MOVES and accepted > ACCEPT_THRESHOLD:
        out.append("**Flag:** degenerate negotiation: the author accepts nearly everything.")
    if len(replies) >= FLAG_MIN_MOVES and withdrawn > WITHDRAW_THRESHOLD:
        out.append("**Flag:** degenerate negotiation: the reviewer withdraws nearly everything.")
    return out
