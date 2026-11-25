"""Word counts."""

import re
from collections import Counter

_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def word_frequencies(text: str, top: int | None = None) -> list[tuple[str, int]]:
    """(word, count) pairs, most frequent first, ties alphabetical."""
    counts = Counter(_WORD.findall(text.lower()))
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return ranked if top is None else ranked[:top]
