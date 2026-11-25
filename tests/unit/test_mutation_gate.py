from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from scripts.mutation_gate import main, problems, score
from tests.support.scripts import ROOT

CLEAN = {"killed": 1067, "timeout": 3, "survived": 0, "total": 1070, "skipped": 0}


def test_a_clean_run_passes_and_reports_its_score(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stats = tmp_path / "stats.json"
    stats.write_text(json.dumps(CLEAN))
    assert main(["--stats", str(stats)]) == 0
    assert capsys.readouterr().out.startswith("mutation score 100.0%: 1067 killed, 3 timed out")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"survived": 1}, "1 mutant(s) survived (budget 0)"),
        ({"no_tests": 2}, "2 mutant(s) in code no selected test reaches"),
        ({"suspicious": 1}, "1 suspicious result(s): rerun before trusting"),
        ({"total": 0}, "no mutants were tested: did `mutmut run` run?"),
    ],
)
def test_each_kind_of_gap_fails_the_gate(overrides: dict[str, int], expected: str) -> None:
    assert problems({**CLEAN, **overrides}, max_survived=0) == [expected]


def test_the_budget_is_configurable() -> None:
    assert problems({**CLEAN, "survived": 2}, max_survived=2) == []


def test_score_counts_timeouts_as_detected_and_ignores_skipped() -> None:
    assert score({"killed": 8, "timeout": 1, "total": 10, "skipped": 1}) == 100.0
    assert score({"total": 0}) == 0.0


def test_missing_stats_fail_with_a_hint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--stats", str(tmp_path / "absent.json")]) == 1
    assert "mutmut export-cicd-stats" in capsys.readouterr().err


def test_every_path_mutmut_copies_exists_and_no_copied_file_is_below_the_root() -> None:
    """mutmut copies a file in ``also_copy`` with ``shutil.copy2`` into ``mutants/``, which
    creates no parent directory: a file below the root (``evals/decision.toml``) crashes the
    whole mutation run before a mutant is tested. A directory is copied whole, parents and
    all, so list the directory instead."""
    also_copy = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "mutmut"
    ]["also_copy"]
    assert [p for p in also_copy if not (ROOT / p).exists()] == []
    assert [p for p in also_copy if (ROOT / p).is_file() and Path(p).parent != Path(".")] == []
