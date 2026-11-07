"""Security-relevant refusals, recorded for the operator as well as returned to the model.

A path escape, a symlink race and a capability denial each come back to the model as a
tool error, and the run continues (PLAN §6.2, I-12 and I-13). They are also worth a human's
attention: a model trying ``../../etc/passwd`` may be following injected instructions from
repository content. Every tool reports them here as ``Violation`` records.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from msa.tools.errors import CapabilityDenied, PathChanged, PathRejected, ToolError

log = logging.getLogger("msa.security")

type ViolationKind = Literal["path_rejected", "path_changed", "capability_denied"]
_KINDS: dict[type[ToolError], ViolationKind] = {
    PathRejected: "path_rejected",
    PathChanged: "path_changed",
    CapabilityDenied: "capability_denied",
}


@dataclass(frozen=True, slots=True)
class Violation:
    kind: ViolationKind
    role: str
    tool: str
    path: str  # as requested, which may be hostile: never use it as a path
    detail: str


def log_violation(violation: Violation) -> None:
    log.warning(
        "%s refused %s for %s on %r: %s",
        violation.tool,
        violation.kind,
        violation.role,
        violation.path,
        violation.detail,
    )


type Report = Callable[[Violation], None]


@contextlib.contextmanager
def reporting(report: Report, role: str, tool: str, path: str) -> Iterator[None]:
    """Record a security refusal raised inside the block, then let it propagate."""
    try:
        yield
    except ToolError as exc:
        if (kind := _KINDS.get(type(exc))) is not None:
            report(Violation(kind, role, tool, path, str(exc)))
        raise
