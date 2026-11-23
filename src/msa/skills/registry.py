"""Skills (ADR-016): versioned, typed instructions that a role carries into its invocations.

A skill is one YAML file per version, ``<layer>/skills/<id>/<version>.yaml``::

    format: 1
    id: py.migrations
    version: 1.2.0
    description: How this repository writes database migrations.
    extends: py.base@1.0.0          # optional
    roles: [coder]
    instructions: |
      Every schema change gets a migration in migrations/, generated, never hand-edited.
    constraints: ["Never edit an applied migration."]
    allowed_tools: [read_file, search, edit, create_file, run, git_diff]
    inputs:  {table: {type: string, description: The table to change}}
    outputs: {migration: {type: string, description: The new migration's path}}

**Layers.** The user layer (``$MSA_USER_SKILLS``, else ``~/.config/msa/skills``) and the
repo layer (``<root>/.msa/skills``). A repo file overrides a user file of the same id and
version; the registry says which one is in force.

**Versions are immutable.** An edit is a new version. A version another skill extends
cannot be deleted. A role uses the latest version of each skill that names it.

**Inheritance only narrows.** A skill that extends another inherits every field it leaves
out. Its roles and tools must be among its parent's: a child can drop a capability, never
add one. Inputs and outputs may be added, never retyped. Instructions and constraints
accumulate, parent first.

**What a skill may not do.** Hold a secret, in any field: skills are sent to the model and
stored. Give a writing tool (``edit``, ``create_file``) to a role that writes no files. Grant
a tool the security policy forbids that role (``msa.skills.policy``): policy outranks every
skill, as it outranks every preference.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from msa.config import CapabilitiesConfig, Role
from msa.core._frozen import Frozen
from msa.secrets import scan

FORMAT = 1
SkillId = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$", max_length=100)
]
Version = Annotated[str, StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")]
Reference = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+@(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
    ),
]
type SkillTool = Literal["read_file", "search", "edit", "create_file", "run", "git_diff"]
TOOLS: tuple[SkillTool, ...] = ("read_file", "search", "edit", "create_file", "run", "git_diff")
WRITE_TOOLS: frozenset[str] = frozenset({"edit", "create_file"})
type FieldType = Literal["string", "integer", "number", "boolean", "array", "object"]
type LayerName = Literal["user", "repo"]
REPO_DIR = Path(".msa") / "skills"


class SkillError(ValueError):
    """A skill file, or the registry as a whole, is not valid."""


class IOField(Frozen):
    type: FieldType
    description: str = ""
    required: bool = True


class SkillDocument(Frozen):
    """One version of one skill, as written. Fields left out are inherited (or empty)."""

    format: Literal[1]
    id: SkillId
    version: Version
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    extends: Reference | None = None
    roles: tuple[Role, ...] = ()
    instructions: str = ""
    constraints: tuple[str, ...] = ()
    allowed_tools: tuple[SkillTool, ...] | None = None  # None: inherit, or no restriction
    inputs: dict[Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*$")], IOField] = Field(
        default_factory=dict
    )
    outputs: dict[Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*$")], IOField] = (
        Field(default_factory=dict)
    )

    @model_validator(mode="after")
    def _shape(self) -> SkillDocument:
        if self.extends is None and not self.roles:
            raise ValueError("a skill that extends nothing must name its roles")
        if self.extends is not None and self.extends.split("@")[0] == self.id:
            raise ValueError("a skill cannot extend a version of itself")
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("roles lists a role twice")
        if self.allowed_tools is not None and len(set(self.allowed_tools)) != len(
            self.allowed_tools
        ):
            raise ValueError("allowed_tools lists a tool twice")
        return self

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"

    def texts(self) -> Iterable[tuple[str, str]]:
        """Every free-text field, by name, for the secret scan."""
        yield "description", self.description
        yield "instructions", self.instructions
        for n, constraint in enumerate(self.constraints):
            yield f"constraints[{n}]", constraint
        for kind, fields in (("inputs", self.inputs), ("outputs", self.outputs)):
            for name, spec in fields.items():
                yield f"{kind}.{name}.description", spec.description


class Skill(Frozen):
    """A skill as a role uses it: its whole lineage resolved."""

    id: SkillId
    version: Version
    description: str
    roles: tuple[Role, ...]
    instructions: str
    constraints: tuple[str, ...]
    allowed_tools: tuple[SkillTool, ...] | None
    inputs: dict[str, IOField]
    outputs: dict[str, IOField]
    lineage: tuple[str, ...]  # id@version, this skill first
    source: str  # the file in force

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"

    def check_inputs(self, values: Mapping[str, object]) -> list[str]:
        """What is wrong with ``values`` as this skill's inputs (empty: nothing)."""
        return _check_fields("input", self.inputs, values)

    def check_outputs(self, values: Mapping[str, object]) -> list[str]:
        return _check_fields("output", self.outputs, values)


