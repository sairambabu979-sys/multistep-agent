"""Whether this machine can run containers, for the container sandbox's tests (ADR-019).

Where Docker is not installed, or its daemon is not running, those tests skip. With
``MSA_REQUIRE_CONTAINER=1`` (set in CI, whose runners have Docker) they fail instead, so
the container backend is never silently untested.

The definition-of-done run in containers also needs its image, which CI builds from
tests/fixtures/images/demo and names in ``MSA_DEMO_IMAGE``.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from msa.config import ContainerConfig, SandboxConfig
from msa.tools.sandbox import Completed, LocalSandbox, container_problem

REQUIRED = os.environ.get("MSA_REQUIRE_CONTAINER") == "1"
IMAGE = "python:3.13-slim"  # nothing but Python, which the tests run
DEMO_IMAGE = os.environ.get("MSA_DEMO_IMAGE", "")


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


def needs_the_demo_image() -> str:
    """The image the definition-of-done run uses in containers, or a skip (a failure in CI)."""
    needs_containers()
    why = None
    if not DEMO_IMAGE:
        why = "MSA_DEMO_IMAGE names no image: build it from tests/fixtures/images/demo"
    else:
        docker = shutil.which("docker") or "docker"
        try:
            done = subprocess.run(  # a fixed argv, no shell
                [docker, "image", "inspect", DEMO_IMAGE],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            why = f"docker image inspect {DEMO_IMAGE!r} did not answer: {exc}"
        else:
            if done.returncode != 0:
                why = f"there is no image {DEMO_IMAGE!r}: build tests/fixtures/images/demo"
    if why is None:
        return DEMO_IMAGE
    if REQUIRED:
        pytest.fail(f"MSA_REQUIRE_CONTAINER=1, but {why}")
    pytest.skip(why)


def recording_runtime(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a ``docker`` ahead of the real one on PATH that logs each call, then runs it.

    The log is the evidence that a run's commands went through containers, not only that
    its settings asked for them. One JSON argv per line; returns the log's path."""
    real = shutil.which("docker")
    assert real is not None, "needs_containers() found no docker"
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "calls.jsonl"
    shim = directory / "docker"
    shim.write_text(
        f"""#!{sys.executable}
import json, os, sys
with open({str(log)!r}, "a", encoding="utf-8") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
os.execv({real!r}, [{real!r}, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    return log


def recording_host_spawns(log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Log each argv a sandbox starts on this machine (``LocalSandbox._spawn``), the
    container runtime included, so a test can show what bypassed the containers. A forked
    child inherits the patch, and each call is appended and closed before the spawn."""
    spawn = LocalSandbox._spawn

    async def logged(self: LocalSandbox, argv: Sequence[str], **kwargs: Any) -> Completed:
        with log.open("a", encoding="utf-8") as spawns:
            spawns.write(json.dumps(list(argv)) + "\n")
        return await spawn(self, argv, **kwargs)

    log.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(LocalSandbox, "_spawn", logged)


def calls(log: Path) -> list[list[str]]:
    """What :func:`recording_runtime` or :func:`recording_host_spawns` logged."""
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    return [json.loads(line) for line in text.splitlines()]
