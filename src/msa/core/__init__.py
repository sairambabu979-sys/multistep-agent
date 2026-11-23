"""The functional core: a pure, total, invariant-checked state machine.

Nothing in this package performs I/O, reads a clock, or draws randomness. This is enforced
by ``tests/contract/test_core_purity.py``. Everything here can be tested exhaustively without
an LLM.

Public API::

    step = start(RunStarted(...))         # create a run
    step = transition(step.state, event)  # advance it; raises TransitionRejected
    state = replay(journal_events)        # rebuild it from the journal
    state = fold(snapshot, journal_tail)  # or resume from a snapshot
"""

from msa.core.errors import InvariantViolation, Rejection, ReplayError, TransitionRejected
from msa.core.machine import fold, replay, start, transition
from msa.core.state import RunState
from msa.core.table import Edge, Step
from msa.core.transitions import TABLE

__all__ = [
    "TABLE",
    "Edge",
    "InvariantViolation",
    "Rejection",
    "ReplayError",
    "RunState",
    "Step",
    "TransitionRejected",
    "fold",
    "replay",
    "start",
    "transition",
]
