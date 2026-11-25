"""Overrides from the environment: APP_<SECTION>__<KEY>=value."""

from collections.abc import Mapping

from configlib.loader import Config, coerce
from configlib.merge import merge


def with_env(config: Config, environ: Mapping[str, str], prefix: str = "APP_") -> Config:
    """A copy of ``config`` with every ``<prefix><SECTION>__<KEY>`` variable applied. The
    section and the key are separated by a double underscore; both are case-insensitive."""
    overrides: Config = {}
    for name, raw in environ.items():
        if not name.startswith(prefix):
            continue
        section, _, key = name[len(prefix) :].lower().partition("_")
        if not section or not key:
            continue
        overrides.setdefault(section, {})[key] = coerce(raw)
    return merge(config, overrides)
