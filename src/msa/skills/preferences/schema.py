"""What a preference is (PLAN §5.2), and the documents that hold them.

A preference document is one layer: ``{version: 1, preferences: [...]}``. Each entry names an
``id`` and any of a preference's fields. A layer may restate only the fields it changes
(``{id: x, enforcement: off}``), so an entry is a *patch*. Merged across layers, the patches
for one id must form a complete ``Preference``.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, StringConstraints, field_validator

from msa import globs
from msa.core._frozen import Frozen

SCHEMA_VERSION = 1

PreferenceId = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$", max_length=100)
]
Glob = Annotated[str, AfterValidator(globs.validate)]
# hard: a blocker. soft: a nit. info: context only. off: disabled (lets a higher layer
# switch off a lower layer's preference while the merge stays a plain union).
Enforcement = Literal["hard", "soft", "info", "off"]
Language = Literal["python", "typescript", "javascript", "go", "rust", "markdown", "toml", "yaml"]

EXTENSIONS: dict[str, Language] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".md": "markdown",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
}
FIELDS = (
    "scope",
    "rule",
    "rationale",
    "enforcement",
    "check",
    "examples",
    "conflicts_with",
    "source",
)


def language_of(path: str) -> Language | None:
    dot = path.rfind(".")
    return EXTENSIONS.get(path[dot:].lower()) if dot > path.rfind("/") else None


class Scope(Frozen):
    language: Language | None = None  # None: any language
    paths: tuple[Glob, ...] = ()  # empty: every path

    def applies_to(self, path: str) -> bool:
        if self.language is not None and language_of(path) != self.language:
            return False
        return not self.paths or any(globs.matches(g, path) for g in self.paths)

    def describe(self) -> str:
        where = ", ".join(self.paths) if self.paths else "all paths"
        return f"{self.language or 'any language'}, {where}"


class RuffCheck(Frozen):
    kind: Literal["ruff"]
    rule: Annotated[str, StringConstraints(pattern=r"^[A-Z]{1,4}[0-9]{1,4}$")]


# Checks run with ripgrep's linear-time engine, which has no lookaround or backreferences.
# Python's re accepts both, so they are refused here rather than at check time.
# An even run of backslashes (none counts) before the construct means it is not escaped.
_UNSUPPORTED = re.compile(r"(?<!\\)(?:\\\\)*(?:\(\?<?[=!]|\(\?P=|\\[1-9])")


@lru_cache(maxsize=256)
def _validate_ripgrep(pattern: str) -> None:
    try:
        done = subprocess.run(
            ["rg", "--no-config", "--engine", "default", "--quiet", "--regexp", pattern, "--", "-"],
            input="",
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        raise ValueError(
            "rg is not installed or cannot be started to validate regex checks"
        ) from None
    except subprocess.TimeoutExpired:
        raise ValueError("ripgrep regex validation timed out") from None
    if done.returncode not in {0, 1}:
        raise ValueError(f"not a valid ripgrep regular expression: {done.stderr.strip()}")


class RegexCheck(Frozen):
    """A pattern that must not occur in files the preference applies to."""

    kind: Literal["regex"]
    pattern: Annotated[str, StringConstraints(min_length=1, max_length=500)]

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from None
        if _UNSUPPORTED.search(value):
            raise ValueError(
                "lookaround and backreferences are not supported: checks run with "
                "ripgrep's linear-time regex engine"
            )
        _validate_ripgrep(value)
        return value


Check = Annotated[RuffCheck | RegexCheck, Field(discriminator="kind")]


class Examples(Frozen):
    good: str | None = None
    bad: str | None = None


class Preference(Frozen):
    id: PreferenceId
    scope: Scope = Scope()
    rule: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    rationale: str | None = None
    enforcement: Enforcement
    check: Check | None = None
    examples: Examples | None = None
    conflicts_with: tuple[PreferenceId, ...] = ()
    source: Literal["user", "learned"] = "user"


class Patch(Frozen):
    """One layer's entry for one preference: the id and whichever fields it sets."""

    id: PreferenceId
    scope: Scope | None = None
    rule: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    rationale: str | None = None
    enforcement: Enforcement | None = None
    check: Check | None = None
    examples: Examples | None = None
    conflicts_with: tuple[PreferenceId, ...] | None = None
    source: Literal["user", "learned"] | None = None

    def fields(self) -> dict[str, Any]:
        """The fields this entry sets, as data (unset fields are absent, not None)."""
        return {
            name: value
            for name, value in self.model_dump(mode="json", exclude_unset=True).items()
            if name != "id"
        }


class Document(Frozen):
    version: Literal[1]
    preferences: tuple[Patch, ...] = ()

    @field_validator("preferences")
    @classmethod
    def _unique_ids(cls, value: tuple[Patch, ...]) -> tuple[Patch, ...]:
        ids = [patch.id for patch in value]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"each id may appear once per layer; repeated: {duplicates}")
        return value
