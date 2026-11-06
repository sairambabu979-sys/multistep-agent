"""Who may write which paths (ADR-006), checked before every write.

A role may write a path when the path matches one of its ``write`` globs, none of its
``deny`` globs, and no ``protected`` glob. The path checked is the *resolved* one (see
``msa.tools.paths``), so an in-tree symlink cannot route a write into another role's files.
The policy is fixed when a workspace is built for a role; nothing a model sends can widen it.

Glob syntax, matched against the whole relative path:

- ``**`` as a whole segment: any number of segments. ``a/**`` is everything under ``a``,
  ``**/x.py`` is ``x.py`` at any depth, ``a/**/b`` includes ``a/b``.
- ``*``: any run of characters within one segment. ``?``: exactly one character.
- Everything else is literal. A newline is an ordinary character (filenames may hold
  one), so ``**`` also spans it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import cache

from msa.config import CapabilitiesConfig, Role
from msa.tools.errors import CapabilityDenied


@cache
def glob_regex(pattern: str) -> re.Pattern[str]:
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


def _matches(globs: tuple[str, ...], key: str, *, case_insensitive: bool) -> bool:
    """``key`` comes from ``Root.key``: NFC, and case-folded when the disk ignores case.
    Each glob gets the same treatment, so both sides compare in one form."""
    for glob in globs:
        normal = unicodedata.normalize("NFC", glob)
        if glob_regex(normal.casefold() if case_insensitive else normal).match(key):
            return True
    return False


@dataclass(frozen=True, slots=True)
class WritePolicy:
    role: Role
    config: CapabilitiesConfig
    case_insensitive: bool = False

    def allows(self, key: str) -> bool:
        own = self.config.roles[self.role]
        return (
            self._any(own.write, key)
            and not self._any(own.deny, key)
            and not self._any(self.config.protected, key)
        )

    def check(self, key: str) -> None:
        """Raise ``CapabilityDenied`` with a reason the model can act on."""
        if self.allows(key):
            return
        role = self.role.value
        if self._any(self.config.protected, key):
            reason = "it is protected: no role may write it"
        elif owners := [
            r.value
            for r, cap in self.config.roles.items()
            if r is not self.role and self._any(cap.write, key) and not self._any(cap.deny, key)
        ]:
            reason = f"it belongs to {' and '.join(owners)}"
        elif not self.config.roles[self.role].write:
            reason = f"{role} writes no files"
        else:
            reason = f"{role} may write only {', '.join(self.config.roles[self.role].write)}"
        raise CapabilityDenied(f"{role} may not write {key!r}: {reason}")

    def _any(self, globs: tuple[str, ...], key: str) -> bool:
        return _matches(globs, key, case_insensitive=self.case_insensitive)
