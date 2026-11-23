"""The negotiation protocol's rules (PLAN §4.2, ADR-014): scope, rounds, the cheap rungs.

The properties are the protocol's guarantees: silence never closes a finding, only an
accept or the reviewer's assent does, moves on other findings change nothing, and no
finding outside the change (or, after a revision, outside the revision) is admitted.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
from msa.runtime.negotiation import (
    admit,
    changed_lines,
    contested,
    first_moves,
    preference_ruling,
    settle_round,
    style_ruling,
    touches,
)
from msa.skills.preferences import Preference, Scope

TESTER = ("tests/acceptance", "tests/repro")


def a_finding(**overrides: Any) -> ReviewFinding:
    fields: dict[str, Any] = {
        "severity": Severity.BLOCKER,
        "kind": FindingKind.CORRECTNESS,
        "location": Location(path="src/a.py", start_line=3, end_line=4),
        "claim": "wrong",
        "evidence": "line 3",
        "suggested_fix": "fix it",
        "preference_ref": "",
        "reopens": "",
        "confidence": 0.5,
    }
    return ReviewFinding(**{**fields, **overrides})


def author(fid: str, action: AuthorAction, evidence: str = "because") -> AuthorMove:
    return AuthorMove(finding_id=fid, action=action, rationale="why", evidence=evidence)


def reply(fid: str, action: ReviewerAction, amendment: str = "") -> ReviewerMove:
    return ReviewerMove(finding_id=fid, action=action, rationale="why", amendment=amendment)


# ---------- scope


DIFF = """\
diff --git a/src/a.py b/src/a.py
index 1..2 100644
--- a/src/a.py
+++ b/src/a.py
@@ -2,0 +3,2 @@ def f():
+    x = 1
+    y = 2
@@ -10 +12 @@ def g():
-    old
+    new
@@ -20,2 +21,0 @@
-    gone
-    gone
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+a
+b
+c
diff --git a/dead.py b/dead.py
deleted file mode 100644
--- a/dead.py
+++ /dev/null
@@ -1 +0,0 @@
-x
"""


def test_changed_lines_are_the_new_side_of_each_hunk() -> None:
    assert changed_lines(DIFF) == {
        "src/a.py": [(3, 4), (12, 12), (21, 21)],  # a deletion: the line it follows
        "new.py": [(1, 3)],
    }
    assert changed_lines("") == {}
    assert changed_lines("+++ b/x.py\n@@ -0,1 +0,0 @@\n-x\n") == {"x.py": [(1, 1)]}


@pytest.mark.parametrize(
    ("lines", "touched"),
    [
        ((3, 3), True),
        ((1, 3), True),
        ((4, 9), True),
        ((5, 11), False),
        ((12, 12), True),
        ((13, 20), False),
    ],
)
def test_a_location_touches_a_change_when_the_ranges_overlap(
    lines: tuple[int, int], touched: bool
) -> None:
    ranges = changed_lines(DIFF)
    assert (
        touches(ranges, Location(path="src/a.py", start_line=lines[0], end_line=lines[1]))
        is touched
    )
    assert not touches(ranges, Location(path="other.py", start_line=3, end_line=3))


def test_a_location_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValueError, match="end_line 2 precedes start_line 3"):
        Location(path="a.py", start_line=3, end_line=2)
    assert Location(path="a.py", start_line=3, end_line=3).render() == "a.py:3"
    assert Location(path="a.py", start_line=3, end_line=5).render() == "a.py:3-5"


CHANGE = {"src/a.py": [(3, 9)], "tests/acceptance/test_a.py": [(1, 9)]}


def _admit(finding: ReviewFinding, **overrides: Any) -> tuple[bool, Severity, str]:
    arguments: dict[str, Any] = {
        "change": CHANGE,
        "revision": None,
        "reopenable": (),
        "preferences": ("py.hard",),
        "tester_directories": TESTER,
    }
    found = admit(finding, **{**arguments, **overrides})
    return found.admitted, found.severity, found.reason


def test_a_finding_on_the_change_is_admitted_at_its_severity() -> None:
    assert _admit(a_finding()) == (True, Severity.BLOCKER, "")


def test_a_finding_off_the_change_is_not_admitted() -> None:
    off = a_finding(location=Location(path="src/a.py", start_line=20, end_line=21))
    assert _admit(off) == (
        False,
        Severity.BLOCKER,
        "src/a.py:20-21 is not on a line this change touched",
    )


def test_after_a_revision_new_findings_must_be_on_its_lines_unless_they_reopen_one() -> None:
    revision = {"src/a.py": [(8, 9)]}
    stale = a_finding()  # lines 3-4: in the change, not in the revision
    assert _admit(stale, revision=revision) == (
        False,
        Severity.BLOCKER,
        "src/a.py:3-4 is not on a line the latest revision changed",
    )
    assert _admit(a_finding(reopens="c0-1"), revision=revision, reopenable={"c0-1"})[0]
    assert _admit(a_finding(reopens="c0-9"), revision=revision, reopenable={"c0-1"}) == (
        False,
        Severity.BLOCKER,
        "reopens c0-9, which no one agreed to fix",
    )
    fresh = a_finding(location=Location(path="src/a.py", start_line=8, end_line=8))
    assert _admit(fresh, revision=revision)[0]


def test_a_preference_finding_must_name_a_preference_in_force() -> None:
    named = a_finding(kind=FindingKind.PREFERENCE, preference_ref="py.hard")
    assert _admit(named)[0]
    unknown = a_finding(kind=FindingKind.PREFERENCE, preference_ref="made.up")
    assert _admit(unknown) == (
        False,
        Severity.BLOCKER,
        "names preference 'made.up', which is not in force",
    )


def test_blocking_findings_on_the_testers_tests_are_reported_not_negotiated() -> None:
    there = Location(path="tests/acceptance/test_a.py", start_line=2, end_line=2)
    assert _admit(a_finding(location=there)) == (
        True,
        Severity.MINOR,
        "reported, not negotiated: the author cannot change the Tester's tests",
    )
    assert _admit(a_finding(location=there, severity=Severity.NIT)) == (True, Severity.NIT, "")


locations = st.builds(
    lambda path, start, length: Location(path=path, start_line=start, end_line=start + length),
    st.sampled_from(["src/a.py", "src/b.py", "tests/acceptance/test_a.py"]),
    st.integers(1, 30),
    st.integers(0, 5),
)
ranges = st.dictionaries(
    st.sampled_from(["src/a.py", "src/b.py", "tests/acceptance/test_a.py"]),
    st.lists(
        st.tuples(st.integers(1, 30), st.integers(0, 5)).map(lambda t: (t[0], t[0] + t[1])),
        max_size=3,
    ),
)


@given(
    location=locations,
    change=ranges,
    revision=st.none() | ranges,
    severity=st.sampled_from(Severity),
    reopens=st.sampled_from(["", "c0-1", "c0-2"]),
)
def test_scope_is_never_widened(
    location: Location,
    change: dict[str, list[tuple[int, int]]],
    revision: dict[str, list[tuple[int, int]]] | None,
    severity: Severity,
    reopens: str,
) -> None:
    """Admitted findings are on the change; after a revision, on it or reopening an agreed
    fix; and admission never raises a finding's severity."""
    finding = a_finding(location=location, severity=severity, reopens=reopens)
    found = admit(
        finding,
        change=change,
        revision=revision,
        reopenable={"c0-1"},
        preferences=(),
        tester_directories=TESTER,
    )
    if found.admitted:
        assert touches(change, location)
        if revision is not None:
            assert touches(revision, location) or reopens == "c0-1"
        rank = list(Severity)
        assert rank.index(found.severity) >= rank.index(severity)
        assert not (found.severity.is_blocking and location.path.startswith("tests/acceptance/"))


