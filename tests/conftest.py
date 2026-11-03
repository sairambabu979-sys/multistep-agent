"""Hypothesis profiles. Select with HYPOTHESIS_PROFILE=dev|ci|nightly (default: dev)."""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile("dev", max_examples=60, deadline=None)
settings.register_profile(
    "ci",
    max_examples=400,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "nightly",
    max_examples=10_000,
    stateful_step_count=100,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
