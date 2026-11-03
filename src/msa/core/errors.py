"""Errors raised by the core.

``TransitionRejected`` means the *event* is invalid in the current state (stale, duplicated,
out of phase). The state is untouched. The interpreter logs the event and does not journal it.

``InvariantViolation`` means the *core itself* is wrong. It is never expected in production.
Every transition checks the invariants, so a bug surfaces at the step that caused it, not
three phases later.
"""

from __future__ import annotations

from enum import StrEnum


class Rejection(StrEnum):
    TERMINAL = "terminal"
    ALREADY_STARTED = "already_started"
    CLOCK_REGRESSION = "clock_regression"
    UNKNOWN_EFFECT = "unknown_effect"
    MISMATCHED_COMPLETION = "mismatched_completion"
    NOT_ACCEPTED_IN_PHASE = "not_accepted_in_phase"
    STALE_EVIDENCE = "stale_evidence"
    UNKNOWN_FINDING = "unknown_finding"
    ILLEGAL_VERDICT = "illegal_verdict"
    INCOMPLETE_RULING = "incomplete_ruling"


class TransitionRejected(Exception):
    def __init__(self, reason: Rejection, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class InvariantViolation(RuntimeError):
    def __init__(self, violations: tuple[str, ...]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


class ReplayError(Exception):
    """A journal could not be folded back into a state: it is corrupt or out of order."""
