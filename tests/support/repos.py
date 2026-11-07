"""Fixture repositories, copied and committed for one test (tests/fixtures/repos/README.md)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "repos"


def git(repo: Path, *args: str) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return done.stdout.strip()


def make_repo(fixture: str, destination: Path) -> Path:
    """A git repository holding ``fixture``, with one commit."""
    shutil.copytree(FIXTURES / fixture, destination)
    git(destination, "init", "-q", "-b", "main")
    git(destination, "add", ".")
    git(destination, "commit", "-qm", "base")
    return destination
