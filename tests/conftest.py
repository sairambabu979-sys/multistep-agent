"""Hypothesis profiles. Select with HYPOTHESIS_PROFILE=dev|ci|nightly|mutation (default: dev)."""

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
# Mutation testing needs the same examples on every run, or a mutant could be killed in
# one run and survive the next.
settings.register_profile(
    "mutation", max_examples=40, derandomize=True, database=None, deadline=None
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
