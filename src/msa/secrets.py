"""Finding secrets in text: credentials that must never be committed, journaled or sent.

The scanner is used wherever text crosses a trust boundary:

- the task, before a run is created (``msa run``) and again in VALIDATING, because the task
  is journaled and sent to the model;
- every line a change adds, in SECURITY_REVIEW;
- the final patch, in FINAL_VERIFICATION.

The rules favour precision. A provider's key format (AWS, GitHub, Anthropic, Slack, Google,
Stripe, private key blocks) is matched exactly. A generic ``password = "..."`` assignment
counts only if the value looks random (entropy) and is not a placeholder. A missed secret
is possible; a flood of false alarms would teach people to ignore the gate.

A match never carries the secret. ``Match.redacted`` keeps the first four characters and
the length, which is enough to find the line and too little to use the key, because
reports, journals and run files are all stored.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

_PROVIDER_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("aws access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})")),
    ("anthropic api key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai api key", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe live key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{24,}")),
)

_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<name>[a-z0-9_.-]*(?:password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token))
    ["']?\s*[:=]\s*
    (?P<quote>["'])(?P<value>[^"'\s]{12,})(?P=quote)
    """
)
_PLACEHOLDERS = (
    "example",
    "changeme",
    "change-me",
    "dummy",
    "placeholder",
    "redacted",
    "your",
    "xxxx",
    "****",
    "<",
    "${",
    "{{",
    "fake",
    "test",
    "sample",
)
MIN_ENTROPY_BITS = 3.5  # per character: random keys score 4 to 6, words about 2.5 to 3.5


@dataclass(frozen=True, slots=True)
class Match:
    rule: str
    line: int  # 1-based, in whatever text was scanned
    redacted: str

    def render(self, path: str | None = None) -> str:
        where = f"{path}:{self.line}" if path else f"line {self.line}"
        return f"{where}: {self.rule} ({self.redacted})"


def redact(secret: str) -> str:
    return f"{secret[:4]}… ({len(secret)} chars)"


def entropy(text: str) -> float:
    """Shannon entropy in bits per character."""
    if not text:
        return 0.0
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def _generic(line: str) -> Iterable[str]:
    for match in _ASSIGNMENT.finditer(line):
        value = match["value"]
        lowered = value.lower()
        if any(p in lowered for p in _PLACEHOLDERS):
            continue
        if entropy(value) >= MIN_ENTROPY_BITS:
            yield value


def scan_line(line: str, number: int) -> list[Match]:
    found: list[Match] = []
    for rule, pattern in _PROVIDER_RULES:
        found.extend(Match(rule, number, redact(m[0])) for m in pattern.finditer(line))
    if not found:  # a provider key is also an assignment: report it once
        found.extend(Match("credential assignment", number, redact(v)) for v in _generic(line))
    return found


def scan(text: str) -> list[Match]:
    """Every secret in ``text``, by line."""
    return [m for n, line in enumerate(text.splitlines(), start=1) for m in scan_line(line, n)]


def scan_lines(lines: Iterable[tuple[int, str]]) -> list[Match]:
    """Every secret in numbered lines, such as the lines a diff adds to one file."""
    return [m for number, line in lines for m in scan_line(line, number)]
