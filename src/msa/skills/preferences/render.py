"""The block of preferences an agent's system prompt carries (PLAN §5.3).

It is rendered once per run from the selected preferences and must be byte-stable: the
same set always renders to the same bytes, whatever order layers or files arrived in, so
the cached system-prompt prefix stays valid (ADR-008). Preferences are listed by id, and
each one's text is normalised (trailing whitespace dropped, line endings unified).
"""

from __future__ import annotations

from collections.abc import Iterable

from msa.skills.preferences.schema import Preference

LABELS = {"hard": "HARD (a blocker)", "soft": "soft (a nit)", "info": "info (context)"}
HEADER = (
    "## Coding preferences\n"
    "Follow these when writing or reviewing code. A HARD preference is a blocker; a soft "
    "one is a nit; info is context. A preference marked [checked] is verified by a tool "
    "before review, so do not spend effort re-checking it by hand."
)


def _clean(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def _indented(label: str, text: str) -> str:
    body = _clean(text).replace("\n", "\n    ")
    return f"  {label}: {body}"


def render(preferences: Iterable[Preference]) -> str:
    entries = sorted((p for p in preferences if p.enforcement != "off"), key=lambda p: p.id)
    if not entries:
        return ""
    blocks = [HEADER]
    for p in entries:
        checked = " [checked]" if p.check is not None else ""
        lines = [f"- {p.id} | {LABELS[p.enforcement]}{checked} | {p.scope.describe()}"]
        lines.append(_indented("Rule", p.rule))
        if p.rationale:
            lines.append(_indented("Why", p.rationale))
        if p.examples and p.examples.good:
            lines.append(_indented("Good", p.examples.good))
        if p.examples and p.examples.bad:
            lines.append(_indented("Bad", p.examples.bad))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
