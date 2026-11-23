"""``msa tokens`` and ``msa serve`` (ADR-015)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

from msa import cli
from msa.service.tokens import TokenStore

runner = CliRunner()


def test_tokens_are_issued_once_listed_by_person_and_revoked(tmp_path: Path) -> None:
    db = str(tmp_path / "runs.db")
    issued = runner.invoke(cli.app, ["tokens", "add", "grace", "--role", "approver", "--db", db])
    assert issued.exit_code == 0, issued.output
    token = issued.stdout.splitlines()[0]
    assert token.startswith("msa_")
    assert TokenStore(tmp_path / "tokens.json").verify(token) is not None
    listed = runner.invoke(cli.app, ["tokens", "list", "--db", db])
    assert listed.output.split()[:2] == ["grace", "approver"]
    assert token not in listed.output
    refused = runner.invoke(cli.app, ["tokens", "add", "agent:coder", "--db", db])
    assert refused.exit_code == 1
    assert "tokens are for people" in refused.output
    bad_role = runner.invoke(cli.app, ["tokens", "add", "ada", "--role", "root", "--db", db])
    assert "is not a role" in bad_role.output
    revoked = runner.invoke(cli.app, ["tokens", "revoke", "grace", "--db", db])
    assert revoked.exit_code == 0
    assert runner.invoke(cli.app, ["tokens", "list", "--db", db]).output.strip() == "no tokens"
    assert runner.invoke(cli.app, ["tokens", "revoke", "grace", "--db", db]).exit_code == 1


def test_serve_binds_to_the_configured_loopback_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(app: Any, **options: Any) -> None:
        seen.update(options, app=app)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = runner.invoke(
        cli.app, ["serve", "--repo", str(tmp_path), "--db", str(tmp_path / "runs.db")]
    )
    assert result.exit_code == 0, result.output
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 8765)
    settings = seen["app"].state.service.settings
    assert settings.state_dir == tmp_path.resolve()
    assert settings.cors_origins == ("http://localhost:3000",)
