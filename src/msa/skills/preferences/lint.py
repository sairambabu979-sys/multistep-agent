"""``msa prefs lint``: problems in a preference set that resolution does not refuse.

Errors make the set misbehave (a check that contradicts its own examples). Warnings are
advice (PLAN §5.4: "if a linter can enforce a preference, no LLM is used for it").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from msa.skills.preferences.resolve import Resolved, conflicting_pairs
from msa.skills.preferences.schema import RegexCheck


@dataclass(frozen=True, slots=True)
class LintFinding:
    level: Literal["error", "warning"]
    preference: str
    message: str

    def render(self) -> str:
        return f"{self.level}: {self.preference}: {self.message}"


def lint(resolved: Resolved) -> list[LintFinding]:
    found: list[LintFinding] = []
    known = {p.id for p in resolved.preferences}
    for p in resolved.preferences:
        if p.enforcement == "hard" and p.check is None:
            found.append(
                LintFinding(
                    "warning",
                    p.id,
                    "hard but unchecked: only the reviewer enforces it, at LLM cost. "
                    "Add a check if a linter or a pattern can catch it.",
                )
            )
        found.extend(
            LintFinding("warning", p.id, f"conflicts_with unknown {other!r}")
            for other in p.conflicts_with
            if other not in known
        )
        if isinstance(p.check, RegexCheck) and p.examples:
            pattern = re.compile(p.check.pattern, re.MULTILINE)
            if p.examples.bad and not pattern.search(p.examples.bad):
                found.append(
                    LintFinding("error", p.id, "its check does not catch its own bad example")
                )
            if p.examples.good and pattern.search(p.examples.good):
                found.append(LintFinding("error", p.id, "its check flags its own good example"))
    # Hard-against-hard pairs never get here: resolve() refuses them.
    found.extend(
        LintFinding(
            "warning",
            a.id,
            f"conflicts with {b.id} where both apply ({a.enforcement} vs {b.enforcement}); "
            "the reviewer will see both",
        )
        for a, b in conflicting_pairs(resolved, enforcement=frozenset({"hard", "soft"}))
    )
    return sorted(found, key=lambda f: (f.level != "error", f.preference, f.message))
