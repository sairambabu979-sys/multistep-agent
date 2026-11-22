"""The negotiation protocol's rules (PLAN §4.2, ADR-014), as pure functions.

No I/O here: the executors in ``msa.runtime.review`` run the agents and the tests, and
these functions decide what their answers mean. That keeps every rule a property test can
reach.

- **Scope** (``admit``). A finding must point at lines the change added or modified. On a
  review after a revision, a new finding must point at lines *that revision* changed, so
  findings cannot keep arriving on code nobody touched (goalpost drift). The one exception
  is a finding that ``reopens`` an earlier one whose agreed fix is still missing.
- **A round** (``settle_round``). The author moves on every open finding; the reviewer
  replies to every move that contests one. A finding closes only by an accept, or by the
  reviewer's assent to a rebuttal, a counterproposal or a deferral. Anything else, a missing
  move included, leaves it open: silence never closes a finding.
- **The cheap rungs of the ladder** (``preference_ruling``, ``style_ruling``). An
  applicable preference decides a preference dispute; style no preference covers never
  blocks a change.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from msa.agents.schemas import (
    AuthorAction,
    AuthorMove,
    FindingKind,
    Location,
    ReviewerAction,
    ReviewerMove,
    ReviewFinding,
)
from msa.core.ids import FindingId
from msa.core.values import Severity, Verdict
from msa.skills.preferences import Preference

type LineRanges = Mapping[str, Sequence[tuple[int, int]]]

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


# ---------- scope


def changed_lines(diff: str) -> dict[str, list[tuple[int, int]]]:
    """The new-side line ranges of a ``git diff -U0`` patch, per file. A pure deletion
    counts as the line it follows, so a finding can point at where code went missing."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else None  # /dev/null: deleted
            continue
        if path is None or not (match := _HUNK.match(line)):
            continue
        start, count = int(match[1]), int(match[2]) if match[2] is not None else 1
        span = (start, start + count - 1) if count else (max(start, 1), max(start, 1))
        ranges.setdefault(path, []).append(span)
    return ranges


def touches(ranges: LineRanges, location: Location) -> bool:
    return any(
        start <= location.end_line and location.start_line <= end
        for start, end in ranges.get(location.path, ())
    )


@dataclass(frozen=True, slots=True)
class Admission:
    admitted: bool
    severity: Severity  # what the run negotiates it as
    reason: str = ""


def admit(
    finding: ReviewFinding,
    *,
    change: LineRanges,
    revision: LineRanges | None,
    reopenable: Collection[str],
    preferences: Collection[str],
    tester_directories: Sequence[str],
) -> Admission:
    """Whether a finding may be raised, and at what severity.

    ``change`` holds the lines the run changed (base to head); ``revision`` the lines the
    latest revision changed, on a review after one (else None). ``reopenable`` names the
    earlier findings whose fix was agreed or upheld."""
    severity = finding.severity
    where = finding.location.render()
    if not touches(change, finding.location):
        return Admission(False, severity, f"{where} is not on a line this change touched")
    if finding.reopens:
        if finding.reopens not in reopenable:
            return Admission(
                False, severity, f"reopens {finding.reopens}, which no one agreed to fix"
            )
    elif revision is not None and not touches(revision, finding.location):
        return Admission(False, severity, f"{where} is not on a line the latest revision changed")
    if finding.kind is FindingKind.PREFERENCE and finding.preference_ref not in preferences:
        return Admission(
            False, severity, f"names preference {finding.preference_ref!r}, which is not in force"
        )
    path = finding.location.path
    if severity.is_blocking and any(path.startswith(f"{d}/") for d in tester_directories):
        return Admission(
            True,
            Severity.MINOR,
            "reported, not negotiated: the author cannot change the Tester's tests",
        )
    return Admission(True, severity)


# ---------- a round


