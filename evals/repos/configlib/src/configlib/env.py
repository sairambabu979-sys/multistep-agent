"""Overrides from the environment: APP_<SECTION>__<KEY>=value."""

from collections.abc import Mapping

from configlib.loader import Config, coerce


def with_env(config: Config, environ: Mapping[str, str], prefix: str = "APP_") -> Config:
    """A copy of ``config`` with every ``<prefix><SECTION>__<KEY>`` variable applied. The
    section and the key are separated by a double underscore; both are case-insensitive."""
    result: Config = {section: dict(values) for section, values in config.items()}
    for name, raw in environ.items():
        if not name.startswith(prefix):
            continue
        section, _, key = name[len(prefix) :].lower().partition("_")
        if not section or not key:
            continue
        values = dict(result.get(section, {}))
        values[key] = coerce(raw)
        result[section] = values
    return result
