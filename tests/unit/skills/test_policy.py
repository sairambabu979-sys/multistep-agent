"""The security policy (ADR-016): locks outrank every preference layer, the run's --pref
flags included; a lock on an unknown preference is an error; and the policy reaches the
roles' tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from msa.config import Role
from msa.skills import policy
from msa.skills.policy import PolicyError
from msa.skills.preferences import PreferenceError, store

KEY = "AKIA" + "Q3EGX7PLMR2TBN5W"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    repo = tmp_path / "repo"
    (repo / ".msa").mkdir(parents=True)
    return repo


def set_policy(root: Path, text: str) -> None:
    (root / policy.POLICY_FILE).write_text(text, encoding="utf-8")


LOCK = """
format: 1
locked:
  - id: py.errors.no-bare-except
    enforcement: hard
    reason: Swallowed exceptions hid an outage.
forbidden_tools:
  tester: [run]
"""


def test_no_policy_file_is_the_empty_policy(root: Path) -> None:
    rules = policy.load(root)
    assert (rules.locked, rules.forbidden_tools, rules.source) == ((), {}, "built-in")


def test_a_lock_outranks_every_layer_including_the_runs_flags(root: Path) -> None:
    set_policy(root, LOCK)
    (root / store.REPO_FILE).write_text(
        "version: 1\npreferences:\n  - id: py.errors.no-bare-except\n    enforcement: soft\n",
        encoding="utf-8",
    )
    resolved = policy.preferences(root, ["py.errors.no-bare-except=off"])
    locked = resolved.get("py.errors.no-bare-except")
    assert locked is not None
    assert locked.enforcement == "hard"
    assert resolved.provenance["py.errors.no-bare-except"]["enforcement"] == (
        f"security policy ({root / policy.POLICY_FILE})"
    )
    unlocked = store.load(root, ["py.errors.no-bare-except=off"]).get("py.errors.no-bare-except")
    assert unlocked is not None
    assert unlocked.enforcement == "off"  # what the lock overrode


def test_a_lock_on_an_unknown_preference_is_an_error(root: Path) -> None:
    set_policy(root, "format: 1\nlocked:\n  - {id: py.typo.here, enforcement: hard}\n")
    with pytest.raises(PreferenceError, match=r"locks py\.typo\.here, which no preference layer"):
        policy.preferences(root)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("format: 1\nlocked:\n  - {id: py.x.y, enforcement: off}\n", "enforcement"),
        ("format: 2\n", "format"),
        ("format: 1\nforbidden_tools:\n  coder: [rm]\n", "forbidden_tools"),
        (
            "format: 1\nlocked:\n  - {id: py.x.y, enforcement: hard}\n"
            "  - {id: py.x.y, enforcement: soft}\n",
            "locked twice",
        ),
        ("[1, 2]\n", "expected a mapping"),
        (f"format: 1\nlocked:\n  - {{id: py.x.y, enforcement: hard, reason: {KEY}}}\n", "secret"),
    ],
)
def test_an_invalid_policy_is_refused(root: Path, text: str, message: str) -> None:
    set_policy(root, text)
    with pytest.raises(PolicyError, match=message) as info:
        policy.load(root)
    assert KEY not in str(info.value)


def test_policy_errors_reject_a_run_like_preference_errors() -> None:
    assert issubclass(PolicyError, PreferenceError)


def test_forbidden_tools_are_listed_per_role(root: Path) -> None:
    set_policy(root, LOCK)
    rules = policy.load(root)
    assert rules.forbids(Role.TESTER) == ("run",)
    assert rules.forbids(Role.CODER) == ()
