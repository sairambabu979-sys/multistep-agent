"""REVIEWING and NEGOTIATING (PLAN §4, ADR-004, ADR-014): the Reviewer, the author, and the
arbitration ladder, with the protocol's rules in ``msa.runtime.negotiation``.

- ``request_review``: the Reviewer reads the change (never the Coder's reasoning) and
  writes findings. The harness admits only findings on lines the change touched (on a
  review after a revision: lines that revision touched, or a reopened agreed fix), gives
  each an id, and raises the admitted ones.
- ``run_negotiation_round``: the Coder, as author and read-only, moves on every open
  finding; the Reviewer replies to every move that contests one. Silence closes nothing.
- ``request_arbitration``: the ladder, cheapest and most objective rung first. A repro
  test settles correctness and security: the Tester writes it in a scratch worktree at the
  head, the harness runs it, and its result is the verdict. A preference settles a
  preference dispute; uncovered style never blocks. What is left goes to the Arbiter role,
  or (escalation = human) stays open for a person.

Every finding, move and rung is kept in the run's files (``findings.jsonl``,
``negotiation.jsonl``, ``arbitration.jsonl``) for the revision that follows, the next
review, and the run report. The state machine sees only ids, severities and verdicts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from msa.agents import arbiter, coder, reviewer, tester
from msa.agents.schemas import (
    AuthorMove,
    FindingKind,
    Plan,
    ReproAttempt,
    ReviewerMove,
    ReviewFinding,
    RulingVerdict,
)
from msa.config import Role
from msa.core.effects import Effect, RequestArbitration, RequestReview, RunNegotiationRound
from msa.core.events import (
    AgentFailed,
    AnyEvent,
    ArbitrationRuled,
    NegotiationRoundCompleted,
    ReviewProduced,
)
from msa.core.ids import Digest, FindingId, TreeHash
from msa.core.values import FindingRef, Resolution, Severity, Verdict
from msa.llm.gateway import InvocationResult
from msa.runtime.artifacts import PLAN_FILE
from msa.runtime.context import Purse, RunContext, reservation
from msa.runtime.guard import checks, find_tests, marks
from msa.runtime.negotiation import (
    LineRanges,
    Outcome,
    admit,
    changed_lines,
    contested,
    first_moves,
    preference_ruling,
    settle_round,
    style_ruling,
)
from msa.runtime.testing import run_pytest

FINDINGS = "findings.jsonl"
REVIEWS = "reviews.jsonl"
ROUNDS = "negotiation.jsonl"
RULINGS = "arbitration.jsonl"


class FindingRecord(BaseModel):
    """A finding as the Reviewer wrote it, with the harness's decision about it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    cycle: int
    tree: str
    finding: ReviewFinding
    admitted: bool
    severity: Severity  # what the run negotiates it as
    reason: str = ""


# ---------- reading the run's record of the review


def findings(ctx: RunContext) -> dict[str, FindingRecord]:
    return {r.id: r for r in (FindingRecord.model_validate(x) for x in ctx.files.records(FINDINGS))}


def history(ctx: RunContext, finding_id: str) -> list[dict[str, object]]:
    return [r for r in ctx.files.records(ROUNDS) if r["finding"] == finding_id]


def rulings(ctx: RunContext, finding_id: str) -> list[dict[str, object]]:
    return [r for r in ctx.files.records(RULINGS) if r["finding"] == finding_id]


def decided(ctx: RunContext) -> dict[str, str]:
    """Each finding's latest verdict, from rounds, rungs and human rulings alike."""
    latest: dict[str, str] = {}
    for name in (ROUNDS, RULINGS):
        for row in ctx.files.records(name):
            if row.get("verdict"):
                latest[str(row["finding"])] = str(row["verdict"])
    return latest


def agreed_fix(ctx: RunContext, record: FindingRecord) -> str:
    """The fix both sides settled on: the counterproposal agreed in a round, else the
    reviewer's suggestion."""
    for row in reversed(history(ctx, record.id)):
        if row.get("verdict") == Verdict.ACCEPTED.value and row.get("agreed_fix"):
            return str(row["agreed_fix"])
    return record.finding.suggested_fix


