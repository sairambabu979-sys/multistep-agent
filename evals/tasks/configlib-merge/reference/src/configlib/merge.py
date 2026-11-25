"""Merging settings, section by section and key by key."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from configlib.loader import Config


def merge(base: Config, override: Config) -> Config:
    """A new Config: every section of ``override`` merged key by key over ``base``."""
    result: Config = {section: dict(values) for section, values in base.items()}
    for section, values in override.items():
        result[section] = {**result.get(section, {}), **values}
    return result
