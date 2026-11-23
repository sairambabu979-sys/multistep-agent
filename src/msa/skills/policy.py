"""The security policy (ADR-016): rules no preference, skill or run flag can weaken.

``<root>/.msa/policy.yaml``, which no agent can write (``.msa`` is protected)::

    format: 1
    locked:                      # these preferences keep this enforcement, whatever the layers say
      - id: py.errors.no-bare-except
        enforcement: hard
        reason: Swallowed exceptions hid an outage.
    forbidden_tools:             # never offered to these roles, whatever a skill allows
      tester: [run]

**Policy outranks preferences.** A lock is applied after every preference layer, the run's
``--pref`` flags included: ``--pref py.errors.no-bare-except=off`` cannot switch a locked
preference off. ``msa prefs show`` names the policy as the source of the enforcement. A lock
on a preference that no layer defines is an error, so a typo cannot leave a rule silently
unenforced.

**Policy outranks skills.** A skill may not grant a forbidden tool, and a role's toolset
never includes one.

The built-in policy is empty: this file adds to it. Its text is scanned for secrets like
everything else that is stored.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from msa.config import Role
from msa.core._frozen import Frozen
from msa.secrets import scan
from msa.skills.preferences import store
from msa.skills.preferences.resolve import PreferenceError, Resolved
from msa.skills.preferences.schema import PreferenceId
from msa.skills.registry import SkillTool

POLICY_FILE = Path(".msa") / "policy.yaml"


class PolicyError(PreferenceError):
    """The policy file is not valid. A run whose policy is broken does not start, like one
    whose preferences are."""


class Lock(Frozen):
    id: PreferenceId
    enforcement: Literal["hard", "soft", "info"]  # a lock never switches a rule off
    reason: Annotated[str, StringConstraints(max_length=500)] = ""


class Policy(Frozen):
    format: Literal[1] = 1
    locked: tuple[Lock, ...] = ()
    forbidden_tools: dict[Role, tuple[SkillTool, ...]] = Field(default_factory=dict)
    source: str = "built-in"

    def lock_for(self, preference_id: str) -> Lock | None:
        return next((lock for lock in self.locked if lock.id == preference_id), None)

    def forbids(self, role: Role) -> tuple[str, ...]:
        return tuple(self.forbidden_tools.get(role, ()))


def load(root: Path) -> Policy:
    path = root / POLICY_FILE
    if not path.exists():
        return Policy()
    try:
        data: Any = YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise PolicyError(f"{path}: not valid YAML: {exc}") from None
    if data is None:
        return Policy(source=str(path))
    if not isinstance(data, dict):
        raise PolicyError(f"{path}: expected a mapping with format, locked, forbidden_tools")
    try:
        policy = Policy.model_validate({**data, "source": str(path)})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise PolicyError(f"{path}: {problems}") from None
    if len({lock.id for lock in policy.locked}) != len(policy.locked):
        raise PolicyError(f"{path}: a preference is locked twice")
    if leaks := [m.render() for lock in policy.locked for m in scan(lock.reason)]:
        raise PolicyError(f"{path}: holds what looks like a secret ({'; '.join(leaks)})")
    return policy


def apply(resolved: Resolved, policy: Policy) -> Resolved:
    """The preferences with every lock in force. Locks are applied last, so they outrank
    every layer; provenance names the policy for each field it set."""
    if not policy.locked:
        return resolved
    by_id = {p.id: p for p in resolved.preferences}
    provenance: dict[str, Mapping[str, str]] = dict(resolved.provenance)
    for lock in policy.locked:
        preference = by_id.get(lock.id)
        if preference is None:
            raise PreferenceError(
                f"the security policy ({policy.source}) locks {lock.id}, which no preference "
                "layer defines: fix the id, or define the preference"
            )
        if preference.enforcement != lock.enforcement:
            by_id[lock.id] = preference.model_copy(update={"enforcement": lock.enforcement})
        provenance[lock.id] = {
            **provenance.get(lock.id, {}),
            "enforcement": f"security policy ({policy.source})",
        }
    return Resolved(tuple(by_id[k] for k in sorted(by_id)), provenance)


def preferences(root: Path, overrides: Iterable[str] = (), *, user: Path | None = None) -> Resolved:
    """The run's preferences: every layer, then the policy's locks over all of them."""
    return apply(store.load(root, overrides, user=user), load(root))
