"""Whether this machine can run containers, for the container sandbox's tests (ADR-019).

Where Docker is not installed, or its daemon is not running, those tests skip. With
``MSA_REQUIRE_CONTAINER=1`` (set in CI, whose runners have Docker) they fail instead, so
the container backend is never silently untested.
"""

from __future__ import annotations

import functools
import os

import pytest

from msa.config import ContainerConfig, SandboxConfig
from msa.tools.sandbox import container_problem

REQUIRED = os.environ.get("MSA_REQUIRE_CONTAINER") == "1"
IMAGE = "python:3.13-slim"  # nothing but Python, which the tests run


@functools.cache
def problem() -> str | None:
    """Why Docker cannot run containers here; None if it can. Asked once per session."""
    return container_problem(
        SandboxConfig(backend="container", container=ContainerConfig(image=IMAGE))
    )


def needs_containers() -> None:
    if (why := problem()) is None:
        return
    if REQUIRED:
        pytest.fail(f"MSA_REQUIRE_CONTAINER=1, but containers cannot run here: {why}")
    pytest.skip(f"containers cannot run here: {why}")
