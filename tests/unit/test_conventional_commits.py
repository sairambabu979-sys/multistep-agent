from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.conventional_commits import invalid, main

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "subject",
    [
        "feat(core): EffectFailed escape",
        "fix: off-by-one in snapshot interval",
        "build: M0 project skeleton",
        "refactor(runtime)!: rename Journal.create",
        "Merge branch 'main' into feature",
        "fixup! feat: something",
    ],
)
def test_conventional_and_exempt_subjects_pass(subject: str) -> None:
    assert invalid([subject]) == []


@pytest.mark.parametrize(
    "subject",
    [
        "Add a feature",
        "feat add colon",
        "feat(Core): uppercase scope",
        "feature: not a type",
        "feat: ",
    ],
)
def test_other_subjects_fail(subject: str) -> None:
    assert invalid([subject]) == [subject]


def test_the_commit_msg_hook_reads_only_the_subject_line(tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("docs: explain the journal\n\nBody text that is free-form.\n")
    assert main(["--message-file", str(message)]) == 0
    message.write_text("explain the journal\n")
    assert main(["--message-file", str(message)]) == 1


def test_this_repository_history_is_conventional() -> None:
    """Every commit so far, so the generated changelog is complete."""
    root_commit = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout.split()[0]
    assert main(["--range", f"{root_commit}^!"]) == 0  # the root commit itself
    assert main(["--range", f"{root_commit}..HEAD"]) == 0
