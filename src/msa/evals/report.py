"""The eval report (``msa eval report``): what each arm achieved, and what the rule decides.

Metrics (PLAN §11), per arm, over the runs the harness completed (a harness error is
counted, not scored):

- **resolve rate**: delivered and passing every hidden test, over the tasks to resolve;
- **cost per resolved task**: everything spent on tasks to resolve, over those resolved;
- wall clock (median), revisions and negotiation rounds (mean);
- **findings**: blocking findings admitted, and how they ended. The **false-blocker rate**
  here is a proxy: the share of settled blocking findings the system itself judged wrong
  (withdrawn by the Reviewer, or dismissed by a repro test or the Arbiter). PLAN §4's
  degenerate-negotiation flags: the author accepted over 95%, or the Reviewer withdrew
  over 80% of what was contested;
- **cache hit rate**: cache reads over all input tokens.

Two things need a person: whether a trap's plan named the real problem, and whether an
upheld finding was a real defect (reviewer precision). The report lists both for labelling.

The decision applies ``evals/decision.toml``: the arm with a component must gain at least
``min_resolve_gain`` of resolve rate, with a one-sided exact paired test (McNemar's, on the
tasks where the arms disagree) at ``max_p`` or under, at no more than ``max_cost_ratio``
times the cost per resolved task, over at least ``min_runs`` results per arm. Anything less
ships the component disabled: a gain that could be chance has not been shown.
"""

from __future__ import annotations

import math
import statistics
import tomllib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from msa.evals.runner import ARMS, Result

BLOCKING = frozenset({"blocker", "major"})
JUDGED_WRONG = frozenset({"withdrawn", "dismissed"})


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    name: str
    with_arm: str
    without_arm: str
    min_resolve_gain: float
    max_p: float
    max_cost_ratio: float
    min_runs: int


def load_rules(path: Path) -> list[Rule]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return [
        Rule(
            name=name,
            with_arm=spec["with"],
            without_arm=spec["without"],
            min_resolve_gain=spec["min_resolve_gain"],
            max_p=spec["max_p"],
            max_cost_ratio=spec["max_cost_ratio"],
            min_runs=spec["min_runs"],
        )
        for name, spec in raw.items()
    ]


@dataclass
class ArmSummary:
    arm: str
    runs: int = 0
    errors: int = 0
    to_resolve: int = 0
    resolved: int = 0
    delivered: int = 0
    spend: float = 0.0
    spend_on_resolve: float = 0.0
    walls: list[float] = field(default_factory=list)
    revisions: list[int] = field(default_factory=list)
    rounds: list[int] = field(default_factory=list)
    by_kind: Counter[tuple[str, bool]] = field(default_factory=Counter)
    verdicts: Counter[str] = field(default_factory=Counter)  # of blocking findings
    raised: int = 0
    blocking: int = 0
    cache_read: int = 0
    input_tokens: int = 0

    @property
    def resolve_rate(self) -> float | None:
        return self.resolved / self.to_resolve if self.to_resolve else None

    @property
    def cost_per_resolved(self) -> float | None:
        return self.spend_on_resolve / self.resolved if self.resolved else None

    @property
    def settled(self) -> int:
        return sum(n for verdict, n in self.verdicts.items() if verdict != "open")

    @property
    def false_blocker_rate(self) -> float | None:
        wrong = sum(self.verdicts[v] for v in JUDGED_WRONG)
        return wrong / self.settled if self.settled else None

    @property
    def acceptance_rate(self) -> float | None:
        return self.verdicts["accepted"] / self.settled if self.settled else None

    @property
    def withdrawal_rate(self) -> float | None:
        contested = self.settled - self.verdicts["accepted"]
        return self.verdicts["withdrawn"] / contested if contested else None

    @property
    def degenerate(self) -> list[str]:
        flags = []
        if (rate := self.acceptance_rate) is not None and rate > 0.95:
            flags.append("the author accepted over 95% of blocking findings")
        if (rate := self.withdrawal_rate) is not None and rate > 0.80:
            flags.append("the Reviewer withdrew over 80% of contested findings")
        return flags

    @property
    def cache_hit_rate(self) -> float | None:
        total = self.cache_read + self.input_tokens
        return self.cache_read / total if total else None


def summarize(results: Sequence[Result]) -> dict[str, ArmSummary]:
    arms: dict[str, ArmSummary] = {}
    for r in results:
        s = arms.setdefault(r.arm, ArmSummary(r.arm))
        s.runs += 1
        if r.error is not None:
            s.errors += 1
            continue
        s.spend += r.spend_usd
        s.walls.append(r.wall_seconds)
        s.revisions.append(r.revisions)
        s.rounds.append(r.negotiation_rounds)
        s.cache_read += r.cache_read_tokens
        s.input_tokens += r.input_tokens
        s.delivered += r.delivered
        if r.expect == "resolve":
            s.to_resolve += 1
            s.resolved += bool(r.resolved)
            s.spend_on_resolve += r.spend_usd
            s.by_kind[(r.kind, bool(r.resolved))] += 1
        s.raised += len(r.findings)
        for f in r.findings:
            if f.admitted and f.severity in BLOCKING:
                s.blocking += 1
                s.verdicts[f.verdict or "open"] += 1
    return dict(sorted(arms.items()))