_PYTHON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _check_fields(
    kind: str, spec: Mapping[str, IOField], values: Mapping[str, object]
) -> list[str]:
    problems = [f"unknown {kind} {name!r}" for name in sorted(set(values) - set(spec))]
    for name, field in sorted(spec.items()):
        if name not in values:
            if field.required:
                problems.append(f"missing required {kind} {name!r}")
            continue
        value = values[name]
        wanted = _PYTHON_TYPES[field.type]
        wrong_bool = isinstance(value, bool) and field.type in {"integer", "number"}
        if not isinstance(value, wanted) or wrong_bool:
            problems.append(f"{kind} {name!r} must be {field.type}, got {type(value).__name__}")
    return problems


def version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return major, minor, patch


# ---------- reading


def _yaml() -> YAML:
    return YAML(typ="safe", pure=True)


@dataclass(frozen=True, slots=True)
class Entry:
    document: SkillDocument
    source: str
    layer: LayerName


def user_dir() -> Path:
    if explicit := os.environ.get("MSA_USER_SKILLS"):
        return Path(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "msa" / "skills"


def read_document(path: Path) -> SkillDocument:
    try:
        data: Any = _yaml().load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise SkillError(f"{path}: not valid YAML: {exc}") from None
    return parse_document(data, str(path))


def parse_document(data: object, source: str) -> SkillDocument:
    if not isinstance(data, dict):
        raise SkillError(f"{source}: expected a mapping with format, id and version")
    try:
        document = SkillDocument.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'skill'}: {e['msg']}" for e in exc.errors()
        )
        raise SkillError(f"{source}: {problems}") from None
    if leaks := [
        f"{name}: {m.rule} ({m.redacted})" for name, text in document.texts() for m in scan(text)
    ]:
        raise SkillError(
            f"{source}: holds what looks like a secret ({'; '.join(leaks)}). Skills are sent "
            "to the model and stored: refer to credentials by name, never by value."
        )
    return document


def scan_layer(directory: Path, layer: LayerName) -> list[Entry]:
    if not directory.is_dir():
        return []
    entries = []
    for path in sorted(directory.glob("*/*.yaml")):
        document = read_document(path)
        expected = Path(document.id) / f"{document.version}.yaml"
        if path.relative_to(directory) != expected:
            raise SkillError(f"{path}: {document.reference} belongs at {directory / expected}")
        entries.append(Entry(document, str(path), layer))
    return entries


# ---------- the registry