# ---------- a round


IDS = [FindingId("a"), FindingId("b"), FindingId("c")]
FIXES = {FindingId("a"): "fix a", FindingId("b"): "fix b", FindingId("c"): "fix c"}


def _verdicts(*args: Any) -> dict[str, Verdict | None]:
    result = settle_round(IDS, *args, suggested_fixes=FIXES)
    return {o.finding_id: o.verdict for o in result.outcomes}


def test_an_accept_closes_a_finding_with_the_suggested_fix() -> None:
    result = settle_round(IDS, [author("a", AuthorAction.ACCEPT, "")], [], suggested_fixes=FIXES)
    first = result.outcomes[0]
    assert (first.verdict, first.agreed_fix) == (Verdict.ACCEPTED, "fix a")
    assert [o.note for o in result.outcomes[1:]] == ["the author made no move"] * 2


@pytest.mark.parametrize(
    ("action", "response", "verdict"),
    [
        (AuthorAction.REBUT, ReviewerAction.WITHDRAW, Verdict.WITHDRAWN),
        (AuthorAction.REBUT, ReviewerAction.MAINTAIN, None),
        (AuthorAction.REBUT, ReviewerAction.AMEND, None),
        (AuthorAction.COUNTER, ReviewerAction.AMEND, Verdict.ACCEPTED),
        (AuthorAction.COUNTER, ReviewerAction.WITHDRAW, Verdict.WITHDRAWN),
        (AuthorAction.COUNTER, ReviewerAction.MAINTAIN, None),
        (AuthorAction.DEFER, ReviewerAction.WITHDRAW, Verdict.DEFERRED),
        (AuthorAction.DEFER, ReviewerAction.MAINTAIN, None),
        (AuthorAction.DEFER, ReviewerAction.AMEND, None),
    ],
)
def test_a_contested_finding_closes_only_with_the_reviewers_assent(
    action: AuthorAction, response: ReviewerAction, verdict: Verdict | None
) -> None:
    assert _verdicts([author("a", action)], [reply("a", response)])["a"] is verdict