def paired(results: Sequence[Result], with_arm: str, without_arm: str) -> tuple[int, int, int]:
    """(only with, only without, pairs): tasks to resolve, matched on (task, repeat)."""
    runs = {
        (r.task, r.repeat, r.arm): r for r in results if r.error is None and r.expect == "resolve"
    }
    only_with = only_without = pairs = 0
    for task, repeat, arm in runs:
        if arm != with_arm or (task, repeat, without_arm) not in runs:
            continue
        pairs += 1
        a, b = runs[(task, repeat, with_arm)].resolved, runs[(task, repeat, without_arm)].resolved
        only_with += bool(a and not b)
        only_without += bool(b and not a)
    return only_with, only_without, pairs


def exact_mcnemar(only_with: int, only_without: int) -> float:
    """One-sided exact p-value of McNemar's test that the arm with the component is better:
    the chance that at least ``only_with`` of the discordant tasks go its way if neither
    arm were better. One-sided because the rule asks only whether the component helps; one
    that hurts ships disabled all the same."""
    n = only_with + only_without
    tail = sum(math.comb(n, i) for i in range(only_with, n + 1))
    return tail / (1 << n)  # 1 << n, not 2 ** n: int ** int is Any to mypy


@dataclass(frozen=True)
class Decision:
    rule: Rule
    verdict: str
    reasons: list[str]
    gain: float | None
    cost_ratio: float | None
    p_value: float
    discordant: tuple[int, int, int]


def decide(rule: Rule, arms: dict[str, ArmSummary], results: Sequence[Result]) -> Decision:
    on, off = arms.get(rule.with_arm), arms.get(rule.without_arm)
    discordant = paired(results, rule.with_arm, rule.without_arm)
    p = exact_mcnemar(discordant[0], discordant[1])
    if on is None or off is None or min(on.to_resolve, off.to_resolve) < rule.min_runs:
        counts = f"{on.to_resolve if on else 0} and {off.to_resolve if off else 0}"
        return Decision(
            rule,
            "not enough evidence",
            [f"{counts} results on tasks to resolve; the rule needs {rule.min_runs} per arm"],
            None,
            None,
            p,
            discordant,
        )
    gain = (on.resolve_rate or 0.0) - (off.resolve_rate or 0.0)
    ratio = (
        on.cost_per_resolved / off.cost_per_resolved
        if on.cost_per_resolved is not None and off.cost_per_resolved
        else None
    )
    reasons = []
    if gain < rule.min_resolve_gain:
        reasons.append(
            f"the resolve rate gained {gain:+.0%}, under the {rule.min_resolve_gain:.0%} margin"
        )
    if p > rule.max_p:
        reasons.append(f"the paired test gives p = {p:.3f}, over {rule.max_p}: it could be chance")
    if ratio is None:
        reasons.append("the cost per resolved task cannot be compared (an arm resolved nothing)")
    elif ratio > rule.max_cost_ratio:
        reasons.append(
            f"it costs {ratio:.2f}x per resolved task, over the {rule.max_cost_ratio:.2f}x cap"
        )
    verdict = "keep it enabled" if not reasons else "ship it disabled"
    if not reasons:
        reasons.append(
            f"the resolve rate gained {gain:+.0%} (p = {p:.3f}) at {ratio:.2f}x the cost per"
            " resolved task"
        )
    return Decision(rule, verdict, reasons, gain, ratio, p, discordant)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.2f}"


def _mean(values: Sequence[float]) -> str:
    return f"{statistics.fmean(values):.1f}" if values else "-"


