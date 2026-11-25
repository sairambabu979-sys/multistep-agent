"""Durations written as "1h30m", "45s", "2h"."""

import re
from datetime import timedelta

_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def parse_duration(text: str) -> timedelta:
    """'1h30m' is an hour and a half. Units h, m and s, each at most once, in that order."""
    match = _DURATION.fullmatch(text.strip())
    if not text.strip() or match is None:
        raise ValueError(f"not a duration: {text!r}")
    hours, minutes, seconds = (int(group) if group else 0 for group in match.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def format_duration(delta: timedelta) -> str:
    """The shortest text parse_duration reads back as ``delta``, to the second."""
    total = int(delta.total_seconds())
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    text = "".join(
        f"{value}{unit}" for value, unit in ((hours, "h"), (minutes, "m"), (seconds, "s")) if value
    )
    return text or "0s"