def test_an_agreed_counter_carries_the_agreed_fix() -> None:
    moves = [
        author("a", AuthorAction.COUNTER, "use a set"),
        author("b", AuthorAction.COUNTER, "cache it"),
    ]
    replies = [
        reply("a", ReviewerAction.AMEND, "use a frozenset"),
        reply("b", ReviewerAction.AMEND),
    ]
    result = settle_round(IDS, moves, replies, suggested_fixes=FIXES)
    assert [o.agreed_fix for o in result.outcomes[:2]] == ["use a frozenset", "cache it"]


def test_a_rebuttal_or_counter_without_evidence_does_not_count() -> None:
    moves = [author("a", AuthorAction.REBUT, " "), author("b", AuthorAction.COUNTER, "")]
    assert contested(dict(first_moves(IDS, moves)[0])) == []
    result = settle_round(IDS, moves, [reply("a", ReviewerAction.WITHDRAW)], suggested_fixes=FIXES)
    assert [o.verdict for o in result.outcomes] == [None, None, None]
    assert result.outcomes[0].note == "a rebut without evidence does not count"
    assert result.outcomes[1].note == "a counter without evidence does not count"
    assert result.ignored == ["a: not an open finding"]  # the reply had nothing to answer


def test_a_contested_finding_the_reviewer_ignores_stays_open() -> None:
    result = settle_round(IDS, [author("a", AuthorAction.DEFER, "")], [], suggested_fixes=FIXES)
    assert (result.outcomes[0].verdict, result.outcomes[0].note) == (
        None,
        "the reviewer did not reply",
    )


def test_moves_on_other_findings_and_second_moves_are_ignored() -> None:
    moves = [
        author("z", AuthorAction.ACCEPT),
        author("a", AuthorAction.ACCEPT),
        author("a", AuthorAction.REBUT),
    ]
    result = settle_round(IDS, moves, [], suggested_fixes=FIXES)
    assert result.resolutions == {"a": Verdict.ACCEPTED}
    assert result.ignored == ["z: not an open finding", "a: a second move (the first one counts)"]


