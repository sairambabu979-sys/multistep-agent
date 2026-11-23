"""State invariants. They are checked after every transition, in production as well as in tests.

Each check returns a human-readable violation or nothing. A non-empty result means there is
a bug in the core. The dispatcher raises ``InvariantViolation`` at the step that caused it.

Numbering follows docs/PLAN.md §2.4. Checks marked "+" are extra structural invariants
that the plan's list implies but does not spell out.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from datetime import timedelta

from msa.core.state import FindingStatus, RunState
from msa.core.values import Phase

P = Phase
# What a phase can assume, by the phase whose work the run is doing (``_working``).
_AFTER_CONTEXT = frozenset(P) - {P.CREATED, P.VALIDATING, P.CONTEXT, P.FAILED, P.CANCELLED}
_AFTER_PLANNING = _AFTER_CONTEXT - {P.PLANNING}
_AFTER_IMPLEMENTING = _AFTER_PLANNING - {P.PLAN_APPROVAL, P.IMPLEMENTING}
# Each gate runs only on a tree that passed every gate before it.
_NEEDS: dict[Phase, str] = {
    P.UNIT_TESTING: "static",
    P.INTEGRATION_TESTING: "unit",
    P.SECURITY_REVIEW: "integration",
    P.CODE_REVIEW: "security",
    P.NEGOTIATION: "security",
}
_RELEASE_PHASES = frozenset({P.FINAL_VERIFICATION, P.HUMAN_APPROVAL, P.COMPLETED})


def _working(s: RunState) -> Phase:
    """The phase whose work a paused or environment-blocked run will resume."""
    return s.resume_phase if s.resume_phase is not None else s.phase


def _ruling(s: RunState) -> bool:
    return any(e.command.kind == "await_human_ruling" for e in s.pending)


def _terminal_shape(s: RunState) -> Iterator[str]:
    """(1, +) A run is terminal exactly when it has an outcome, and then owes no work."""
    if s.is_terminal != (s.outcome is not None):
        yield f"phase {s.phase} inconsistent with outcome {s.outcome}"
    if s.outcome is not None and s.outcome.phase is not s.phase:
        yield f"outcome {s.outcome.reason} belongs to {s.outcome.phase}, run is {s.phase}"
    if s.is_terminal and (s.pending or s.ledger.reservations):
        yield "terminal run still has pending effects or reservations"


def _never_stuck(s: RunState) -> Iterator[str]:
    """(+) A live run always waits on at least one effect, so it can never stall silently."""
    if not s.is_terminal and not s.pending:
        yield f"non-terminal run in {s.phase} has nothing pending: it can never progress"


def _finalization_gate(s: RunState) -> Iterator[str]:
    """(2) Nothing reaches release without every gate passed for the current head tree and
    zero open blocking findings; nothing is approved or published without a verified bundle."""
    phase = _working(s)
    if not s.is_terminal and phase is not P.HUMAN_APPROVAL and s.release is not None:
        yield f"{phase} still holds a release bundle for an earlier tree"
    if not s.is_terminal and phase is not P.HUMAN_APPROVAL and s.approved_by is not None:
        yield f"{phase} holds an approval outside the approval stage"
    if phase not in _RELEASE_PHASES:
        return
    head = s.head_tree
    if not s.evidence.holds_through("security", head):
        yield f"{phase} without every gate passed for head {head}: {s.evidence}"
    if s.open_finding_ids:
        yield f"{phase} with open blocking findings {s.open_finding_ids}"
    if phase is not P.FINAL_VERIFICATION and s.release is None:
        yield f"{phase} without a verified release bundle"
    if s.approved_by is not None and phase is P.FINAL_VERIFICATION:
        yield "an approval recorded before the release was verified"


def _evidence_is_current(s: RunState) -> Iterator[str]:
    """(+) Each gate runs only on a tree every earlier gate passed, and disputes happen
    only about such a tree."""
    phase, head = _working(s), s.head_tree
    gate = _NEEDS.get(P.NEGOTIATION if s.phase is P.BLOCKED and _ruling(s) else phase)
    if gate is not None and not s.evidence.holds_through(gate, head):
        yield f"{phase} on tree {head} without {gate} evidence for it: {s.evidence}"


def _interruptions(s: RunState) -> Iterator[str]:
    """(+) A paused run, or one blocked on the environment, knows what it resumes; no other
    run carries resume state."""
    pending = {e.command.kind for e in s.pending}
    interrupted = s.phase is P.PAUSED or "await_unblock" in pending
    if interrupted != (s.resume_phase is not None):
        yield f"{s.phase} with resume_phase {s.resume_phase} (pending: {sorted(pending)})"
    if interrupted and not s.resume_commands:
        yield f"{s.phase} has nothing to resume"
    if not interrupted and s.resume_commands:
        yield f"{s.phase} carries resume commands it will never issue"
    if s.phase is P.PAUSED and pending != {"await_resume"}:
        yield f"paused but waiting on {sorted(pending)}"
    if s.phase is P.BLOCKED and len(pending) != 1:
        yield f"blocked on {sorted(pending)}: exactly one question at a time"


def _build_progress_matches_pending_work(s: RunState) -> Iterator[str]:
    """(+) While IMPLEMENTING, a region is recorded done exactly when its effect is no
    longer pending, so integration can only start after both regions really finished."""
    if s.phase is not P.IMPLEMENTING:
        return
    pending = {e.command.kind for e in s.pending}
    for done, kind in (
        (s.build.implementation_done, "implement_plan"),
        (s.build.acceptance_tests_done, "author_acceptance_tests"),
    ):
        if done == (kind in pending):
            yield f"build progress for {kind} is {done} while it is {'' if done else 'not '}pending"


def _bounded_work(s: RunState) -> Iterator[str]:
    """(3) Every loop in the machine is bounded by a limit."""
    lim = s.limits
    if s.replans > lim.max_replans:
        yield f"replans {s.replans} > {lim.max_replans}"
    if s.revisions > lim.max_revisions:
        yield f"revisions {s.revisions} > {lim.max_revisions}"
    if s.negotiation_round > lim.max_negotiation_rounds:
        yield f"negotiation round {s.negotiation_round} > {lim.max_negotiation_rounds}"


def _budget_safety(s: RunState) -> Iterator[str]:
    """(5) Live runs never commit more than the budget; reservations match pending effects."""
    ledger = s.ledger
    if not s.is_terminal and ledger.overcommitted:
        yield (
            f"committed {ledger.committed_usd_micros} > limit {ledger.limit_usd_micros} "
            "in a live run"
        )
    spending = {e.id for e in s.pending if e.command.spends}
    reserved = {r.effect_id for r in ledger.reservations}
    if spending != reserved:
        yield f"reservations {sorted(reserved)} do not match spending effects {sorted(spending)}"
    for effect in s.pending:
        reservation = ledger.reservation_for(effect.id)
        cap = reservation.usd_micros if reservation else None
        if effect.budget_usd_micros != cap:
            yield f"effect {effect.id} cap {effect.budget_usd_micros} != reservation {cap}"


def _phase_prerequisites(s: RunState) -> Iterator[str]:
    """(+) Facts a phase depends on are established before it is entered."""
    phase = _working(s)
    if phase in _AFTER_CONTEXT and s.base_tree is None:
        yield f"{phase} without a base tree"
    if phase in _AFTER_PLANNING and s.plan is None:
        yield f"{phase} without a plan"
    if phase in _AFTER_IMPLEMENTING and s.head_tree is None:
        yield f"{phase} without a head tree"
    disputed = phase is P.NEGOTIATION or (s.phase is P.BLOCKED and _ruling(s))
    if disputed and not s.open_finding_ids:
        yield f"{s.phase} with no open findings to dispute"
    if phase is P.REWORK and s.revisions == 0:
        yield "reworking without a counted revision"


def _identities(s: RunState) -> Iterator[str]:
    """(+) Ids are unique; findings are only open while they can still be disputed."""
    for kind, ids in (
        ("effect", [e.id for e in s.pending]),
        ("finding", [f.id for f in s.findings]),
    ):
        dupes = [i for i, n in Counter(ids).items() if n > 1]
        if dupes:
            yield f"duplicate {kind} ids {dupes}"
    for finding in s.findings:
        if finding.status is FindingStatus.OPEN and not finding.severity.is_blocking:
            yield f"non-blocking finding {finding.id} is open"
        if finding.status is FindingStatus.ADVISORY and finding.severity.is_blocking:
            yield f"blocking finding {finding.id} is advisory"


def _clock(s: RunState) -> Iterator[str]:
    """(+) Time never runs backwards. The deadline is fixed when the run starts and moves
    only by time spent waiting on a person, which is counted exactly while one is asked."""
    if s.updated_at < s.started_at:
        yield "updated_at precedes started_at"
    if s.paused < timedelta(0):
        yield f"paused {s.paused} is negative"
    if s.deadline != s.started_at + s.limits.wall_clock + s.paused:
        yield "deadline drifted from started_at + wall_clock + paused"
    asking = sum(e.command.human for e in s.pending)
    if asking > 1:
        yield f"{asking} people asked at once"
    if (s.waiting_since is not None) != (asking == 1):
        yield f"waiting_since is {s.waiting_since} while {asking} human effect(s) are pending"
    if s.waiting_since is not None and s.waiting_since > s.updated_at:
        yield "waiting_since is later than updated_at"


CHECKS: tuple[Callable[[RunState], Iterator[str]], ...] = (
    _terminal_shape,
    _never_stuck,
    _finalization_gate,
    _evidence_is_current,
    _interruptions,
    _build_progress_matches_pending_work,
    _bounded_work,
    _budget_safety,
    _phase_prerequisites,
    _identities,
    _clock,
)


def violations(state: RunState) -> tuple[str, ...]:
    return tuple(message for check in CHECKS for message in check(state))
