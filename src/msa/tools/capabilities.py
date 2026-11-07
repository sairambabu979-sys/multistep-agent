"""Who may write which paths (ADR-006), checked before every write.

A role may write a path when the path matches one of its ``write`` globs, none of its
``deny`` globs, and no ``protected`` glob. The path checked is the *resolved* one (see
``msa.tools.paths``), so an in-tree symlink cannot route a write into another role's files.
The policy is fixed when a workspace is built for a role; nothing a model sends can widen it.

Glob syntax is ``msa.globs``'s.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from msa import globs
from msa.config import CapabilitiesConfig, Role
from msa.tools.errors import CapabilityDenied


def _matches(patterns: tuple[str, ...], key: str, *, case_insensitive: bool) -> bool:
    """``key`` comes from ``Root.key``: NFC, and case-folded when the disk ignores case.
    Each glob gets the same treatment, so both sides compare in one form."""
    for glob in patterns:
        normal = unicodedata.normalize("NFC", glob)
        if globs.matches(normal.casefold() if case_insensitive else normal, key):
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

    def _any(self, patterns: tuple[str, ...], key: str) -> bool:
        return _matches(patterns, key, case_insensitive=self.case_insensitive)
