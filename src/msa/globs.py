"""Path globs, shared by capabilities (ADR-006) and preference scopes (PLAN §5).

Matched against a whole path relative to the worktree:

- ``**`` as a whole segment: any number of segments. ``a/**`` is everything under ``a``,
  ``**/x.py`` is ``x.py`` at any depth, ``a/**/b`` includes ``a/b``.
- ``*``: any run of characters within one segment. ``?``: exactly one character.
- Everything else is literal. A newline is an ordinary character (filenames may hold
  one), so ``**`` also spans it.

It sits below every layer that matches paths, and imports nothing from the package.
"""

from __future__ import annotations

import re
from functools import cache

_WILDCARDS = "*?"


def validate(pattern: str) -> str:
    """Reject globs that cannot name a relative path inside a worktree."""
    segments = pattern.split("/")
    if "\x00" in pattern or any(seg in {"", ".", ".."} for seg in segments):
        raise ValueError(
            f"{pattern!r}: use a relative path glob without empty, '.' or '..' segments"
        )
    return pattern


@cache
def regex(pattern: str) -> re.Pattern[str]:
    segments = pattern.split("/")
    out: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            out.append(".+" if last else "(?:[^/]+/)*")
            continue
        out.extend(
            "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char)
            for char in segment
        )
        if not last:
            out.append("/")
    return re.compile(r"\A" + "".join(out) + r"\Z", re.DOTALL)


def matches(pattern: str, path: str) -> bool:
    return regex(pattern).match(path) is not None


def may_overlap(first: str, second: str) -> bool:
    """False only when no path can match both globs.

    Sound, not complete: a path matching both must start with both literal prefixes and
    end with both literal suffixes, so if either pair is incompatible the globs are
    disjoint. Otherwise they are assumed to overlap, which errs on the side of reporting
    a conflict the user can resolve by narrowing a scope.
    """
    a, b = _prefix(first), _prefix(second)
    if not (a.startswith(b) or b.startswith(a)):
        return False
    a, b = _suffix(first), _suffix(second)
    return a.endswith(b) or b.endswith(a)


def _prefix(pattern: str) -> str:
    """The literal text every match starts with."""
    cut = min((i for i, c in enumerate(pattern) if c in _WILDCARDS), default=len(pattern))
    return pattern[:cut]


def _suffix(pattern: str) -> str:
    """The literal text after the last wildcard. (A leading ``**/`` that matches nothing
    leaves a path equal to that text, so the slash never makes this unsound.)"""
    cut = max((i for i, c in enumerate(pattern) if c in _WILDCARDS), default=-1)
    return pattern[cut + 1 :]
