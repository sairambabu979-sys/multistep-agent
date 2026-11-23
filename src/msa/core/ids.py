"""Identifier types.

Each identifier is a ``NewType`` so mypy rejects passing a ``TreeHash`` where a ``FindingId``
is expected. The ``Valid*`` aliases add runtime validation and are what model fields use.
"""

from __future__ import annotations

from typing import Annotated, NewType

from pydantic import StringConstraints

RunId = NewType("RunId", str)
EffectId = NewType("EffectId", str)
TreeHash = NewType("TreeHash", str)
PlanId = NewType("PlanId", str)
FindingId = NewType("FindingId", str)
Digest = NewType("Digest", str)

# A run id never contains ':' so that effect ids ("<run>:<seq>:<index>") parse unambiguously.
RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
# git object ids: SHA-1 (40 hex) or SHA-256 (64 hex) repositories.
TREE_HASH_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"

ValidRunId = Annotated[RunId, StringConstraints(pattern=RUN_ID_PATTERN)]
ValidTreeHash = Annotated[TreeHash, StringConstraints(pattern=TREE_HASH_PATTERN)]
ValidDigest = Annotated[Digest, StringConstraints(pattern=DIGEST_PATTERN)]
ValidPlanId = Annotated[PlanId, StringConstraints(pattern=SLUG_PATTERN)]
ValidFindingId = Annotated[FindingId, StringConstraints(pattern=SLUG_PATTERN)]
ValidEffectId = Annotated[EffectId, StringConstraints(min_length=1)]


def effect_id(run_id: RunId, seq: int, index: int) -> EffectId:
    """Deterministic effect id: the same journal always yields the same ids.

    This is what makes effects idempotent across crashes: on resume, the interpreter can
    tell whether an effect with this id already completed.
    """
    return EffectId(f"{run_id}:{seq}:{index}")
