"""Time, owned by the shell.

The core never reads a clock: every event carries its own ``at``. The interpreter stamps
each event as it commits it, so time is monotonic by construction even when concurrent
effects finish out of order. Which clock does the stamping is a policy:

* ``SystemClock``: wall time, used for real runs. It enforces the run's deadline.
* ``LogicalClock``: one tick per event, used for simulations. It is deterministic, so a
  crashed and resumed simulation reproduces an uninterrupted one exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from msa.core import RunState

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class Clock(Protocol):
    def start_time(self) -> datetime:
        """The ``at`` of a new run's ``RunStarted``."""
        ...

    def stamp(self, state: RunState) -> datetime:
        """The ``at`` for the next event. Must never precede ``state.updated_at``."""
        ...

    def wait_budget(self, state: RunState) -> float | None:
        """Seconds to wait for a completion before declaring the deadline passed.
        ``None`` waits indefinitely."""
        ...


class SystemClock:
    def start_time(self) -> datetime:
        return datetime.now(UTC)

    def stamp(self, state: RunState) -> datetime:
        return max(datetime.now(UTC), state.updated_at)

    def wait_budget(self, state: RunState) -> float | None:
        return max(0.0, (state.deadline - datetime.now(UTC)).total_seconds())


class LogicalClock:
    """Each event happens exactly one tick after the previous one."""

    def __init__(self, epoch: datetime = EPOCH, tick: timedelta = timedelta(seconds=1)) -> None:
        self.epoch = epoch
        self.tick = tick

    def start_time(self) -> datetime:
        return self.epoch

    def stamp(self, state: RunState) -> datetime:
        return state.updated_at + self.tick

    def wait_budget(self, state: RunState) -> float | None:
        return None
