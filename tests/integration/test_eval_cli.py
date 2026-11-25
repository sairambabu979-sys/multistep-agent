"""msa eval check / run / report, end to end, offline.

``msa eval run`` is driven with the scripted roles of test_eval_runner in place of the model
client, so the command's whole path runs (settings, corpus, arms, spending cap, results,
report) without spending.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from msa import cli
from msa.cli import app
from msa.evals import runner
from tests.integration.test_eval_runner import GOOD, scripts
from tests.support.scripts import ROOT

pytestmark = pytest.mark.slow

CONFIG = ["--config", str(ROOT / "msa.toml")]


def test_check_finds_the_named_tasks_fit() -> None:
    done = CliRunner().invoke(
        app,
        [
            "eval",
            "check",
            "--corpus",
            str(ROOT / "evals"),
            "--task",
            "ledger-statement",
            "--task",
            "textkit-ambiguous-names",
        ],
    )
    assert done.exit_code == 0, done.output
    assert "ok  ledger-statement" in done.output
    assert "2 of 2 task(s) fit to score" in done.output


def test_check_refuses_an_unknown_task() -> None:
    done = CliRunner().invoke(
        app, ["eval", "check", "--corpus", str(ROOT / "evals"), "--task", "nope"]
    )
    assert done.exit_code == 1
    assert "no such task: nope" in done.output


def test_run_requires_a_total_spending_cap() -> None:
    done = CliRunner().invoke(app, [*CONFIG, "eval", "run", "--corpus", str(ROOT / "evals")])
    assert done.exit_code == 2
    # click.unstyle: on GitHub Actions, typer forces Rich's terminal mode, so the usage error
    # arrives with colour codes inside it.
    assert "--max-total-usd" in click.unstyle(done.output)


@pytest.mark.parametrize("arm", ["A", "C"])
def test_run_refuses_an_unbuilt_arm_before_spending(arm: str, tmp_path: Path) -> None:
    done = CliRunner().invoke(
        app,
        [
            *CONFIG,
            "eval",
            "run",
            "--corpus",
            str(ROOT / "evals"),
            "--max-total-usd",
            "1",
            "--arm",
            arm,
            "--out",
            str(tmp_path),
        ],
    )
    assert done.exit_code == 1
    assert f"arm {arm}" in done.output
    assert not (tmp_path / runner.RESULTS).exists()


def test_run_then_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    monkeypatch.setattr(cli, "_client", scripts(GOOD))
    out = tmp_path / "results"
    ran = CliRunner().invoke(
        app,
        [
            *CONFIG,
            "eval",
            "run",
            "--corpus",
            str(ROOT / "evals"),
            "--task",
            "textkit-wrap-last-line",
            "--arm",
            "B",
            "--max-total-usd",
            "5",
            "--out",
            str(out),
        ],
    )
    assert ran.exit_code == 0, ran.output
    assert "1 run(s), each capped at $5.00 (one that errors is tried once more)" in ran.output
    assert "✓  textkit-wrap-last-line  arm B" in ran.output
    (recorded,) = (json.loads(line) for line in (out / runner.RESULTS).read_text().splitlines())
    assert runner.Result.model_validate(recorded).resolved is True

    reported = CliRunner().invoke(
        app, ["eval", "report", str(out), "--corpus", str(ROOT / "evals")]
    )
    assert reported.exit_code == 0, reported.output
    assert "# Eval report" in reported.output
    assert "**review** (arm D against B): **not enough evidence**." in reported.output
    assert "| textkit-wrap-last-line | bugfix | ✓ $" in reported.output


def test_report_without_results_fails(tmp_path: Path) -> None:
    done = CliRunner().invoke(
        app, ["eval", "report", str(tmp_path), "--corpus", str(ROOT / "evals")]
    )
    assert done.exit_code == 1
    assert "no results" in done.output
