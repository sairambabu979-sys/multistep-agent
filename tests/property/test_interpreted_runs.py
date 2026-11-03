"""Properties of complete runs driven by the real interpreter and journal (no crashes)."""

from __future__ import annotations

import asyncio
from collections import Counter

from hypothesis import given
from hypothesis import strategies as st

from msa.core import RunState, replay
from msa.core.effects import Effect
from msa.core.events import Completion
from msa.core.ids import RunId
from msa.core.values import Limits
from msa.runtime.clock import LogicalClock
from msa.runtime.interpreter import Interpreter
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import Profile, SimulatedEnvironment, SimulationConfig, digest
from tests.support.environment import limits_strategy

RUN = RunId("prop")


@given(
    seed=st.integers(min_value=0, max_value=10**6),
    profile=st.sampled_from(Profile),
    snapshot_every=st.integers(min_value=1, max_value=12),
    limits=limits_strategy().filter(lambda lim: lim.wall_clock.total_seconds() >= 600),
)
def test_a_journaled_run_is_reproducible_and_executes_each_effect_once(
    seed: int, profile: Profile, snapshot_every: int, limits: Limits
) -> None:
    config = SimulationConfig(seed=seed, profile=profile)
    executed: list[Effect] = []
    published: list[RunState] = []
    with SqliteJournal(":memory:") as journal:
        interpreter = Interpreter(
            journal,
            SimulatedEnvironment(config, on_execute=executed.append),
            clock=LogicalClock(),
            on_outcome=published.append,
            snapshot_every=snapshot_every,
        )
        final = asyncio.run(
            interpreter.start(
                run_id=RUN,
                task_digest=digest("t"),
                limits=limits,
                environment=config.model_dump(mode="json"),
            )
        )
        events = journal.events(RUN)
        assert journal.load(RUN) == final
        assert journal.verify(RUN).ok

    assert final.is_terminal
    assert published == [final]
    assert replay(events) == final
    # Without crashes, every effect runs exactly once, and every completion in the
    # journal answers an effect that actually ran (cancelled effects may not answer).
    runs = Counter(e.id for e in executed)
    assert set(runs.values()) <= {1}
    assert {e.effect_id for e in events if isinstance(e, Completion)} <= set(runs)
