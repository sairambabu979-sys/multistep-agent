"""Deterministic builders for test values."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from msa.core.events import RunStarted
from msa.core.ids import RunId
from msa.core.values import Limits

# Deterministic identifiers are shared with the simulator, so tests and `msa simulate`
# describe the same world.
from msa.runtime.simulation import BASE, BUILD, digest, finding, plan_ref, tree

__all__ = [
    "BASE",
    "BUILD",
    "T0",
    "digest",
    "finding",
    "limits",
    "plan_ref",
    "run_started",
    "tree",
]

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def limits(**overrides: Any) -> Limits:
    defaults: dict[str, Any] = {
        "budget_usd_micros": 5_000_000,
        "wall_clock": timedelta(minutes=45),
    }
    return Limits.model_validate({**defaults, **overrides})


def run_started(
    run_limits: Limits | None = None, *, run_id: str = "run-1", at: datetime = T0
) -> RunStarted:
    return RunStarted(
        at=at,
        run_id=RunId(run_id),
        task_digest=digest("task"),
        limits=run_limits or limits(),
    )
