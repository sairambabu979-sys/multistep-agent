"""Shortening text to a width."""


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """``text`` cut to at most ``width`` characters, ellipsis included, at a word boundary
    if any word fits."""
    if width <= len(ellipsis):
        raise ValueError("width must be larger than the ellipsis")
    if len(text) <= width:
        return text
    budget = width - len(ellipsis)
    kept = ""
    for word in text.split():
        candidate = f"{kept} {word}" if kept else word
        if len(candidate) > budget:
            break
        kept = candidate
    if not kept:
        kept = text.split()[0][:budget]
    return kept + ellipsis