def repro_test(ctx: RunContext, finding_id: str) -> tuple[str, Digest] | None:
    """The repro test that upheld a finding: its path and stored content."""
    for row in reversed(rulings(ctx, finding_id)):
        if row.get("rung") == "repro" and row.get("verdict") == Verdict.UPHELD.value:
            return str(row["path"]), Digest(str(row["test"]))
    return None


# ---------- rendering for prompts


def render_lines(ranges: LineRanges) -> str:
    if not ranges:
        return "(no lines changed)"
    out = []
    for path, spans in sorted(ranges.items()):
        shown = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)
        out.append(f"{path}: {shown}")
    return "\n".join(out)


def render_finding(record: FindingRecord) -> str:
    f = record.finding
    lines = [
        f"### {record.id}: {record.severity.value} {f.kind.value} at {f.location.render()}",
        f"Claim: {f.claim.strip()}",
        f"Evidence: {f.evidence.strip()}",
    ]
    if f.suggested_fix.strip():
        lines.append(f"Suggested fix: {f.suggested_fix.strip()}")
    if f.preference_ref:
        lines.append(f"Preference: {f.preference_ref}")
    return "\n".join(lines)


def render_history(rows: Sequence[Mapping[str, object]]) -> str:
    out = []
    for row in rows:
        author = f"author {row.get('author') or 'made no move'}"
        if row.get("author_rationale"):
            author += f": {row['author_rationale']}"
        if row.get("author_evidence"):
            author += f" (evidence: {row['author_evidence']})"
        reply = ""
        if row.get("reviewer"):
            reply = f"; reviewer {row['reviewer']}: {row.get('reviewer_rationale', '')}"
            if row.get("amendment"):
                reply += f" (amended: {row['amendment']})"
        out.append(f"Round {row['round']}: {author}{reply}")
    return "\n".join(out)


def _with_history(ctx: RunContext, record: FindingRecord) -> str:
    rows = history(ctx, record.id)
    text = render_finding(record)
    return f"{text}\n{render_history(rows)}" if rows else text


# ---------- the executors


class ReviewExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, RequestReview):
            raise TypeError(f"the reviewer cannot execute {command.kind}")
        ctx = self.context
        tree, cycle = command.tree, command.cycle
        base = await ctx.worktree.base_tree()
        change = changed_lines(await ctx.worktree.changes(base, tree))
        earlier = [r for r in findings(ctx).values() if r.cycle < cycle and r.admitted]
        previous = [r for r in ctx.files.records(REVIEWS) if int(str(r["cycle"])) < cycle]
        revision = None
        if previous:
            last = TreeHash(str(previous[-1]["tree"]))
            revision = changed_lines(await ctx.worktree.changes(last, tree))
        verdicts = decided(ctx)
        reopenable = {r.id for r in earlier if verdicts.get(r.id) in {"accepted", "upheld"}}
        tests = ctx.files.records("tests.jsonl")
        role = Role.REVIEWER
        invocation = reviewer.review(
            task=ctx.task,
            plan=_plan(ctx),
            preferences=await ctx.guidance(role),
            test_report=ctx.files.text(Digest(tests[-1]["report"])) if tests else "(none)",
            changed=render_lines(change),
            earlier=(
                "\n\n".join(
                    f"{render_finding(r)}\nOutcome: {verdicts.get(r.id, 'open')}" for r in earlier
                )
                if previous
                else None
            ),
            tools=ctx.tools(role),
            budget_usd_micros=reservation(effect),
            key=effect.id,
            max_turns=ctx.config.agents.reviewer_max_turns,
        )
        answer = await ctx.invoke(effect, invocation, now=now)
        if not isinstance(answer, InvocationResult):
            return answer
        preferences = {p.id for p in ctx.preferences().preferences if p.enforcement != "off"}
        tester_dirs = ctx.config.capabilities.owned_directories(Role.TESTER)
        refs = []
        for number, finding in enumerate(answer.output.findings, 1):
            admission = admit(
                finding,
                change=change,
                revision=revision,
                reopenable=reopenable,
                preferences=preferences,
                tester_directories=tester_dirs,
            )
            record = FindingRecord(
                id=f"c{cycle}-{number}",
                cycle=cycle,
                tree=tree,
                finding=finding,
                admitted=admission.admitted,
                severity=admission.severity,
                reason=admission.reason,
            )
            ctx.files.record(FINDINGS, record.model_dump(mode="json"))
            if admission.admitted:
                refs.append(FindingRef(id=FindingId(record.id), severity=admission.severity))
        ctx.files.record(
            REVIEWS,
            {
                "effect": effect.id,
                "cycle": cycle,
                "tree": tree,
                "summary": answer.output.summary,
                "raised": len(answer.output.findings),
                "admitted": len(refs),
            },
        )
        return ReviewProduced(
            at=now, effect_id=effect.id, tree=tree, findings=tuple(refs), spend=answer.spend
        )


class NegotiationExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, RunNegotiationRound):
            raise TypeError(f"negotiation cannot execute {command.kind}")
        ctx = self.context
        records = findings(ctx)
        open_ids = list(command.findings)
        purse = Purse(reservation(effect))
        plan = _plan(ctx)

        author = coder.respond(
            task=ctx.task,
            plan=plan,
            preferences=await ctx.guidance(Role.CODER),
            findings="\n\n".join(_with_history(ctx, records[f]) for f in open_ids),
            tools=ctx.tools(Role.CODER),
            budget_usd_micros=purse.left,
            key=f"{effect.id}:author",
            max_turns=ctx.config.agents.author_max_turns,
        )
        moved = await ctx.invoke(effect, author, now=now)
        if not isinstance(moved, InvocationResult):
            return purse.charge(moved)
        purse.pay(moved.spend)
        chosen, _ = first_moves(open_ids, moved.output.moves)

        replies: list[ReviewerMove] = []
        if to_reply := contested(chosen):
            reply = reviewer.reply(
                task=ctx.task,
                plan=plan,
                preferences=await ctx.guidance(Role.REVIEWER),
                contested="\n\n".join(_contest(records[f], chosen[f]) for f in to_reply),
                tools=ctx.tools(Role.REVIEWER),
                budget_usd_micros=purse.left,
                key=f"{effect.id}:reviewer",
                max_turns=ctx.config.agents.reviewer_max_turns,
            )
            answered = await ctx.invoke(effect, reply, now=now)
            if not isinstance(answered, InvocationResult):
                return purse.charge(answered)
            purse.pay(answered.spend)
            replies = answered.output.moves

        result = settle_round(
            open_ids,
            moved.output.moves,
            replies,
            suggested_fixes={FindingId(i): r.finding.suggested_fix for i, r in records.items()},
        )
        for outcome in result.outcomes:
            ctx.files.record(ROUNDS, _row(effect, command.round, outcome.finding_id, outcome))
        resolutions = tuple(
            Resolution(finding_id=fid, verdict=verdict)
            for fid, verdict in result.resolutions.items()
        )
        return NegotiationRoundCompleted(
            at=now, effect_id=effect.id, resolutions=resolutions, spend=purse.spent
        )


class ArbitrationExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, RequestArbitration):
            raise TypeError(f"arbitration cannot execute {command.kind}")
        ctx = self.context
        records = findings(ctx)
        purse = Purse(reservation(effect))
        resolved = ctx.preferences()
        verdicts: dict[FindingId, Verdict] = {}
        left: list[FindingId] = []
        for fid in command.findings:
            record = records[fid]
            kind = record.finding.kind
            if kind.executable:
                outcome = await self._repro(effect, record, command.tree, purse, now)
                if isinstance(outcome, AgentFailed):
                    return purse.charge(outcome)
                if not isinstance(outcome, Verdict):
                    left.append(fid)
                    continue
                verdicts[fid] = outcome
            elif kind is FindingKind.PREFERENCE:
                preference = resolved.get(record.finding.preference_ref)
                verdict, detail = preference_ruling(record.finding, preference)
                self._keep(effect, fid, "preference", verdict, detail)
                verdicts[fid] = verdict
            elif kind is FindingKind.STYLE:
                verdict, detail = style_ruling()
                self._keep(effect, fid, "style", verdict, detail)
                verdicts[fid] = verdict
            else:
                left.append(fid)
        if left and command.arbiter:
            ruled = await self._arbiter(effect, [records[f] for f in left], purse, now)
            if not isinstance(ruled, dict):
                return purse.charge(ruled)
            verdicts |= ruled
        resolutions = tuple(Resolution(finding_id=f, verdict=v) for f, v in verdicts.items())
        return ArbitrationRuled(
            at=now, effect_id=effect.id, resolutions=resolutions, spend=purse.spent
        )

    # ---------- the repro rung

    async def _repro(
        self, effect: Effect, record: FindingRecord, tree: TreeHash, purse: Purse, now: datetime
    ) -> Verdict | AnyEvent | None:
        """The repro test's verdict; None when no test could decide the claim."""
        ctx = self.context
        scratch = ctx.repro_worktree
        key = f"{effect.id}:repro:{record.id}"
        path = (
            f"{ctx.config.testing.repro_directory}/test_{_slug(ctx.run_id)}_{_slug(record.id)}.py"
        )
        await scratch.ensure(await ctx.worktree.base_tree())
        if not ctx.has_paid_turns(effect, key):
            await scratch.reset(tree)
        role = Role.TESTER
        invocation = tester.repro(
            task=ctx.task,
            plan=_plan(ctx),
            preferences=await ctx.guidance(role),
            dispute=_with_history(ctx, record),
            path=path,
            tools=ctx.tools(role, worktree=scratch),
            budget_usd_micros=purse.left,
            key=key,
            max_turns=ctx.config.agents.repro_max_turns,
        )
        answer = await ctx.invoke(effect, invocation, now=now)
        if not isinstance(answer, InvocationResult):
            return answer
        purse.pay(answer.spend)
        verdict, detail, content = await self._run_repro(answer.output, scratch.path, path)
        test = ctx.files.put(content) if content is not None else None
        self._keep(effect, record.id, "repro", verdict, detail, path=path, test=test)
        return verdict

    async def _run_repro(
        self, attempt: ReproAttempt, root: Path, path: str
    ) -> tuple[Verdict | None, str, str | None]:
        ctx = self.context
        try:
            content = (root / path).read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError):
            content = None
        if not attempt.written or content is None:
            return None, f"no repro test: {attempt.explanation.strip()}", None
        live = checks(content)
        if not find_tests(content) or not any(live.values()):
            return None, "the repro test has no test with a live check", content
        if marks(content):
            return None, "the repro test is marked skip or xfail", content
        sandbox = ctx.sandbox(Role.TESTER, worktree=ctx.repro_worktree)
        junit = ctx.files.directory.resolve() / "junit" / "repro" / f"{_slug(path)}.xml"
        run = await run_pytest(sandbox, [path], junit)
        mine = [c for c in run.cases if c.nodeid == path or c.nodeid.startswith(f"{path}::")]
        if any(c.nodeid == path for c in mine):
            return None, "the repro test does not import", content
        if any(c.outcome == "failed" and not c.error for c in mine):
            return Verdict.UPHELD, "the repro test fails on the head tree: the claim holds", content
        if mine and all(c.outcome == "passed" for c in mine):
            return (
                Verdict.DISMISSED,
                "the repro test passes on the head tree: the claim does not hold",
                content,
            )
        return None, "the repro test neither failed an assertion nor passed", content

    # ---------- the last rung

    async def _arbiter(
        self, effect: Effect, records: Sequence[FindingRecord], purse: Purse, now: datetime
    ) -> dict[FindingId, Verdict] | AnyEvent:
        ctx = self.context
        invocation = arbiter.rule(
            task=ctx.task,
            plan=_plan(ctx),
            preferences=await ctx.guidance(Role.ARBITER),
            disputes="\n\n".join(self._dispute(r) for r in records),
            budget_usd_micros=purse.left,
            key=f"{effect.id}:arbiter",
            max_turns=ctx.config.agents.arbiter_max_turns,
        )
        answer = await ctx.invoke(effect, invocation, now=now)
        if not isinstance(answer, InvocationResult):
            return answer
        purse.pay(answer.spend)
        wanted = [FindingId(r.id) for r in records]
        given, _ = first_moves(wanted, answer.output.rulings)
        if missing := [f for f in wanted if f not in given]:
            return AgentFailed(
                at=now,
                effect_id=effect.id,
                error=f"the arbiter did not rule on {', '.join(missing)}",
                spend=purse.spent,
            )
        out: dict[FindingId, Verdict] = {}
        for fid in wanted:
            ruling = given[fid]
            verdict = _RULED[ruling.verdict]
            self._keep(effect, fid, "arbiter", verdict, ruling.rationale)
            out[fid] = verdict
        return out

    def _dispute(self, record: FindingRecord) -> str:
        return dispute(self.context, record.id)

    def _keep(
        self,
        effect: Effect,
        finding_id: str,
        rung: str,
        verdict: Verdict | None,
        detail: str,
        **extra: object,
    ) -> None:
        self.context.files.record(
            RULINGS,
            {
                "effect": effect.id,
                "finding": finding_id,
                "rung": rung,
                "verdict": verdict.value if verdict else None,
                "detail": detail,
                **extra,
            },
        )