class _Move(Protocol):
    """Anything that answers one finding: an author move, a reply, a ruling."""

    @property
    def finding_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one round did to one finding: its verdict (None: still open) and why."""

    finding_id: FindingId
    verdict: Verdict | None
    author: AuthorMove | None
    reviewer: ReviewerMove | None
    note: str = ""
    agreed_fix: str = ""  # for a change: the fix both sides agreed on


@dataclass
class Round:
    outcomes: list[Outcome] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)  # moves on findings that were not open

    @property
    def resolutions(self) -> dict[FindingId, Verdict]:
        return {o.finding_id: o.verdict for o in self.outcomes if o.verdict is not None}


def first_moves[M: _Move](
    open_ids: Sequence[FindingId], moves: Iterable[M]
) -> tuple[dict[FindingId, M], list[str]]:
    """One move per open finding (the first one wins), and what was ignored."""
    chosen: dict[FindingId, M] = {}
    ignored: list[str] = []
    wanted = set(open_ids)
    for move in moves:
        fid = FindingId(move.finding_id)
        if fid not in wanted:
            ignored.append(f"{move.finding_id}: not an open finding")
        elif fid in chosen:
            ignored.append(f"{move.finding_id}: a second move (the first one counts)")
        else:
            chosen[fid] = move
    return chosen, ignored


def contested(author: Mapping[FindingId, AuthorMove]) -> list[FindingId]:
    """The findings the reviewer must reply to: every valid move but an accept."""
    return [
        fid
        for fid, move in author.items()
        if move.action is not AuthorAction.ACCEPT and _valid(move)
    ]


def _valid(move: AuthorMove) -> bool:
    """A rebuttal or counterproposal needs its evidence (the counter's is the fix)."""
    needs_evidence = move.action in {AuthorAction.REBUT, AuthorAction.COUNTER}
    return not needs_evidence or bool(move.evidence.strip())


_ASSENT = {
    AuthorAction.REBUT: {ReviewerAction.WITHDRAW: Verdict.WITHDRAWN},
    AuthorAction.COUNTER: {
        ReviewerAction.WITHDRAW: Verdict.WITHDRAWN,
        ReviewerAction.AMEND: Verdict.ACCEPTED,
    },
    AuthorAction.DEFER: {ReviewerAction.WITHDRAW: Verdict.DEFERRED},
}


def settle_round(
    open_ids: Sequence[FindingId],
    author_moves: Iterable[AuthorMove],
    reviewer_moves: Iterable[ReviewerMove],
    *,
    suggested_fixes: Mapping[FindingId, str],
) -> Round:
    """Apply the protocol to one round's moves."""
    author, ignored = first_moves(open_ids, author_moves)
    reviewer, ignored_replies = first_moves(contested(author), reviewer_moves)
    result = Round(ignored=[*ignored, *ignored_replies])
    for fid in open_ids:
        move, reply = author.get(fid), reviewer.get(fid)
        if move is None:
            result.outcomes.append(Outcome(fid, None, None, None, "the author made no move"))
        elif not _valid(move):
            note = f"a {move.action.value} without evidence does not count"
            result.outcomes.append(Outcome(fid, None, move, None, note))
        elif move.action is AuthorAction.ACCEPT:
            fix = suggested_fixes.get(fid, "")
            result.outcomes.append(Outcome(fid, Verdict.ACCEPTED, move, None, agreed_fix=fix))
        elif reply is None:
            result.outcomes.append(Outcome(fid, None, move, None, "the reviewer did not reply"))
        else:
            verdict = _ASSENT[move.action].get(reply.action)
            fix = (reply.amendment.strip() or move.evidence) if verdict is Verdict.ACCEPTED else ""
            result.outcomes.append(Outcome(fid, verdict, move, reply, agreed_fix=fix))
    return result


# ---------- the cheap rungs of the ladder


def preference_ruling(finding: ReviewFinding, preference: Preference | None) -> tuple[Verdict, str]:
    """A preference dispute is settled by the preference, if one applies: a hard one
    upholds the finding, a soft one makes it a follow-up nit. Without one, it is style."""
    if preference is None or preference.enforcement == "off":
        return style_ruling()
    if not preference.scope.applies_to(finding.location.path):
        return Verdict.DISMISSED, f"{preference.id} does not apply to {finding.location.path}"
    if preference.enforcement == "hard":
        return Verdict.UPHELD, f"{preference.id} is a hard preference"
    return (
        Verdict.DEFERRED,
        f"{preference.id} is {preference.enforcement}: a follow-up, not a blocker",
    )


def style_ruling() -> tuple[Verdict, str]:
    return Verdict.DISMISSED, "style that no preference covers never blocks a change"
