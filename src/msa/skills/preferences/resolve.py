"""Layers, their merge, and the resolved set a run uses (PLAN §5.3).

    built-in < user (~/.config/msa/preferences.yaml) < repo (.msa/preferences.yaml) < run (--pref)

**The merge.** A layer maps each id to the fields it sets. Merging is a right-biased union,
first by id and then by field: a higher layer's field replaces a lower one's, and fields it
leaves out are inherited. That is associative, the empty layer is its identity, and both
laws are property-tested. ``enforcement: off`` disables an inherited preference without
breaking the laws, since it is just another field value.

**Resolution** validates each merged entry as a complete ``Preference`` and remembers which
layer set which field (provenance, for ``msa prefs explain`` and error messages). It then
refuses contradictions: two ``hard`` preferences whose scopes may overlap, where either
declares ``conflicts_with`` the other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from msa import globs
from msa.skills.preferences.schema import Document, Preference, Scope

type LayerName = Literal["built-in", "user", "repo", "run"]
type Fields = dict[str, Any]
type Patches = dict[str, Fields]  # id -> the fields one layer sets

ORDER: tuple[LayerName, ...] = ("built-in", "user", "repo", "run")


class PreferenceError(ValueError):
    """A layer, or the merge of the layers, is not a valid preference set."""


class PreferenceConflict(PreferenceError):
    """Two hard preferences contradict each other where both apply."""


@dataclass(frozen=True, slots=True)
class Layer:
    name: LayerName
    source: str  # where it came from, for messages: a path, or "--pref"
    patches: Mapping[str, Fields] = field(default_factory=dict)

    @classmethod
    def from_document(cls, name: LayerName, source: str, data: object) -> Layer:
        if data is None:  # an empty file: a layer with nothing in it yet
            return cls(name, source, {})
        if not isinstance(data, dict):
            raise PreferenceError(f"{source}: expected a mapping with version and preferences")
        try:
            document = Document.model_validate(data)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise PreferenceError(f"{source}: {problems}") from None
        return cls(name, source, {p.id: p.fields() for p in document.preferences})


def merge(lower: Mapping[str, Fields], higher: Mapping[str, Fields]) -> Patches:
    """Right-biased union by id, then by field."""
    return {key: {**lower.get(key, {}), **higher.get(key, {})} for key in {*lower, *higher}}


@dataclass(frozen=True, slots=True)
class Resolved:
    preferences: tuple[Preference, ...]  # sorted by id
    provenance: Mapping[str, Mapping[str, str]]  # id -> field -> source that set it

    def get(self, preference_id: str) -> Preference | None:
        return next((p for p in self.preferences if p.id == preference_id), None)

    def origin(self, preference_id: str) -> str:
        """The sources that contributed to a preference, lowest first."""
        sources = dict.fromkeys(self.provenance.get(preference_id, {}).values())
        return " + ".join(sources) or "nowhere"

    def select(self, files: Iterable[str]) -> tuple[Preference, ...]:
        """The enabled preferences that apply to any of ``files``, by id. The order does not
        depend on how ``files`` is ordered, so a rendered block stays byte-stable."""
        paths = sorted(set(files))
        return tuple(
            p
            for p in self.preferences
            if p.enforcement != "off" and any(p.scope.applies_to(f) for f in paths)
        )


def resolve(layers: Sequence[Layer]) -> Resolved:
    ordered = sorted(layers, key=lambda layer: ORDER.index(layer.name))
    merged: Patches = {}
    provenance: dict[str, dict[str, str]] = {}
    for layer in ordered:
        merged = merge(merged, layer.patches)
        for key, fields in layer.patches.items():
            provenance.setdefault(key, {}).update(dict.fromkeys(fields, layer.source))
            provenance[key].setdefault("id", layer.source)
    preferences = []
    for key in sorted(merged):
        try:
            preferences.append(Preference.model_validate({"id": key, **merged[key]}))
        except ValidationError as exc:
            missing = [str(e["loc"][0]) for e in exc.errors() if e["type"] == "missing"]
            detail = (
                f"no layer sets {', '.join(missing)}"
                if missing
                else "; ".join(e["msg"] for e in exc.errors())
            )
            raise PreferenceError(
                f"{key}: {detail} (defined by {_sources(provenance[key])})"
            ) from None
    result = Resolved(tuple(preferences), provenance)
    _refuse_conflicts(result)
    return result


def _sources(provenance: Mapping[str, str]) -> str:
    return " + ".join(dict.fromkeys(provenance.values()))


def scopes_overlap(a: Scope, b: Scope) -> bool:
    """Whether some path could fall in both scopes (sound: never False when one could)."""
    if a.language is not None and b.language is not None and a.language != b.language:
        return False
    first, second = a.paths or ("**",), b.paths or ("**",)
    return any(globs.may_overlap(x, y) for x in first for y in second)


def conflicting_pairs(
    resolved: Resolved, *, enforcement: frozenset[str] = frozenset({"hard"})
) -> list[tuple[Preference, Preference]]:
    """Pairs, by id, that declare a conflict, both at the given enforcement, whose scopes
    may overlap. A declaration on either side counts."""
    by_id = {p.id: p for p in resolved.preferences}
    pairs: set[tuple[str, str]] = set()
    for p in resolved.preferences:
        for other in p.conflicts_with:
            if other in by_id and other != p.id:
                pairs.add((min(p.id, other), max(p.id, other)))
    found = []
    for first_id, second_id in sorted(pairs):
        first, second = by_id[first_id], by_id[second_id]
        if (
            first.enforcement in enforcement
            and second.enforcement in enforcement
            and scopes_overlap(first.scope, second.scope)
        ):
            found.append((first, second))
    return found


def _refuse_conflicts(resolved: Resolved) -> None:
    pairs = conflicting_pairs(resolved)
    if not pairs:
        return
    lines = [
        f"{a.id} (from {resolved.origin(a.id)}) conflicts with {b.id} "
        f"(from {resolved.origin(b.id)}): both are hard and both apply to "
        f"{a.scope.describe()} / {b.scope.describe()}"
        for a, b in pairs
    ]
    advice = "make one soft, narrow a scope, or switch one off with --pref <id>=off"
    raise PreferenceConflict("conflicting hard preferences: " + "; ".join(lines) + f". {advice}")


def parse_override(text: str) -> tuple[str, Fields]:
    """``--pref <id>=<enforcement>``: a run-layer patch for one preference."""
    key, sep, value = text.partition("=")
    if not sep or value not in {"hard", "soft", "info", "off"}:
        raise PreferenceError(f"--pref {text!r}: expected <id>=hard|soft|info|off")
    try:
        Document.model_validate({"version": 1, "preferences": [{"id": key}]})
    except ValidationError:
        raise PreferenceError(f"--pref {text!r}: {key!r} is not a preference id") from None
    return key, {"enforcement": value}


def run_layer(overrides: Iterable[str]) -> Layer:
    patches: Patches = {}
    for text in overrides:
        key, fields = parse_override(text)
        patches[key] = {**patches.get(key, {}), **fields}
    return Layer("run", "--pref", patches)
