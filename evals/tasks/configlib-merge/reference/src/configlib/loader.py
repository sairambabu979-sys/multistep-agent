"""Settings written as "[section]" headers followed by "key = value" lines."""

from configlib.merge import merge

Value = bool | int | float | str
Config = dict[str, dict[str, Value]]


def coerce(raw: str) -> Value:
    """true/false as a bool, then an int, then a float, else the text without its quotes."""
    text = raw.strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def load(text: str) -> Config:
    """One settings file. Blank lines and lines starting with # are ignored."""
    config: Config = {}
    section: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            config.setdefault(section, {})
            continue
        key, sep, value = stripped.partition("=")
        if not sep or section is None:
            raise ValueError(f"line {number}: expected 'key = value' inside a [section]")
        config[section][key.strip().lower()] = coerce(value)
    return config


def layered(*texts: str) -> Config:
    """Several settings files, each overriding the ones before it, key by key."""
    result: Config = {}
    for text in texts:
        result = merge(result, load(text))
    return result
