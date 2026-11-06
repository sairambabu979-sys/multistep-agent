"""Write capabilities (ADR-006): glob semantics and the committed policy."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from msa.config import CapabilitiesConfig, ConfigError, Role, RoleCapability, load_config
from msa.tools.capabilities import WritePolicy, glob_regex
from msa.tools.errors import CapabilityDenied

ROOT = Path(__file__).resolve().parents[3]
COMMITTED = load_config(ROOT / "msa.toml").capabilities


@pytest.mark.parametrize(
    ("glob", "path", "matches"),
    [
        ("tests/acceptance/**", "tests/acceptance/test_x.py", True),
        ("tests/acceptance/**", "tests/acceptance/deep/er/test_x.py", True),
        ("tests/acceptance/**", "tests/acceptance", False),
        ("tests/acceptance/**", "tests/acceptancex/test.py", False),
        ("tests/acceptance/**", "tests/acceptance/a\nb.py", True),  # newlines are characters
        ("**/conftest.py", "conftest.py", True),
        ("**/conftest.py", "a/b/conftest.py", True),
        ("**/conftest.py", "a/b/myconftest.py", False),
        ("src/**/*.py", "src/x.py", True),
        ("src/**/*.py", "src/a/b/x.py", True),
        ("src/**/*.py", "src/a/b/x.pyc", False),
        ("*.py", "x.py", True),
        ("*.py", "a/x.py", False),
        ("?.md", "a.md", True),
        ("?.md", "ab.md", False),
        ("**", "anything/at/all", True),
        ("a.b+c", "a.b+c", True),  # regex characters are literal
        ("a.b+c", "aXbbc", False),
    ],
)
def test_glob_semantics(glob: str, path: str, matches: bool) -> None:
    assert bool(glob_regex(glob).match(path)) is matches


@given(st.text(alphabet="ab/.*?", min_size=1, max_size=20))
def test_a_glob_without_wildcards_matches_only_itself(text: str) -> None:
    literal = text.replace("*", "x").replace("?", "y")
    assert glob_regex(literal).match(literal)
    assert not glob_regex(literal).match(literal + "z")


def policy(role: Role, *, case_insensitive: bool = False) -> WritePolicy:
    return WritePolicy(role, COMMITTED, case_insensitive)


@pytest.mark.parametrize(
    ("role", "path", "allowed"),
    [
        (Role.CODER, "src/app.py", True),
        (Role.CODER, "tests/unit/test_app.py", True),
        (Role.CODER, "tests/acceptance/test_login.py", False),
        (Role.CODER, "tests/repro/test_bug.py", False),
        (Role.CODER, ".git/config", False),
        (Role.CODER, ".msa/runs.db", False),
        (Role.TESTER, "tests/acceptance/test_login.py", True),
        (Role.TESTER, "tests/repro/deep/test_bug.py", True),
        (Role.TESTER, "src/app.py", False),
        (Role.TESTER, "tests/unit/test_app.py", False),
        (Role.PLANNER, "src/app.py", False),
        (Role.REVIEWER, "src/app.py", False),
        (Role.ARBITER, "tests/repro/test_bug.py", False),
    ],
)
def test_the_committed_policy(role: Role, path: str, allowed: bool) -> None:
    assert policy(role).allows(path) is allowed


@given(st.from_regex(r"\A[a-z_./]{1,40}\Z"))
def test_coder_and_tester_never_both_own_a_path(path: str) -> None:
    """The two BUILDING regions write disjoint paths (ADR-006), so they merge cleanly."""
    assert not (policy(Role.CODER).allows(path) and policy(Role.TESTER).allows(path))


@pytest.mark.parametrize(
    ("role", "path", "reason"),
    [
        (Role.CODER, "tests/acceptance/test_login.py", "it belongs to tester"),
        (Role.TESTER, ".git/hooks/pre-commit", "it is protected: no role may write it"),
        (Role.REVIEWER, "src/app.py", "it belongs to coder"),
        (Role.PLANNER, "notes.md", "it belongs to coder"),
        (Role.TESTER, "src/app.py", "it belongs to coder"),
    ],
)
def test_denials_say_why(role: Role, path: str, reason: str) -> None:
    with pytest.raises(CapabilityDenied, match=f"^{role.value} may not write '{path}': {reason}$"):
        policy(role).check(path)


def test_denials_without_an_owner_say_what_the_role_may_write() -> None:
    custom = CapabilitiesConfig(
        roles={
            **COMMITTED.roles,
            Role.CODER: RoleCapability(write=("src/**",)),
            Role.PLANNER: RoleCapability(write=()),
        }
    )
    with pytest.raises(CapabilityDenied, match=r"coder may write only src/\*\*$"):
        WritePolicy(Role.CODER, custom).check("README.md")
    with pytest.raises(CapabilityDenied, match=r"planner writes no files$"):
        WritePolicy(Role.PLANNER, custom).check("README.md")
    WritePolicy(Role.CODER, custom).check("src/x.py")  # allowed: no exception


def test_on_a_case_insensitive_disk_case_cannot_dodge_a_deny() -> None:
    # Root.key case-folds the path on such disks; the globs are folded to match.
    assert policy(Role.CODER, case_insensitive=True).allows("tests/acceptance/x.py") is False
    assert policy(Role.TESTER, case_insensitive=True).allows("tests/acceptance/x.py") is True
    folded = CapabilitiesConfig(
        roles={**COMMITTED.roles, Role.TESTER: RoleCapability(write=("Tests/Acceptance/**",))}
    )
    assert WritePolicy(Role.TESTER, folded, case_insensitive=True).allows("tests/acceptance/x")
    assert not WritePolicy(Role.TESTER, folded).allows("tests/acceptance/x")


@pytest.mark.parametrize("bad", ["/abs/**", "a//b", "a/../b", "./a", "a/", ""])
def test_malformed_globs_are_rejected_in_settings(tmp_path: Path, bad: str) -> None:
    text = (ROOT / "msa.toml").read_text(encoding="utf-8")
    old = 'protected = [".git/**", ".msa/**"]'
    path = tmp_path / "msa.toml"
    path.write_text(text.replace(old, f"protected = [{bad!r}]"), encoding="utf-8")
    with pytest.raises(ConfigError, match="use a relative path glob"):
        load_config(path)


def test_every_role_needs_a_capability(tmp_path: Path) -> None:
    text = (ROOT / "msa.toml").read_text(encoding="utf-8")
    path = tmp_path / "msa.toml"
    path.write_text(text.replace("arbiter = { write = [] }\n", ""), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[capabilities.roles\] is missing \['arbiter'\]"):
        load_config(path)


def test_the_built_in_defaults_are_the_committed_policy() -> None:
    assert CapabilitiesConfig() == COMMITTED


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "aws_secret"])
def test_credential_like_variables_can_never_be_passed_through(tmp_path: Path, name: str) -> None:
    text = (ROOT / "msa.toml").read_text(encoding="utf-8")
    old = 'env_passthrough = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM"]'
    assert old in text
    path = tmp_path / "msa.toml"
    path.write_text(text.replace(old, f'env_passthrough = ["PATH", "{name}"]'), encoding="utf-8")
    with pytest.raises(ConfigError, match="looks like a credential and is never passed"):
        load_config(path)
