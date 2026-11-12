"""``msa prefs``: every subcommand, its refusals and its exit codes (PLAN §5.6)."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from msa import cli
from msa.skills.preferences import store
from msa.tools.sandbox import Completed, LocalSandbox

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]
REPO_LAYER = """\
version: 1
preferences:
  - id: team.no-todo
    rule: "Track work in issues, not TODO comments."
    enforcement: soft
    check: { kind: regex, pattern: "TODO" }
    examples: { bad: "# TODO", good: "# see issue 12" }
"""
needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "user.yaml"))
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def f():\n    try:\n        pass\n    except:\n        pass\n"
    )
    (root / "src" / "todo.py").write_text("# TODO later\n")
    (root / ".msa").mkdir()
    (root / store.REPO_FILE).write_text(REPO_LAYER)
    return root


@pytest.fixture
def settings(tmp_path: Path) -> str:
    """msa.toml with a launcher that works without a uv project: the current interpreter."""
    text = (ROOT / "msa.toml").read_text(encoding="utf-8")
    old = 'launcher = ["uv", "run", "--frozen", "python", "-m"]'
    assert old in text
    path = tmp_path / "msa.toml"
    path.write_text(text.replace(old, f'launcher = [{sys.executable!r}, "-m"]'), encoding="utf-8")
    return str(path)


def msa(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


def test_list_shows_every_preference_and_its_origin(repo: Path) -> None:
    result = msa("prefs", "list", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert [line.split()[0] for line in lines] == [
        "general.no-merge-markers",
        "py.debug.no-breakpoints",
        "py.errors.no-bare-except",
        "py.output.no-print",
        "team.no-todo",
    ]
    assert lines[-1].endswith(str(repo / store.REPO_FILE))
    assert "built-in defaults" in lines[0]


def test_a_run_override_shows_up_in_list(repo: Path) -> None:
    result = msa("prefs", "list", "--repo", str(repo), "--pref", "team.no-todo=off")
    last = result.output.splitlines()[-1]
    assert last.split()[:2] == ["team.no-todo", "off"]
    assert last.endswith("+ --pref")


def test_show_attributes_every_field_to_its_layer(repo: Path) -> None:
    result = msa(
        "prefs", "show", "team.no-todo", "--repo", str(repo), "--pref", "team.no-todo=hard"
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith("team.no-todo\n")
    assert "enforcement    hard" in result.output
    assert "[--pref]" in result.output
    assert f"[{repo / store.REPO_FILE}]" in result.output
    missing = msa("prefs", "show", "no.such", "--repo", str(repo))
    assert missing.exit_code == 1
    assert "no preference 'no.such'" in missing.output


@needs_rg
def test_explain_lists_what_applies_and_what_fires_on_a_line(repo: Path, settings: str) -> None:
    result = msa("--config", settings, "prefs", "explain", "src/app.py:4", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert result.output.startswith("src/app.py: 5 preference(s) apply\n")
    assert "line 4: src/app.py:4: py.errors.no-bare-except (hard)" in result.output
    quiet = msa("--config", settings, "prefs", "explain", "src/app.py:1", "--repo", str(repo))
    assert "line 1: no findings" in quiet.output
    nothing = msa(
        "prefs",
        "explain",
        "Makefile",
        "--repo",
        str(repo),
        "--pref",
        "general.no-merge-markers=off",
        "--pref",
        "team.no-todo=off",
    )
    assert nothing.output == "Makefile: no preference applies\n"


def test_add_writes_the_repo_layer_and_refuses_what_would_fail_intake(repo: Path) -> None:
    added = msa(
        "prefs",
        "add",
        "team.no-star-imports",
        "--repo",
        str(repo),
        "--rule",
        "No star imports.",
        "--enforcement",
        "hard",
        "--language",
        "python",
        "--path",
        "src/**",
        "--ruff",
        "F403",
        "--rationale",
        "They hide where names come from.",
        "--good",
        "from x import y",
    )
    assert added.exit_code == 0, added.output
    assert added.output == f"added team.no-star-imports to {repo / store.REPO_FILE}\n"
    assert "team.no-star-imports" in (repo / store.REPO_FILE).read_text()

    clash = msa("prefs", "add", "team.no-todo", "--repo", str(repo), "--rule", "Again.")
    assert clash.exit_code == 1
    assert "team.no-todo is already in" in clash.output

    both = msa(
        "prefs", "add", "a.b", "--repo", str(repo), "--rule", "R.", "--ruff", "E1", "--regex", "x"
    )
    assert "--ruff and --regex cannot be combined" in both.output
    invalid = msa("prefs", "add", "Bad Id", "--repo", str(repo), "--rule", "R.")
    assert invalid.exit_code == 1
    assert "Bad Id: String should match pattern" in invalid.output


def test_add_to_the_user_layer(repo: Path, tmp_path: Path) -> None:
    result = msa(
        "prefs",
        "add",
        "me.tabs",
        "--user",
        "--repo",
        str(repo),
        "--rule",
        "Tabs.",
        "--regex",
        "^ {8}",
    )
    assert result.exit_code == 0, result.output
    assert "me.tabs" in (tmp_path / "user.yaml").read_text()


def test_lint_fails_only_on_errors(repo: Path) -> None:
    clean = msa("prefs", "lint", "--repo", str(repo))
    assert clean.exit_code == 0, clean.output
    assert clean.output == "0 error(s), 0 warning(s)\n"
    (repo / store.REPO_FILE).write_text(REPO_LAYER.replace('bad: "# TODO"', 'bad: "# FIXME"'))
    broken = msa("prefs", "lint", "--repo", str(repo))
    assert broken.exit_code == 1
    assert "error: team.no-todo: its check does not catch its own bad example" in broken.output


PROPOSAL = """\
version: 1
reason: "Dismissed three times."
preference: { id: team.no-print, rule: "Use logging.", enforcement: soft }
"""


def test_review_lists_approves_and_rejects_proposals(repo: Path) -> None:
    assert msa("prefs", "review", "--repo", str(repo)).output == "no proposals\n"
    (repo / store.PROPOSALS).mkdir()
    (repo / store.PROPOSALS / "a.yaml").write_text(PROPOSAL)
    listed = msa("prefs", "review", "--repo", str(repo))
    assert (
        listed.output
        == "team.no-print (soft): Use logging.\n  why proposed: Dismissed three times.\n"
    )
    unknown = msa("prefs", "review", "--repo", str(repo), "--approve", "nope.x")
    assert unknown.exit_code == 1
    assert "no proposal 'nope.x'" in unknown.output
    both = msa(
        "prefs",
        "review",
        "--repo",
        str(repo),
        "--approve",
        "team.no-print",
        "--reject",
        "team.no-print",
    )
    assert "cannot be combined" in both.output
    approved = msa("prefs", "review", "--repo", str(repo), "--approve", "team.no-print")
    assert approved.exit_code == 0, approved.output
    assert "team.no-print" in (repo / store.REPO_FILE).read_text()
    (repo / store.PROPOSALS / "b.yaml").write_text(PROPOSAL.replace("team.no-print", "team.other"))
    rejected = msa("prefs", "review", "--repo", str(repo), "--reject", "team.other")
    assert rejected.output == "rejected team.other\n"
    assert list((repo / store.PROPOSALS).iterdir()) == []


def test_review_refuses_a_proposal_that_would_conflict(repo: Path) -> None:
    (repo / store.PROPOSALS).mkdir()
    conflicting = PROPOSAL.replace(
        "enforcement: soft }", "enforcement: hard, conflicts_with: [py.errors.no-bare-except] }"
    )
    (repo / store.PROPOSALS / "a.yaml").write_text(conflicting)
    result = msa("prefs", "review", "--repo", str(repo), "--approve", "team.no-print")
    assert result.exit_code == 1
    assert "conflicting hard preferences" in result.output
    (repo / store.PROPOSALS / "bad.yaml").write_text("version: 9\n")
    assert msa("prefs", "review", "--repo", str(repo)).exit_code == 1


def test_diff_layers_shows_who_sets_what(repo: Path) -> None:
    result = msa("prefs", "diff-layers", "--repo", str(repo), "--pref", "py.output.no-print=off")
    assert result.exit_code == 0, result.output
    block = result.output.split("py.output.no-print\n")[1].split("\n")[:2]
    assert block[0].startswith("  built-in check=")
    assert block[1] == "  run      enforcement=off"


@needs_rg
def test_check_reports_findings_and_fails_on_hard_ones(repo: Path, settings: str) -> None:
    result = msa("--config", settings, "prefs", "check", "--repo", str(repo))
    assert result.exit_code == 1, result.output
    assert result.output.splitlines() == [
        "src/app.py:4: py.errors.no-bare-except (hard): E722 Do not use bare `except`",
        "src/todo.py:1: team.no-todo (soft): matches 'TODO': # TODO later",
        "2 finding(s), 1 hard, in 2 file(s)",
    ]
    soft_only = msa("--config", settings, "prefs", "check", "src/todo.py", "--repo", str(repo))
    assert soft_only.exit_code == 0
    assert soft_only.output.endswith("1 finding(s), 0 hard, in 1 file(s)\n")
    outside = msa("--config", settings, "prefs", "check", "../elsewhere.py", "--repo", str(repo))
    assert outside.exit_code == 1
    assert "resolves outside the repository" in outside.output


def test_check_without_ripgrep_says_so(
    repo: Path, settings: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(repo / "no-bin"))
    result = msa("--config", settings, "prefs", "check", "--repo", str(repo))
    assert result.exit_code == 1
    assert "rg is not installed" in result.output


@pytest.mark.parametrize(
    ("completed", "message"),
    [
        (Completed("rg", 2, 0.0, "src/todo.py\n", "enumeration failed", 0), "enumeration failed"),
        (Completed("rg", 1, 0.0, "", "rg: broken.txt: Permission denied", 0), "Permission denied"),
        (Completed("rg", 1, 0.0, "src/todo.py\n", "", 0), "rg --files failed"),
        (Completed("rg", None, 1.0, "src/todo.py\n", "", 0), "timed out"),
        (Completed("rg", 0, 0.0, "src/todo.py\n", "", 100), "output was elided"),
    ],
)
def test_incomplete_file_enumeration_is_a_cli_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch, completed: Completed, message: str
) -> None:
    async def execute(self: LocalSandbox, argv: Sequence[str], **kwargs: Any) -> Completed:
        assert argv == ["rg", "--files", "--sort=path"]
        return completed

    monkeypatch.setattr(LocalSandbox, "execute", execute)
    result = msa("prefs", "check", "--repo", str(repo))
    assert result.exit_code == 1
    assert message in result.output
    assert "finding(s)" not in result.output


def test_empty_successful_file_enumeration_is_allowed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def execute(self: LocalSandbox, argv: Sequence[str], **kwargs: Any) -> Completed:
        return Completed("rg", 1, 0.0, "", "", 0)  # what rg --files really does with no files

    monkeypatch.setattr(LocalSandbox, "execute", execute)
    result = msa("prefs", "check", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "0 finding(s), 0 hard, in 0 file(s)" in result.output


@needs_rg
def test_a_repo_with_no_files_checks_clean_with_real_ripgrep(repo: Path) -> None:
    for path in (repo / "src").iterdir():
        path.unlink()
    result = msa("prefs", "check", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "0 finding(s), 0 hard, in 0 file(s)" in result.output


def test_a_broken_layer_file_is_reported_by_every_command(repo: Path) -> None:
    (repo / store.REPO_FILE).write_text("version: 1\nversion: 1\n")
    for command in (["list"], ["lint"], ["diff-layers"], ["show", "x.y"]):
        result = msa("prefs", *command, "--repo", str(repo))
        assert result.exit_code == 1, command
        assert "not valid YAML" in result.output
    bad_override = msa("prefs", "list", "--repo", str(repo), "--pref", "nonsense")
    assert bad_override.exit_code == 1


def test_every_command_refuses_a_set_that_fails_intake(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "user.yaml").write_text(
        "version: 1\npreferences:\n  - id: me.todos-ok\n    rule: TODOs are fine.\n"
        "    enforcement: hard\n    conflicts_with: [team.no-todo]\n"
    )
    hard = msa("prefs", "list", "--repo", str(repo), "--pref", "team.no-todo=hard")
    assert hard.exit_code == 1
    assert "conflicting hard preferences: me.todos-ok" in hard.output
    soft = msa("prefs", "list", "--repo", str(repo))  # team.no-todo is soft: no conflict
    assert soft.exit_code == 0


def test_unchecked_preferences_and_explain_without_a_line(repo: Path) -> None:
    msa("prefs", "add", "team.naming", "--repo", str(repo), "--rule", "Name things well.")
    listed = msa("prefs", "list", "--repo", str(repo))
    naming = next(line for line in listed.output.splitlines() if line.startswith("team.naming"))
    assert naming.split()[1:3] == ["soft", "-"]
    explained = msa("prefs", "explain", "src/app.py", "--repo", str(repo))
    assert explained.exit_code == 0
    assert "line" not in explained.output.splitlines()[-1]
