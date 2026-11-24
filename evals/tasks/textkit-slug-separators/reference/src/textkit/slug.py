"""URL slugs."""

import re
import unicodedata

from textkit.utils import norm


def slugify(text: str, separator: str = "-") -> str:
    """Lowercase ASCII words joined by ``separator``: "Crème brûlée" gives "creme-brulee"."""
    ascii_text = unicodedata.normalize("NFKD", norm(text)).encode("ascii", "ignore").decode()
    words = re.split(r"[^a-z0-9]+", ascii_text.lower())
    return separator.join(word for word in words if word)
