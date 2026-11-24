"""Small text utilities."""

from textkit.frequencies import word_frequencies
from textkit.slug import slugify
from textkit.utils import norm, title_case
from textkit.wrap import wrap

__all__ = ["norm", "slugify", "title_case", "word_frequencies", "wrap"]
