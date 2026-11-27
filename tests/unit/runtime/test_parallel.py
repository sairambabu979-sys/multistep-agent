"""gather_bounded: independent checks at once, bounded, reported in a fixed order."""

from __future__ import annotations

import asyncio
from functools import partial

import pytest

from msa.runtime.parallel import gather_bounded


class Probe:
    """Jobs that sleep, and record how many run at once."""

    def __init__(self) -> None:
        self.running = 0
        self.most = 0
        self.finished: list[str] = []

    async def job(self, name: str, seconds: float, error: Exception | None = None) -> str:
        self.running += 1
        self.most = max(self.most, self.running)
        try:
            await asyncio.sleep(seconds)
        finally:
            self.running -= 1
            self.finished.append(name)
        if error is not None:
            raise error
        return name


def test_results_come_back_in_job_order_whatever_order_they_finish_in() -> None:
    probe = Probe()
    jobs = [partial(probe.job, "slow", 0.05), partial(probe.job, "fast", 0.0)]
    assert asyncio.run(gather_bounded(jobs, 3)) == ["slow", "fast"]
    assert probe.finished == ["fast", "slow"]  # they did run at once
    assert probe.most == 2


def test_no_more_than_the_limit_run_at_once() -> None:
    probe = Probe()
    jobs = [partial(probe.job, f"j{i}", 0.01) for i in range(7)]
    assert asyncio.run(gather_bounded(jobs, 3)) == [f"j{i}" for i in range(7)]
    assert probe.most == 3
    probe = Probe()
    asyncio.run(gather_bounded([partial(probe.job, f"j{i}", 0.0) for i in range(3)], 1))
    assert probe.most == 1  # a limit of one is one after another


def test_every_job_runs_to_its_end_and_the_first_failure_in_order_is_raised() -> None:
    """The later job fails first; the earlier one, failing later, is the one reported, so the
    report does not depend on timing. And nothing is cancelled half way."""
    probe = Probe()
    jobs = [
        partial(probe.job, "a", 0.03, RuntimeError("a could not run")),
        partial(probe.job, "b", 0.0, RuntimeError("b could not run")),
        partial(probe.job, "c", 0.01),
    ]
    with pytest.raises(RuntimeError, match="a could not run"):
        asyncio.run(gather_bounded(jobs, 3))
    assert sorted(probe.finished) == ["a", "b", "c"]


def test_a_limit_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        asyncio.run(gather_bounded([], 0))
