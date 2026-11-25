"""Reading one setting by its dotted path."""

from configlib.loader import Config, Value

_MISSING = object()


def get(config: Config, path: str, default: object = _MISSING) -> Value | object:
    """``config[section][key]`` for ``path`` "section.key"."""
    if path.count(".") != 1:
        raise ValueError(f"a path is 'section.key', not {path!r}")
    section, key = path.split(".")
    try:
        return config[section][key]
    except KeyError:
        if default is _MISSING:
            raise KeyError(path) from None
        return default
