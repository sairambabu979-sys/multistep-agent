"""Greedy word wrapping."""


def wrap(text: str, width: int) -> list[str]:
    """``text`` as lines of at most ``width`` characters, words kept whole. A word longer
    than ``width`` gets a line of its own."""
    if width < 1:
        raise ValueError("width must be at least 1")
    lines: list[str] = []
    line = ""
    for word in text.split():
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line = f"{line} {word}"
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines
