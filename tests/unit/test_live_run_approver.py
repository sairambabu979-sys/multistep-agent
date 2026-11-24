"""scripts/live_run_approver.py: whose name the live run's release goes out in.

The release job approves a run in the name this script prints, so it must print a person who
approved this run's deployment to the release environment, and nothing otherwise.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from tests.support.scripts import load

ENV = "live-release"


def approval(
    login: str = "ada", *, state: str = "approved", env: str = ENV, kind: str = "User"
) -> dict[str, Any]:
    return {
        "state": state,
        "environments": [{"name": env}],
        "user": {"login": login, "type": kind},
        "comment": "",
    }


def main(approvals: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(approvals)))
    code: int = load("live_run_approver").main([ENV])
    return code


def test_the_person_who_approved_the_release_environment_is_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([approval("ada")], monkeypatch) == 0
    assert capsys.readouterr().out == "ada\n"


@pytest.mark.parametrize(
    "approvals",
    [
        [],  # an environment with no reviewer: GitHub ran the job without asking anyone
        [approval(state="rejected")],
        [approval(env="production")],  # someone approved something else
        [approval("release-bot", kind="Bot")],
    ],
    ids=["nobody", "rejected", "another-environment", "a-bot"],
)
def test_without_a_persons_approval_nobody_is_named_and_it_fails(
    approvals: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(approvals, monkeypatch) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "nobody approved the 'live-release' deployment" in out.err
