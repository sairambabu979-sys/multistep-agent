"""Reading layer files safely, writing them without losing comments, and proposals."""

from __future__ import annotations

from pathlib import Path

import pytest

from msa.skills.preferences import Preference, PreferenceConflict, PreferenceError, store
from msa.skills.preferences.resolve import Layer

REPO_DOC = """\
# Our team's rules. Keep this comment.
version: 1
preferences:
  - id: team.no-todo   # tracked in issues instead
    rule: "Track work in issues, not TODO comments."
    enforcement: soft
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "user" / "preferences.yaml"))
    root = tmp_path / "repo"
    (root / ".msa").mkdir(parents=True)
    (root / store.REPO_FILE).write_text(REPO_DOC, encoding="utf-8")
    return root


def preference(**fields: object) -> Preference:
    return Preference.model_validate({"rule": "R.", "enforcement": "soft", **fields})


# ---------- locations


def test_the_user_layer_follows_the_override_then_xdg_then_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "explicit.yaml"))
    assert store.user_path() == tmp_path / "explicit.yaml"
    monkeypatch.delenv("MSA_USER_PREFERENCES")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert store.user_path() == tmp_path / "xdg" / "msa" / "preferences.yaml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert store.user_path() == tmp_path / "home" / ".config" / "msa" / "preferences.yaml"


def test_layers_load_lowest_first_and_missing_files_are_skipped(repo: Path) -> None:
    layers = store.load_layers(repo, ["team.no-todo=hard"])
    assert [layer.name for layer in layers] == ["built-in", "repo", "run"]
    resolved = store.load(repo, ["team.no-todo=hard"])
    assert resolved.get("team.no-todo") is not None
    assert resolved.get("team.no-todo").enforcement == "hard"  # type: ignore[union-attr]


# ---------- reading safely


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("version: 1\nversion: 1\n", "not valid YAML"),  # duplicate keys are refused
        ("x: !!python/object/apply:os.system ['touch /tmp/pwned']\n", "not valid YAML"),
        ("version: [1\n", "not valid YAML"),
        ("version: 1\npreferences: {}\n", "preferences"),
    ],
)
def test_unsafe_or_malformed_files_are_refused(repo: Path, text: str, problem: str) -> None:
    (repo / store.REPO_FILE).write_text(text, encoding="utf-8")
    with pytest.raises(PreferenceError, match=problem):
        store.load(repo)


# ---------- writing


def test_add_keeps_comments_and_layout_and_resolves(repo: Path) -> None:
    others = store.load_layers(repo)
    resolved = store.add(
        repo / store.REPO_FILE,
        preference(id="team.no-wildcard-imports", check={"kind": "ruff", "rule": "F403"}),
        others=others,
    )
    text = (repo / store.REPO_FILE).read_text(encoding="utf-8")
    assert text.startswith(REPO_DOC.rstrip("\n").split("\n    enforcement")[0])
    assert "# Our team's rules. Keep this comment." in text
    assert "# tracked in issues instead" in text
    assert "  - id: team.no-wildcard-imports\n" in text
    assert resolved.get("team.no-wildcard-imports") is not None
    assert [p.name for p in (repo / ".msa").iterdir()] == ["preferences.yaml"]  # no temp left


def test_add_creates_a_missing_or_empty_layer_file(tmp_path: Path) -> None:
    target = tmp_path / "new" / "preferences.yaml"
    store.add(target, preference(id="a.b"), others=[store.builtin_layer()], name="user")
    assert "version: 1" in target.read_text(encoding="utf-8")
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    store.add(empty, preference(id="a.c"), others=[store.builtin_layer()], name="user")
    assert "a.c" in empty.read_text(encoding="utf-8")


def test_add_refuses_a_duplicate_id_and_leaves_the_file_alone(repo: Path) -> None:
    with pytest.raises(PreferenceError, match=r"team\.no-todo is already in"):
        store.add(repo / store.REPO_FILE, preference(id="team.no-todo"), others=[])
    assert (repo / store.REPO_FILE).read_text(encoding="utf-8") == REPO_DOC


def test_add_refuses_a_preference_that_would_fail_intake(repo: Path) -> None:
    hard_user = Layer(
        "user",
        "user.yaml",
        {"user.tabs": {"rule": "Tabs.", "enforcement": "hard", "conflicts_with": ["team.spaces"]}},
    )
    with pytest.raises(PreferenceConflict, match=r"team\.spaces .* conflicts with user\.tabs"):
        store.add(
            repo / store.REPO_FILE,
            preference(id="team.spaces", enforcement="hard"),
            others=[*store.load_layers(repo), hard_user],
        )
    assert (repo / store.REPO_FILE).read_text(encoding="utf-8") == REPO_DOC


def test_a_failed_write_leaves_neither_a_partial_file_nor_a_temp(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("msa.skills.preferences.store.os.replace", broken)
    with pytest.raises(OSError, match="No space left"):
        store.add(repo / store.REPO_FILE, preference(id="a.b"), others=[])
    assert (repo / store.REPO_FILE).read_text(encoding="utf-8") == REPO_DOC
    assert [p.name for p in (repo / ".msa").iterdir()] == ["preferences.yaml"]


# ---------- proposals


def propose(repo: Path, name: str, text: str) -> None:
    (repo / store.PROPOSALS).mkdir(parents=True, exist_ok=True)
    (repo / store.PROPOSALS / name).write_text(text, encoding="utf-8")


LEARNED = """\
version: 1
reason: "Dismissed as style three times across runs."
preference:
  id: team.no-print
  rule: "Use logging."
  enforcement: soft
"""


def test_a_proposal_is_applied_only_when_approved(repo: Path) -> None:
    propose(repo, "team.no-print.yaml", LEARNED)
    (proposal,) = store.proposals(repo)
    assert proposal.reason == "Dismissed as style three times across runs."
    assert proposal.preference.source == "learned"
    assert store.load(repo).get("team.no-print") is None
    store.approve(repo, proposal, others=store.load_layers(repo))
    learned = store.load(repo).get("team.no-print")
    assert learned is not None
    assert learned.source == "learned"
    assert store.proposals(repo) == []


def test_a_rejected_proposal_is_discarded(repo: Path) -> None:
    propose(repo, "team.no-print.yaml", LEARNED)
    (proposal,) = store.proposals(repo)
    store.reject(proposal)
    assert store.proposals(repo) == []
    assert store.load(repo).get("team.no-print") is None


@pytest.mark.parametrize(
    "text",
    [
        "version: 2\npreference: {}\n",
        "version: 1\nreason: x\n",
        "version: 1\npreference: {id: a.b}\n",
        "- 1\n",
    ],
)
def test_malformed_proposals_are_refused(repo: Path, text: str) -> None:
    propose(repo, "bad.yaml", text)
    with pytest.raises(PreferenceError, match=r"bad\.yaml"):
        store.proposals(repo)