def render(results: Sequence[Result], rules: Sequence[Rule], rule_commit: str | None = None) -> str:
    arms = summarize(results)
    lines = ["# Eval report", ""]
    total = sum(r.spend_usd for r in results)
    lines += [
        f"{len(results)} run(s) over {len({r.task for r in results})} task(s), ${total:.2f} spent.",
        "",
    ]
    lines += ["## Arms", "", "| | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    rows = [
        ("what it runs", [ARMS.get(a, a) for a in arms]),
        ("runs (harness errors)", [f"{s.runs} ({s.errors})" for s in arms.values()]),
        (
            "**resolve rate**",
            [f"**{_pct(s.resolve_rate)}** ({s.resolved}/{s.to_resolve})" for s in arms.values()],
        ),
        ("delivered", [str(s.delivered) for s in arms.values()]),
        ("cost per resolved task", [_usd(s.cost_per_resolved) for s in arms.values()]),
        ("spent", [_usd(s.spend) for s in arms.values()]),
        (
            "wall clock, median",
            [f"{statistics.median(s.walls):.0f}s" if s.walls else "-" for s in arms.values()],
        ),
        ("revisions, mean", [_mean(s.revisions) for s in arms.values()]),
        ("negotiation rounds, mean", [_mean(s.rounds) for s in arms.values()]),
        ("findings raised", [str(s.raised) for s in arms.values()]),
        ("blocking findings admitted", [str(s.blocking) for s in arms.values()]),
        ("false-blocker rate (proxy)", [_pct(s.false_blocker_rate) for s in arms.values()]),
        ("cache hit rate", [_pct(s.cache_hit_rate) for s in arms.values()]),
    ]
    lines += [f"| {name} | " + " | ".join(values) + " |" for name, values in rows]
    lines.append("")
    for s in arms.values():
        if s.verdicts:
            ended = ", ".join(f"{n} {v}" for v, n in sorted(s.verdicts.items()))
            lines.append(f"- Arm {s.arm}: blocking findings ended {ended}.")
        for flag in s.degenerate:
            lines.append(f"- Arm {s.arm}: **degenerate negotiation**: {flag}.")
    lines += [
        "",
        "## By kind",
        "",
        "| kind | " + " | ".join(arms) + " |",
        "|---|" + "---|" * len(arms),
    ]
    for kind in ("bugfix", "feature", "refactor"):
        cells = []
        for s in arms.values():
            done, failed = s.by_kind[(kind, True)], s.by_kind[(kind, False)]
            cells.append(f"{done}/{done + failed}" if done + failed else "-")
        lines.append(f"| {kind} | " + " | ".join(cells) + " |")
    lines += ["", "## Decision", ""]
    if rule_commit:
        lines += [f"The rule (`evals/decision.toml`) was last changed in `{rule_commit}`.", ""]
    for rule in rules:
        d = decide(rule, arms, results)
        only_with, only_without, pairs = d.discordant
        lines += [
            f"**{rule.name}** (arm {rule.with_arm} against {rule.without_arm}): **{d.verdict}**.",
            "",
            *(f"- {reason}." for reason in d.reasons),
            f"- Of {pairs} paired task(s), {only_with} resolved only with it and "
            f"{only_without} only without it: exact one-sided McNemar p = {d.p_value:.3f}"
            + (" (could be chance)." if d.p_value > rule.max_p else "."),
            "",
        ]
    lines += _per_task(results, list(arms)) + _for_a_person(results)
    return "\n".join(lines).rstrip() + "\n"


def _per_task(results: Sequence[Result], arms: list[str]) -> list[str]:
    cell = {(r.task, r.arm): r for r in results}
    lines = [
        "## Per task",
        "",
        "| task | kind | " + " | ".join(arms) + " |",
        "|---|---|" + "---|" * len(arms),
    ]
    for task in sorted({r.task for r in results}):
        kind = next(r.kind for r in results if r.task == task)
        marks = []
        for arm in arms:
            r = cell.get((task, arm))
            if r is None:
                marks.append("")
            elif r.error is not None:
                marks.append("error")
            elif r.resolved is None:
                marks.append(f"{'delivered' if r.delivered else 'stopped'} {_usd(r.spend_usd)}")
            else:
                marks.append(f"{'✓' if r.resolved else '✗'} {_usd(r.spend_usd)}")
        lines.append(f"| {task} | {kind} | " + " | ".join(marks) + " |")
    return [*lines, ""]


def _for_a_person(results: Sequence[Result]) -> list[str]:
    lines = ["## For a person to judge", ""]
    traps = [r for r in results if r.expect == "flag" and r.error is None]
    lines += ["### Traps: did the plan name the real problem?", ""]
    for r in traps:
        named = "; ".join(r.plan_risks) or "(no risks named)"
        lines.append(
            f"- `{r.task}`, arm {r.arm}, {'delivered' if r.delivered else 'stopped'}: {named}"
        )
    upheld = [
        (r, f)
        for r in results
        for f in r.findings
        if f.admitted and f.severity in BLOCKING and f.verdict in {"accepted", "upheld"}
    ]
    lines += ["", "### Reviewer precision: was each finding the system acted on a real defect?", ""]
    lines += [
        f"- `{r.task}` {r.arm} `{f.id}` [{f.severity}, {f.verdict}]: {f.claim}" for r, f in upheld
    ]
    if not upheld:
        lines.append("- No blocking finding was accepted or upheld.")
    return [*lines, ""]
