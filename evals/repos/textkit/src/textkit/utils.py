"""Whitespace and capitalisation."""

SMALL_WORDS = frozenset({"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to"})


def norm(text: str) -> str:
    """``text`` with every run of whitespace turned into one space, and none at the ends."""
    return " ".join(text.split())


def title_case(text: str) -> str:
    """Every word capitalised, except small words such as "of" after the first word."""
    words = norm(text).split(" ")
    out = []
    for index, word in enumerate(words):
        lower = word.lower()
        if index > 0 and lower in SMALL_WORDS:
            out.append(lower)
        else:
            out.append(lower[:1].upper() + lower[1:])
    return " ".join(out)