_RULED = {
    RulingVerdict.UPHELD: Verdict.UPHELD,
    RulingVerdict.DISMISSED: Verdict.DISMISSED,
    RulingVerdict.DEFERRED: Verdict.DEFERRED,
}


def _plan(ctx: RunContext) -> Plan:
    stored = ctx.files.read(PLAN_FILE)
    if stored is None:
        raise FileNotFoundError(f"{PLAN_FILE} is missing: no plan was implemented in this run")
    return Plan.model_validate_json(stored)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def _contest(record: FindingRecord, move: AuthorMove) -> str:
    lines = [render_finding(record), f"The author's move: {move.action.value}: {move.rationale}"]
    if move.evidence.strip():
        lines.append(f"The author's evidence: {move.evidence.strip()}")
    return "\n".join(lines)


def _row(effect: Effect, round_: int, finding_id: str, outcome: Outcome) -> dict[str, object]:
    author, reply = outcome.author, outcome.reviewer
    return {
        "effect": effect.id,
        "round": round_,
        "finding": finding_id,
        "author": author.action.value if author else None,
        "author_rationale": author.rationale if author else "",
        "author_evidence": author.evidence if author else "",
        "reviewer": reply.action.value if reply else None,
        "reviewer_rationale": reply.rationale if reply else "",
        "amendment": reply.amendment if reply else "",
        "verdict": outcome.verdict.value if outcome.verdict else None,
        "note": outcome.note,
        "agreed_fix": outcome.agreed_fix,
    }


def agreed(ctx: RunContext, ids: Iterable[str]) -> str:
    """The revision request for findings that must change the code: each finding, how it
    was decided, the fix agreed, and the repro test that shows it, if any."""
    records = findings(ctx)
    verdicts = decided(ctx)
    out = []
    for fid in ids:
        record = records[fid]
        text = render_finding(record)
        how = verdicts.get(fid, "upheld")
        text += f"\nDecided: {how}"
        if fix := agreed_fix(ctx, record).strip():
            text += f"\nAgreed fix: {fix}"
        if repro := repro_test(ctx, fid):
            text += f"\nRepro test (make it pass; do not change it): {repro[0]}"
        out.append(text)
    return "\n\n".join(out)


def dispute(ctx: RunContext, finding_id: str) -> str:
    """A disputed finding for whoever rules on it: the finding, every round, and what each
    rung of the ladder found."""
    record = findings(ctx)[finding_id]
    tried = [f"{r['rung']}: {r['detail']}" for r in rulings(ctx, finding_id)]
    text = _with_history(ctx, record)
    return f"{text}\nArbitration so far: {'; '.join(tried)}" if tried else text