# Few ids and mostly-valid moves, so moves and replies on the same finding are the common
# case: those are the collisions the protocol's rules are about.
author_moves = st.lists(
    st.builds(
        author,
        st.sampled_from(["a", "b"]),
        st.sampled_from(AuthorAction),
        st.sampled_from(["evidence", "evidence", ""]),
    ),
    min_size=1,
    max_size=4,
)
reviewer_moves = st.lists(
    st.builds(
        reply,
        st.sampled_from(["a", "b"]),
        st.sampled_from(ReviewerAction),
        st.sampled_from(["amended", ""]),
    ),
    max_size=4,
)


@given(author_moves, reviewer_moves)
def test_silence_never_closes_a_finding_and_only_assent_closes_a_contested_one(
    moves: list[AuthorMove], replies: list[ReviewerMove]
) -> None:
    result = settle_round(IDS, moves, replies, suggested_fixes=FIXES)
    assert [o.finding_id for o in result.outcomes] == IDS  # one outcome per open finding
    for outcome in result.outcomes:
        if outcome.verdict is None:
            continue
        assert outcome.author is not None  # no move, no verdict
        if outcome.author.action is AuthorAction.ACCEPT:
            assert outcome.verdict is Verdict.ACCEPTED
        else:
            assert outcome.reviewer is not None  # closed only by the reviewer's assent
            assert outcome.reviewer.action is not ReviewerAction.MAINTAIN
            assert outcome.author.evidence.strip() or outcome.author.action is AuthorAction.DEFER
        if outcome.verdict.requires_change:
            assert outcome.agreed_fix  # a change always comes with the fix to make


@given(author_moves, reviewer_moves, author_moves, reviewer_moves)
def test_moves_on_findings_that_are_not_open_change_nothing(
    moves: list[AuthorMove],
    replies: list[ReviewerMove],
    noise: list[AuthorMove],
    noisy_replies: list[ReviewerMove],
) -> None:
    noise = [m.model_copy(update={"finding_id": "zz"}) for m in noise]
    noisy_replies = [m.model_copy(update={"finding_id": "zz"}) for m in noisy_replies]

    before = settle_round(IDS, moves, replies, suggested_fixes=FIXES).resolutions
    after = settle_round(
        IDS,
        [*moves, *noise],
        [*replies, *noisy_replies],
        suggested_fixes=FIXES,
    ).resolutions
    assert before == after


# ---------- the cheap rungs


def pref(enforcement: str, language: str = "python") -> Preference:
    return Preference.model_validate(
        {
            "id": "py.rule",
            "rule": "a rule",
            "enforcement": enforcement,
            "scope": Scope.model_validate({"language": language}),
        }
    )


@pytest.mark.parametrize(
    ("preference", "verdict", "detail"),
    [
        (pref("hard"), Verdict.UPHELD, "py.rule is a hard preference"),
        (pref("soft"), Verdict.DEFERRED, "py.rule is soft: a follow-up, not a blocker"),
        (pref("info"), Verdict.DEFERRED, "py.rule is info: a follow-up, not a blocker"),
        (pref("hard", "markdown"), Verdict.DISMISSED, "py.rule does not apply to src/a.py"),
        (pref("off"), Verdict.DISMISSED, "style that no preference covers never blocks a change"),
        (None, Verdict.DISMISSED, "style that no preference covers never blocks a change"),
    ],
)
def test_a_preference_dispute_is_settled_by_the_preference(
    preference: Preference | None, verdict: Verdict, detail: str
) -> None:
    assert preference_ruling(a_finding(kind=FindingKind.PREFERENCE), preference) == (
        verdict,
        detail,
    )
    assert style_ruling()[0] is Verdict.DISMISSED
