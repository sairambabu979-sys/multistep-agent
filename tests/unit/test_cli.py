from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from msa import __version__, cli
from msa.core.graph import render_markdown
from msa.core.table import TransitionTable
from msa.core.transitions import TABLE

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_fsm_check_passes_for_the_production_table() -> None:
    result = runner.invoke(cli.app, ["fsm", "check"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("ok:")


def test_fsm_check_fails_with_reasons_for_a_broken_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "TABLE", TransitionTable(()))
    result = runner.invoke(cli.app, ["fsm", "check"])
    assert result.exit_code == 1
    assert "error: PLANNING is unreachable from INTAKE" in result.output


def test_fsm_diagram_prints_the_generated_page() -> None:
    result = runner.invoke(cli.app, ["fsm", "diagram"])
    assert result.exit_code == 0
    assert result.output == render_markdown(TABLE)


def test_fsm_diagram_writes_to_a_file(tmp_path: Path) -> None:
    target = tmp_path / "fsm.md"
    result = runner.invoke(cli.app, ["fsm", "diagram", "--output", str(target)])
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8") == render_markdown(TABLE)


def test_main_runs_the_app(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["msa", "version"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__