@dataclass(frozen=True, slots=True)
class Registry:
    entries: Mapping[tuple[str, str], Entry]  # (id, version) -> the entry in force
    shadowed: Mapping[tuple[str, str], Entry]  # user entries a repo entry overrides

    @classmethod
    def from_entries(cls, entries: Iterable[Entry]) -> Registry:
        in_force: dict[tuple[str, str], Entry] = {}
        shadowed: dict[tuple[str, str], Entry] = {}
        for entry in sorted(entries, key=lambda e: e.layer == "repo"):  # user, then repo
            key = (entry.document.id, entry.document.version)
            if key in in_force and in_force[key].layer == entry.layer:
                raise SkillError(f"{entry.document.reference} is defined twice in {entry.layer}")
            if key in in_force:
                shadowed[key] = in_force[key]
            in_force[key] = entry
        return cls(in_force, shadowed)

    @classmethod
    def empty(cls) -> Registry:
        return cls({}, {})

    @classmethod
    def load(cls, root: Path, *, user: Path | None = None) -> Registry:
        return cls.from_entries(
            [*scan_layer(user or user_dir(), "user"), *scan_layer(root / REPO_DIR, "repo")]
        )

    def ids(self) -> list[str]:
        return sorted({skill_id for skill_id, _ in self.entries})

    def versions(self, skill_id: str) -> list[str]:
        return sorted((v for i, v in self.entries if i == skill_id), key=version_key)

    def latest(self, skill_id: str) -> Skill:
        versions = self.versions(skill_id)
        if not versions:
            raise SkillError(f"no skill {skill_id}")
        return self.get(skill_id, versions[-1])

    def get(self, skill_id: str, version: str) -> Skill:
        return self._resolve(skill_id, version, ())

    def lookup(self, reference: str) -> Skill:
        """``id`` (the latest version) or ``id@version``."""
        skill_id, _, version = reference.partition("@")
        return self.get(skill_id, version) if version else self.latest(skill_id)

    def for_role(self, role: Role) -> tuple[Skill, ...]:
        """The latest version of every skill the role may use, by id."""
        found = (self.latest(skill_id) for skill_id in self.ids())
        return tuple(s for s in found if role in s.roles)

    def dependents(self, skill_id: str, version: str) -> list[str]:
        target = f"{skill_id}@{version}"
        return sorted(
            e.document.reference for e in self.entries.values() if e.document.extends == target
        )

    def _resolve(self, skill_id: str, version: str, seen: tuple[str, ...]) -> Skill:
        reference = f"{skill_id}@{version}"
        if reference in seen:
            raise SkillError(f"inheritance cycle: {' -> '.join((*seen, reference))}")
        entry = self.entries.get((skill_id, version))
        if entry is None:
            known = self.versions(skill_id)
            hint = f" (versions: {', '.join(known)})" if known else ""
            raise SkillError(f"no skill {reference}{hint}")
        doc = entry.document
        if doc.extends is None:
            return Skill(
                id=doc.id,
                version=doc.version,
                description=doc.description,
                roles=doc.roles,
                instructions=doc.instructions.strip(),
                constraints=_unique(doc.constraints),
                allowed_tools=doc.allowed_tools,
                inputs=dict(doc.inputs),
                outputs=dict(doc.outputs),
                lineage=(reference,),
                source=entry.source,
            )
        parent_id, _, parent_version = doc.extends.partition("@")
        parent = self._resolve(parent_id, parent_version, (*seen, reference))
        return _inherit(parent, doc, entry.source)

    def validate(
        self, capabilities: CapabilitiesConfig, forbidden: Mapping[Role, Sequence[str]]
    ) -> None:
        """Resolve every skill and check it against the capabilities and the policy;
        raise one error listing every problem."""
        problems: list[str] = []
        for skill_id, version in sorted(self.entries):
            try:
                skill = self.get(skill_id, version)
            except SkillError as exc:
                problems.append(str(exc))
                continue
            problems += permission_problems(skill, capabilities, forbidden)
        if problems:
            raise SkillError("; ".join(dict.fromkeys(problems)))


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i.strip() for i in items if i.strip()))


def _inherit(parent: Skill, doc: SkillDocument, source: str) -> Skill:
    where = f"{doc.reference} (extends {parent.reference})"
    roles = doc.roles or parent.roles
    if extra := [r.value for r in roles if r not in parent.roles]:
        raise SkillError(f"{where}: adds roles {extra}: a child may only narrow its parent")
    tools = doc.allowed_tools if doc.allowed_tools is not None else parent.allowed_tools
    narrowed = parent.allowed_tools is None or tools is None
    if not narrowed and (
        extra_tools := [t for t in tools or () if t not in (parent.allowed_tools or ())]
    ):
        raise SkillError(f"{where}: adds tools {extra_tools}: a child may only narrow its parent")
    inputs, outputs = dict(parent.inputs), dict(parent.outputs)
    for kind, mine, theirs in (("input", doc.inputs, inputs), ("output", doc.outputs, outputs)):
        for name, spec in mine.items():
            if name in theirs and theirs[name].type != spec.type:
                raise SkillError(
                    f"{where}: retypes {kind} {name!r} from {theirs[name].type} to {spec.type}"
                )
            theirs[name] = spec
    instructions = "\n\n".join(t for t in (parent.instructions, doc.instructions.strip()) if t)
    return Skill(
        id=doc.id,
        version=doc.version,
        description=doc.description,
        roles=roles,
        instructions=instructions,
        constraints=_unique((*parent.constraints, *doc.constraints)),
        allowed_tools=tools,
        inputs=inputs,
        outputs=outputs,
        lineage=(doc.reference, *parent.lineage),
        source=source,
    )


def permission_problems(
    skill: Skill, capabilities: CapabilitiesConfig, forbidden: Mapping[Role, Sequence[str]]
) -> list[str]:
    """What the skill grants that a role may not have. A skill that restricts no tools
    grants nothing: the role keeps exactly what its capabilities and the policy allow."""
    if skill.allowed_tools is None:
        return []
    tools = set(skill.allowed_tools)
    problems = []
    for role in skill.roles:
        if not capabilities.roles[role].write and (granted := sorted(WRITE_TOOLS & tools)):
            problems.append(
                f"{skill.reference}: gives {role.value} the writing tools {granted}, but "
                f"{role.value} writes no files ([capabilities])"
            )
        if banned := sorted(set(forbidden.get(role, ())) & tools):
            problems.append(
                f"{skill.reference}: allows {role.value} {banned}, which the security policy "
                "forbids it"
            )
    return problems


