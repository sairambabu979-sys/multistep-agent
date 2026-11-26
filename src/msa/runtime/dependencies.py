"""Which pinned Python dependencies a change adds, and what pip-audit says of them.

Read from the files only, at the head and the base tree, so no package is installed and no
build hook runs:

- ``requirements*.txt`` and ``requirements*.in``: ``name==version`` lines (extras,
  environment markers and ``--hash`` options allowed);
- ``pyproject.toml``: ``[project].dependencies``, its optional dependencies and
  ``[dependency-groups]``, pinned with ``==``;
- ``uv.lock`` and ``poetry.lock``: every locked package from a package index;
- ``Pipfile.lock``: its ``default`` and ``develop`` packages.

An addition is a (name, version) the head has and the base did not: a new dependency, or a
new version of one. A requirement with no exact version cannot be audited, so it is listed,
not checked. Names are compared normalized (PEP 503).
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any, Literal

type Kind = Literal["requirements", "pyproject", "lock", "pipfile"]
_KINDS: tuple[tuple[str, Kind], ...] = (
    ("requirements*.txt", "requirements"),
    ("requirements*.in", "requirements"),
    ("pyproject.toml", "pyproject"),
    ("uv.lock", "lock"),
    ("poetry.lock", "lock"),
    ("Pipfile.lock", "pipfile"),
)
_SPEC = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*"
    r"(?P<pin>===?\s*(?P<version>[A-Za-z0-9.!+*_-]+))?"
)
_LOCAL_SOURCES = frozenset({"editable", "virtual", "path", "directory", "git", "url", "file"})


@dataclass(frozen=True, slots=True, order=True)
class Pin:
    name: str  # normalized
    version: str
    path: str  # the file that adds it


@dataclass(frozen=True, slots=True)
class Added:
    pins: tuple[Pin, ...]  # exact versions: audited
    unpinned: tuple[str, ...]  # "path: requirement", no exact version: listed only


@dataclass(frozen=True, slots=True)
class Vulnerable:
    name: str
    version: str
    ids: tuple[str, ...]
    fixed_in: tuple[str, ...]


def kind_of(path: str) -> Kind | None:
    """What sort of Python dependency file ``path`` is, or None if it is not one."""
    name = PurePosixPath(path).name
    return next((kind for pattern, kind in _KINDS if fnmatchcase(name, pattern)), None)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement(text: str) -> tuple[str, str | None] | None:
    """(name, exact version or None) of one requirement string, or None for no name."""
    spec = text.split(";", 1)[0].strip()  # an environment marker does not change the pin
    match = _SPEC.match(spec)
    if match is None:
        return None
    version = match["version"]
    return normalize(match["name"]), None if version and "*" in version else version


def _requirements_lines(text: str) -> Iterable[str]:
    joined = re.sub(r"\\\r?\n", " ", text)  # line continuations
    for raw in joined.splitlines():
        line = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
        line = re.split(r"\s--?[a-z]", line, maxsplit=1)[0].strip()  # --hash=..., options
        if line and not line.startswith("-"):  # -r, -c, -e, --index-url: not a requirement
            yield line


def _pyproject_strings(text: str) -> Iterable[str]:
    data = tomllib.loads(text)
    project = data.get("project", {})
    yield from project.get("dependencies", [])
    for group in project.get("optional-dependencies", {}).values():
        yield from group
    for group in data.get("dependency-groups", {}).values():
        yield from (item for item in group if isinstance(item, str))  # not {include-group}


def _locked(text: str) -> set[tuple[str, str]]:
    data = tomllib.loads(text)
    found = set()
    for package in data.get("package", []):
        source = package.get("source") or {}
        local = source.keys() & _LOCAL_SOURCES or source.get("type") in _LOCAL_SOURCES
        if "name" in package and "version" in package and not local:
            found.add((normalize(package["name"]), str(package["version"])))
    return found


def _pipfile(text: str) -> set[tuple[str, str]]:
    data: dict[str, Any] = json.loads(text)
    found = set()
    for section in ("default", "develop"):
        for name, entry in data.get(section, {}).items():
            version = str(entry.get("version", "")) if isinstance(entry, dict) else ""
            if version.startswith("=="):
                found.add((normalize(name), version.removeprefix("==")))
    return found


def _parse(kind: Kind, text: str) -> tuple[set[tuple[str, str]], set[str]]:
    """(pinned (name, version) pairs, requirements without an exact version)."""
    if not text.strip():
        return set(), set()
    if kind == "lock":
        return _locked(text), set()
    if kind == "pipfile":
        return _pipfile(text), set()
    strings = _pyproject_strings(text) if kind == "pyproject" else _requirements_lines(text)
    pins, loose = set(), set()
    for item in strings:
        parsed = _requirement(item)
        if parsed is None:
            continue
        name, version = parsed
        if version is None:
            loose.add(item.strip())
        else:
            pins.add((name, version))
    return pins, loose


def additions(path: str, head: str, base: str) -> Added:
    """What the change to ``path`` adds. Raises ValueError for a file that does not parse."""
    kind = kind_of(path)
    if kind is None:
        return Added((), ())
    try:
        now, now_loose = _parse(kind, head)
        before, before_loose = _parse(kind, base)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise ValueError(f"{path} does not parse: {exc}") from exc
    pins = tuple(sorted(Pin(name, version, path) for name, version in now - before))
    unpinned = tuple(sorted(f"{path}: {spec}" for spec in now_loose - before_loose))
    return Added(pins, unpinned)


def requirements(pins: Iterable[Pin]) -> str:
    """A requirements file pinning exactly ``pins``, for ``pip-audit --no-deps``."""
    return "".join(
        f"{name}=={version}\n" for name, version in sorted({(p.name, p.version) for p in pins})
    )


def vulnerable(report: str) -> list[Vulnerable]:
    """The dependencies with known vulnerabilities in ``pip-audit --format json`` output.
    Raises ValueError if it is not that output."""
    try:
        data = json.loads(report)
        dependencies = data["dependencies"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"not pip-audit's JSON output: {exc}") from exc
    found = []
    for dependency in dependencies:
        vulns = dependency.get("vulns") or []
        if not vulns:
            continue
        found.append(
            Vulnerable(
                name=normalize(dependency["name"]),
                version=str(dependency.get("version", "")),
                ids=tuple(str(v["id"]) for v in vulns),
                fixed_in=tuple(sorted({str(f) for v in vulns for f in v.get("fix_versions", [])})),
            )
        )
    return found
