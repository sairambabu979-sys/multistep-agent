"""``[verify].commands`` names sandbox commands; config cannot import the sandbox, so the
two lists are kept in step here."""

from __future__ import annotations

from typing import get_args

from msa.config import VerifyCommand, load_config
from msa.tools.sandbox import COMMANDS
from tests.support.llm import ROOT


def test_verify_commands_are_exactly_the_sandbox_allowlist() -> None:
    assert set(get_args(VerifyCommand.__value__)) == set(COMMANDS)


def test_the_committed_settings_verify_with_every_allowlisted_command() -> None:
    assert set(load_config(ROOT / "msa.toml").verify.commands) == set(COMMANDS)
