"""Managing skills, preferences and the policy from the command line (ADR-016)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from msa import cli
from msa.skills import policy, registry
from msa.skills.preferences import store
from msa.skills.preferences.resolve import Layer

runner = CliRunner()
KEY = "AKIA" + "Q3EGX7PLMR2TBN5W"

SKILL = """format: 1
id: py.greeter
version: 1.0.0
description: How the greeter module is extended.
roles: [coder]
instructions: Add each new greeting next to greet().
allowed_tools: [read_file, search, edit, create_file, run]
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("MSA_USER_SKILLS", str(tmp_path / "no-user-skills"))
    root = tmp_path / "repo"
    (root / ".msa").mkdir(parents=True)
    return root


def msa(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


def skill_file(tmp_path: Path, text: str = SKILL) -> str:
    path = tmp_path / "skill.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_add_list_show_export_and_delete_a_skill(repo: Path, tmp_path: Path) -> None:
    added = msa("skills", "add", skill_file(tmp_path), "--repo", str(repo))
    assert added.exit_code == 0, added.output
    stored = repo / registry.REPO_DIR / "py.greeter" / "1.0.0.yaml"
    assert added.output.strip() == f"added py.greeter@1.0.0 ({stored})"

    again = msa("skills", "add", skill_file(tmp_path), "--repo", str(repo))
    assert again.exit_code == 1
    assert "versions are immutable" in again.output

    listed = msa("skills", "list", "--repo", str(repo))
    assert listed.output.startswith("py.greeter@1.0.0  [coder]  versions 1.0.0")
    shown = msa("skills", "show", "py.greeter", "--repo", str(repo))
    assert "## py.greeter@1.0.0: How the greeter module is extended." in shown.output
    assert "tools: read_file, search, edit, create_file, run" in shown.output
    exported = msa("skills", "export", "py.greeter@1.0.0", "--repo", str(repo))
    assert "id: py.greeter" in exported.output
    assert msa("skills", "check", "--repo", str(repo)).output.strip() == "ok: 1 skill version(s)"

    deleted = msa("skills", "delete", "py.greeter@1.0.0", "--repo", str(repo))
    assert deleted.exit_code == 0, deleted.output
    assert msa("skills", "list", "--repo", str(repo)).output.strip() == "no skills"


def test_a_skill_with_a_secret_is_refused_without_repeating_it(repo: Path, tmp_path: Path) -> None:
    leaky = SKILL.replace("Add each new greeting next to greet().", f"Deploy with {KEY}.")
    result = msa("skills", "add", skill_file(tmp_path, leaky), "--repo", str(repo))
    assert result.exit_code == 1
    assert "looks like a secret" in result.output
    assert KEY not in result.output
    assert not (repo / registry.REPO_DIR).exists()


def test_the_policy_is_shown_and_outranks_a_skill(repo: Path, tmp_path: Path) -> None:
    assert "no locks and no forbidden tools" in msa("policy", "show", "--repo", str(repo)).output
    (repo / policy.POLICY_FILE).write_text(
        "format: 1\nlocked:\n  - {id: py.debug.no-breakpoints, enforcement: hard, "
        "reason: Hung CI twice}\nforbidden_tools:\n  coder: [run]\n",
        encoding="utf-8",
    )
    shown = msa("policy", "show", "--repo", str(repo))
    assert "locked py.debug.no-breakpoints = hard: Hung CI twice" in shown.output
    assert "forbidden for coder: run" in shown.output
    refused = msa("skills", "add", skill_file(tmp_path), "--repo", str(repo))
    assert refused.exit_code == 1
    assert "allows coder ['run'], which the security policy forbids it" in refused.output

    explained = msa(
        "prefs", "show", "py.debug.no-breakpoints", "--repo", str(repo),
        "--pref", "py.debug.no-breakpoints=off",
    )  # fmt: skip
    assert explained.exit_code == 0, explained.output
    assert "hard" in explained.output
    assert "security policy" in explained.output


def test_prefs_export_writes_a_complete_layer(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "prefs.yaml"
    result = msa("prefs", "export", "--output", str(out), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    exported = Layer.from_document("repo", str(out), store._safe().load(out.read_text()))
    assert "py.errors.no-bare-except" in exported.patches
    assert exported.patches["py.errors.no-bare-except"]["enforcement"] == "hard"


def test_prefs_delete_removes_an_entry_and_refuses_what_is_not_there(repo: Path) -> None:
    added = msa(
        "prefs", "add", "team.docs.changelog", "--rule", "Update CHANGELOG.md.",
        "--repo", str(repo),
    )  # fmt: skip
    assert added.exit_code == 0, added.output
    deleted = msa("prefs", "delete", "team.docs.changelog", "--repo", str(repo))
    assert deleted.exit_code == 0, deleted.output
    assert "team.docs.changelog" not in (repo / store.REPO_FILE).read_text(encoding="utf-8")
    missing = msa("prefs", "delete", "team.docs.changelog", "--repo", str(repo))
    assert missing.exit_code == 1
    assert "team.docs.changelog is not in" in missing.output


def test_prefs_add_refuses_a_secret(repo: Path) -> None:
    result = msa(
        "prefs", "add", "team.deploy.key", "--rule", f"Deploy with {KEY}.", "--repo", str(repo)
    )
    assert result.exit_code == 1
    assert "looks like a secret" in result.output
    assert KEY not in result.output
    assert not (repo / store.REPO_FILE).exists()
