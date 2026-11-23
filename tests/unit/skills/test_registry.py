"""The skills registry (ADR-016): versions are immutable, inheritance only narrows, no
skill holds a secret or grants a role what it may not have, and policy outranks skills."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

from msa.config import CapabilitiesConfig, Role
from msa.skills import registry
from msa.skills.registry import Registry, SkillError

CAPABILITIES = CapabilitiesConfig()
KEY = "AKIA" + "Q3EGX7PLMR2TBN5W"  # built at runtime: this file holds no key

BASE = """
format: 1
id: py.base
version: 1.0.0
description: How Python is written here.
roles: [coder, tester]
instructions: Prefer the standard library.
constraints: [Type every public function.]
allowed_tools: [read_file, search, edit, create_file, run, git_diff]
inputs:
  module: {type: string, description: The module to change}
"""

CHILD = """
format: 1
id: py.migrations
version: 1.2.0
description: How migrations are written.
extends: py.base@1.0.0
roles: [coder]
instructions: Generate migrations; never hand-edit one.
constraints: [Never edit an applied migration., Type every public function.]
allowed_tools: [read_file, search, edit, create_file, run]
outputs:
  migration: {type: string, description: The new migration}
"""


def write(directory: Path, text: str) -> None:
    data = registry.parse_document(registry._yaml().load(text), "test")
    target = directory / data.id / f"{data.version}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MSA_USER_SKILLS", str(tmp_path / "user-skills"))
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def repo_dir(root: Path) -> Path:
    return root / registry.REPO_DIR


def test_a_child_inherits_and_narrows(root: Path) -> None:
    write(repo_dir(root), BASE)
    write(repo_dir(root), CHILD)
    skill = Registry.load(root).latest("py.migrations")
    assert skill.lineage == ("py.migrations@1.2.0", "py.base@1.0.0")
    assert skill.roles == (Role.CODER,)
    assert skill.instructions == (
        "Prefer the standard library.\n\nGenerate migrations; never hand-edit one."
    )
    assert skill.constraints == ("Type every public function.", "Never edit an applied migration.")
    assert skill.allowed_tools == ("read_file", "search", "edit", "create_file", "run")
    assert set(skill.inputs) == {"module"}  # inherited
    assert set(skill.outputs) == {"migration"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("roles: [coder]", "roles: [coder, planner]"),
        ("allowed_tools: [read_file, search, edit, create_file, run]", None),
    ],
)
def test_a_child_can_never_add_what_its_parent_lacks(
    root: Path, change: str, message: str | None
) -> None:
    write(repo_dir(root), BASE)
    widened = (
        CHILD.replace(change, message)
        if message
        else CHILD.replace(change, "allowed_tools: [read_file, search, edit, create_file, run]")
    )
    if message is None:  # the parent without git_diff... and a child that wants it back
        base = BASE.replace("run, git_diff]", "run]")
        (repo_dir(root) / "py.base" / "1.0.0.yaml").unlink()
        write(repo_dir(root), base)
        widened = CHILD.replace(change, "allowed_tools: [read_file, git_diff]")
    write(repo_dir(root), widened)
    with pytest.raises(SkillError, match="a child may only narrow its parent"):
        Registry.load(root).latest("py.migrations")


def test_a_child_may_not_retype_an_inherited_field(root: Path) -> None:
    write(repo_dir(root), BASE)
    write(repo_dir(root), CHILD + "inputs:\n  module: {type: integer}\n")
    with pytest.raises(SkillError, match="retypes input 'module' from string to integer"):
        Registry.load(root).latest("py.migrations")


def test_inheritance_cycles_and_missing_parents_are_named(root: Path) -> None:
    write(repo_dir(root), CHILD.replace("extends: py.base@1.0.0", "extends: py.other@1.0.0"))
    with pytest.raises(SkillError, match=r"no skill py.other@1.0.0"):
        Registry.load(root).latest("py.migrations")
    a = BASE.replace("roles: [coder, tester]", "extends: x.b@1.0.0").replace("py.base", "x.a")
    b = BASE.replace("roles: [coder, tester]", "extends: x.a@1.0.0").replace("py.base", "x.b")
    write(repo_dir(root), a)
    write(repo_dir(root), b)
    cycle = "inheritance cycle: x.a@1.0.0 -> x.b@1.0.0 -> x.a@1.0.0"
    with pytest.raises(SkillError, match=re.escape(cycle)):
        Registry.load(root).get("x.a", "1.0.0")


def test_a_role_uses_the_latest_version_of_each_skill_that_names_it(root: Path) -> None:
    write(repo_dir(root), BASE)
    write(repo_dir(root), BASE.replace("1.0.0", "1.10.0").replace("standard library", "stdlib"))
    write(repo_dir(root), BASE.replace("1.0.0", "1.9.0"))
    found = Registry.load(root)
    assert found.versions("py.base") == ["1.0.0", "1.9.0", "1.10.0"]  # numeric, not text
    (coder,) = found.for_role(Role.CODER)
    assert coder.version == "1.10.0"
    assert found.for_role(Role.PLANNER) == ()


def test_a_repo_skill_overrides_the_same_version_in_the_user_layer(
    root: Path, tmp_path: Path
) -> None:
    write(tmp_path / "user-skills", BASE)
    write(repo_dir(root), BASE.replace("standard library", "repo's own helpers"))
    found = Registry.load(root)
    assert "repo's own helpers" in found.latest("py.base").instructions
    assert found.shadowed[("py.base", "1.0.0")].layer == "user"


@pytest.mark.parametrize(
    "where",
    [
        f"instructions: Use the key {KEY} to deploy.",
        f"description: Deploys with {KEY}",
        f"constraints: [Never print {KEY}]",
    ],
)
def test_a_skill_holding_a_secret_is_refused_without_repeating_it(root: Path, where: str) -> None:
    field = where.split(":", maxsplit=1)[0]
    text = (
        "\n".join(line for line in BASE.splitlines() if not line.startswith(f"{field}:"))
        + f"\n{where}\n"
    )
    with pytest.raises(SkillError) as info:
        registry.parse_document(registry._yaml().load(text), "skill.yaml")
    assert "looks like a secret" in str(info.value)
    assert KEY not in str(info.value)


def test_writing_tools_are_refused_for_roles_that_write_nothing(root: Path) -> None:
    write(repo_dir(root), BASE.replace("roles: [coder, tester]", "roles: [planner]"))
    with pytest.raises(
        SkillError, match=r"gives planner the writing tools \['create_file', 'edit'\]"
    ):
        Registry.load(root).validate(CAPABILITIES, {})
    write(
        repo_dir(root),
        BASE.replace("1.0.0", "2.0.0")
        .replace("roles: [coder, tester]", "roles: [planner]")
        .replace("allowed_tools: [read_file, search, edit, create_file, run, git_diff]", ""),
    )
    (repo_dir(root) / "py.base" / "1.0.0.yaml").unlink()
    Registry.load(root).validate(CAPABILITIES, {})  # no grant: nothing to refuse


def test_the_policy_outranks_skills(root: Path) -> None:
    write(repo_dir(root), BASE)
    with pytest.raises(SkillError, match=r"allows tester \['run'\], which the security policy"):
        Registry.load(root).validate(CAPABILITIES, {Role.TESTER: ("run",)})


def test_allowed_tools_intersect_across_a_roles_skills(root: Path) -> None:
    write(repo_dir(root), BASE)
    write(repo_dir(root), CHILD)
    skills = Registry.load(root).for_role(Role.CODER)
    assert registry.allowed_tools(skills) == frozenset(
        {"read_file", "search", "edit", "create_file", "run"}
    )
    assert registry.allowed_tools(()) is None


def test_typed_io_is_checked(root: Path) -> None:
    write(repo_dir(root), BASE)
    write(repo_dir(root), CHILD)
    skill = Registry.load(root).latest("py.migrations")
    assert skill.check_inputs({"module": "app.models"}) == []
    assert skill.check_inputs({}) == ["missing required input 'module'"]
    assert skill.check_inputs({"module": 3, "extra": 1}) == [
        "unknown input 'extra'",
        "input 'module' must be string, got int",
    ]
    assert skill.check_outputs({"migration": True}) == [
        "output 'migration' must be string, got bool"
    ]


def test_versions_are_immutable_and_a_used_parent_cannot_be_deleted(root: Path) -> None:
    added = registry.add(root, BASE, layer="repo", capabilities=CAPABILITIES, forbidden={})
    assert added.reference == "py.base@1.0.0"
    with pytest.raises(SkillError, match="versions are immutable"):
        registry.add(root, BASE, layer="repo", capabilities=CAPABILITIES, forbidden={})
    registry.add(root, CHILD, layer="repo", capabilities=CAPABILITIES, forbidden={})
    used = "is extended by py.migrations@1.2.0: delete those first"
    with pytest.raises(SkillError, match=re.escape(used)):
        registry.delete(root, "py.base@1.0.0", layer="repo")
    registry.delete(root, "py.migrations@1.2.0", layer="repo")
    registry.delete(root, "py.base@1.0.0", layer="repo")
    assert Registry.load(root).ids() == []
    with pytest.raises(SkillError, match="name the version"):
        registry.delete(root, "py.base", layer="repo")


def test_an_invalid_addition_writes_nothing(root: Path) -> None:
    bad = BASE.replace("roles: [coder, tester]", "roles: [planner]")
    with pytest.raises(SkillError, match="writing tools"):
        registry.add(root, bad, layer="repo", capabilities=CAPABILITIES, forbidden={})
    assert not (repo_dir(root) / "py.base").exists()


def test_a_file_must_live_where_its_id_and_version_say(root: Path) -> None:
    misplaced = repo_dir(root) / "py.base" / "9.9.9.yaml"
    misplaced.parent.mkdir(parents=True)
    misplaced.write_text(BASE, encoding="utf-8")
    with pytest.raises(SkillError, match="belongs at"):
        Registry.load(root)


def test_export_resolves_inheritance_into_a_standalone_document(root: Path) -> None:
    write(repo_dir(root), BASE)
    write(repo_dir(root), CHILD)
    text = registry.export(Registry.load(root).latest("py.migrations"))
    assert "extends" not in text
    standalone = registry.parse_document(registry._yaml().load(text), "export")
    assert standalone.reference == "py.migrations@1.2.0"
    assert "Prefer the standard library." in standalone.instructions
    assert set(standalone.inputs) == {"module"}


def test_rendering_is_byte_stable_and_names_every_part(root: Path) -> None:
    write(repo_dir(root), BASE)
    write(repo_dir(root), CHILD)
    skills = Registry.load(root).for_role(Role.CODER)
    text = registry.render(skills)
    assert text == registry.render(Registry.load(root).for_role(Role.CODER))
    assert text.startswith("# Skills\n")
    assert "## py.base@1.0.0: How Python is written here." in text
    assert "- Never edit an applied migration." in text
    assert "- `module` (string): The module to change" in text
    assert registry.render(()) == ""