def allowed_tools(skills: Sequence[Skill]) -> frozenset[str] | None:
    """The tools a role may use under all its skills: the intersection of what each one
    allows, or None when none restricts them."""
    restricting = [frozenset(s.allowed_tools) for s in skills if s.allowed_tools is not None]
    if not restricting:
        return None
    return frozenset.intersection(*restricting)


def render(skills: Sequence[Skill]) -> str:
    """The skills block of a role's system prompt. Byte-stable for one set of skills."""
    if not skills:
        return ""
    lines = ["# Skills", "", "Apply each of these whenever its subject comes up.", ""]
    for skill in skills:
        lines += [f"## {skill.reference}: {skill.description.strip()}", ""]
        if skill.instructions:
            lines += [skill.instructions, ""]
        if skill.constraints:
            lines += ["Constraints:", *(f"- {c}" for c in skill.constraints), ""]
        for title, fields in (("Inputs", skill.inputs), ("Outputs", skill.outputs)):
            if fields:
                lines.append(f"{title}:")
                lines += [
                    f"- `{name}` ({spec.type}{'' if spec.required else ', optional'})"
                    + (f": {spec.description}" if spec.description else "")
                    for name, spec in sorted(fields.items())
                ]
                lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------- writing


def layer_dir(root: Path, layer: LayerName) -> Path:
    return root / REPO_DIR if layer == "repo" else user_dir()


def path_of(directory: Path, document: SkillDocument) -> Path:
    return directory / document.id / f"{document.version}.yaml"


def add(
    root: Path,
    text: str,
    *,
    layer: LayerName,
    capabilities: CapabilitiesConfig,
    forbidden: Mapping[Role, Sequence[str]],
    user: Path | None = None,
) -> Skill:
    """Store a new skill version from its YAML. Refused if that version exists in the
    layer (versions are immutable), or if the registry would no longer validate."""
    try:
        data: Any = _yaml().load(text)
    except YAMLError as exc:
        raise SkillError(f"not valid YAML: {exc}") from None
    document = parse_document(data, "the new skill")
    directory = user if (layer == "user" and user is not None) else layer_dir(root, layer)
    target = path_of(directory, document)
    if target.exists():
        raise SkillError(
            f"{document.reference} already exists in {target}: versions are immutable, so "
            "add the change as a new version"
        )
    current = Registry.load(root, user=user)
    entry = Entry(document, str(target), layer)
    candidate = Registry.from_entries(
        [*current.entries.values(), *current.shadowed.values(), entry]
    )
    candidate.validate(capabilities, forbidden)
    _write_atomic(target, text if text.endswith("\n") else text + "\n")
    return candidate.get(document.id, document.version)


def delete(root: Path, reference: str, *, layer: LayerName, user: Path | None = None) -> Path:
    """Remove one version from a layer. Refused while another skill extends it."""
    skill_id, _, version = reference.partition("@")
    if not version:
        raise SkillError(f"{reference!r}: name the version to delete (id@version)")
    directory = user if (layer == "user" and user is not None) else layer_dir(root, layer)
    current = Registry.load(root, user=user)
    target = directory / skill_id / f"{version}.yaml"
    if not target.exists():
        raise SkillError(f"no {reference} in the {layer} layer ({target})")
    shadowed = current.shadowed.get((skill_id, version))
    if (dependents := current.dependents(skill_id, version)) and shadowed is None:
        raise SkillError(f"{reference} is extended by {', '.join(dependents)}: delete those first")
    target.unlink()
    if not any(target.parent.iterdir()):
        target.parent.rmdir()
    return target


def export(skill: Skill) -> str:
    """The resolved skill as a standalone document (no ``extends``): it can be added to
    another repository as is."""
    data: dict[str, Any] = {
        "format": FORMAT,
        "id": skill.id,
        "version": skill.version,
        "description": skill.description,
        "roles": [r.value for r in skill.roles],
    }
    if skill.instructions:
        data["instructions"] = skill.instructions
    if skill.constraints:
        data["constraints"] = list(skill.constraints)
    if skill.allowed_tools is not None:
        data["allowed_tools"] = list(skill.allowed_tools)
    for kind, fields in (("inputs", skill.inputs), ("outputs", skill.outputs)):
        if fields:
            data[kind] = {n: f.model_dump(exclude_defaults=True) for n, f in sorted(fields.items())}
    yaml = YAML()
    yaml.default_flow_style = False
    out = StringIO()
    yaml.dump(data, out)
    return out.getvalue()


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
