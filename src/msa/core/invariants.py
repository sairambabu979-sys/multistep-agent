"""State invariants. They are checked after every transition, in production as well as in tests.

Each check returns a human-readable violation or nothing. A non-empty result means there is
a bug in the core. The dispatcher raises ``InvariantViolation`` at the step that caused it.

Numbering follows docs/PLAN.md §2.4. Checks marked "+" are extra structural invariants
that the plan's list implies but does not spell out.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator

from msa.core.state import FindingStatus, RunState
from msa.core.values import Phase

_AFTER_INTAKE = frozenset(Phase) - {Phase.INTAKE, Phase.FAILED, Phase.ABORTED}
_AFTER_PLANNING = _AFTER_INTAKE - {Phase.PLANNING}
_AFTER_BUILDING = _AFTER_PLANNING - {Phase.PLAN_GATE, Phase.BUILDING}
_DISPUTE_PHASES = frozenset({Phase.NEGOTIATING, Phase.AWAITING_HUMAN})


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
    """(2) Nothing is finalized without current evidence and zero open blocking findings."""
    if s.phase not in {Phase.FINALIZING, Phase.DONE}:
        return
    head = s.head_tree
    if head is None or s.evidence.verified != head or s.evidence.tested != head:
        yield f"{s.phase} without verify+test evidence for head {head}: {s.evidence}"
    if s.open_finding_ids:
        yield f"{s.phase} with open blocking findings {s.open_finding_ids}"


def _evidence_is_current(s: RunState) -> Iterator[str]:
    """(+) Each phase sees only evidence for the current head tree."""
    head = s.head_tree
    if s.phase is Phase.TESTING and s.evidence.verified != head:
        yield "testing a tree that was not verified"
    if s.phase in {Phase.REVIEWING} | _DISPUTE_PHASES and (
        s.evidence.verified != head or s.evidence.tested != head
    ):
        yield f"{s.phase} on a tree without current verify+test evidence"


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
    if s.phase in _AFTER_INTAKE and s.base_tree is None:
        yield f"{s.phase} without a base tree"
    if s.phase in _AFTER_PLANNING and s.plan is None:
        yield f"{s.phase} without a plan"
    if s.phase in _AFTER_BUILDING and s.head_tree is None:
        yield f"{s.phase} without a head tree"
    if s.phase in _DISPUTE_PHASES and not s.open_finding_ids:
        yield f"{s.phase} with no open findings to dispute"
    if s.phase is Phase.REVISING and s.revisions == 0:
        yield "revising without a counted revision"


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
    """(+) Time never runs backwards, and the deadline is fixed when the run starts."""
    if s.updated_at < s.started_at:
        yield "updated_at precedes started_at"
    if s.deadline != s.started_at + s.limits.wall_clock:
        yield "deadline drifted from started_at + wall_clock"


CHECKS: tuple[Callable[[RunState], Iterator[str]], ...] = (
    _terminal_shape,
    _never_stuck,
    _finalization_gate,
    _evidence_is_current,
    _bounded_work,
    _budget_safety,
    _phase_prerequisites,
    _identities,
    _clock,
)


def violations(state: RunState) -> tuple[str, ...]:
    return tuple(message for check in CHECKS for message in check(state))
