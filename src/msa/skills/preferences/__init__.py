"""The preferences skill (PLAN §5): layered, scoped, versioned coding preferences.

``schema`` defines a preference, ``resolve`` merges the layers and refuses contradictions,
``render`` produces the byte-stable prompt block, ``store`` reads and writes the layer files,
and ``lint`` reports what resolution lets through. ``SKILL.md`` is the same guidance in the
Agent Skill format, so the skill also works outside this harness.
"""

from msa.skills.preferences.resolve import (
    Layer,
    PreferenceConflict,
    PreferenceError,
    Resolved,
    merge,
    resolve,
)
from msa.skills.preferences.schema import Preference, Scope

__all__ = [
    "Layer",
    "Preference",
    "PreferenceConflict",
    "PreferenceError",
    "Resolved",
    "Scope",
    "merge",
    "resolve",
]
