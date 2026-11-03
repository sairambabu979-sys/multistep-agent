"""Deterministic builders for test values."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from msa.core.events import RunStarted
from msa.core.ids import Digest, FindingId, PlanId, RunId, TreeHash
from msa.core.values import FindingRef, Limits, PlanRef, Severity

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def tree(label: object) -> TreeHash:
    return TreeHash(hashlib.sha1(f"tree:{label}".encode(), usedforsecurity=False).hexdigest())


def digest(label: object) -> Digest:
    return Digest("sha256:" + hashlib.sha256(f"digest:{label}".encode()).hexdigest())


BASE = tree("base")
BUILD = tree("build")


def plan_ref(attempt: int = 1) -> PlanRef:
    return PlanRef(id=PlanId(f"plan-{attempt}"), digest=digest(f"plan-{attempt}"))


def finding(name: str, severity: Severity) -> FindingRef:
    return FindingRef(id=FindingId(name), severity=severity)


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
