"""Serialization of events and states for the journal, with schema versioning.

Every journaled event is stored with a version ``v``. When an event's shape changes, bump
``CURRENT_EVENT_VERSION`` and register an *upcaster* for each kind whose shape changed:
a pure function that rewrites the payload from version ``v`` to ``v + 1``. Old journals
then load through the chain of upcasters instead of becoming unreadable.

Snapshots are caches, not history. A snapshot written under a different
``STATE_VERSION`` is ignored and the state is rebuilt from events.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, cast

from msa.core import RunState
from msa.core.events import EVENT_ADAPTER, EVENT_TYPES, AnyEvent, EventBase

CURRENT_EVENT_VERSION = 1
STATE_VERSION = 1

type Payload = dict[str, Any]
type Upcaster = Callable[[Payload], Payload]

UPCASTERS: Mapping[tuple[str, int], Upcaster] = {}


class CodecError(Exception):
    """A stored payload cannot be decoded with this version of msa."""


def event_kind(event: EventBase) -> str:
    """The discriminator of a journaled event type."""
    if type(event) not in EVENT_TYPES:
        raise CodecError(f"{type(event).__name__} is not a journaled event type")
    return cast(str, type(event).model_fields["kind"].default)


def encode_event(event: EventBase) -> str:
    event_kind(event)  # rejects event types outside the journaled union
    return EVENT_ADAPTER.dump_json(cast(AnyEvent, event)).decode()


def decode_event(
    kind: str,
    version: int,
    payload: str,
    *,
    upcasters: Mapping[tuple[str, int], Upcaster] = UPCASTERS,
    current: int = CURRENT_EVENT_VERSION,
) -> AnyEvent:
    if version > current:
        raise CodecError(
            f"{kind} v{version} was written by a newer msa (this one reads v{current})"
        )
    data: Payload = json.loads(payload)
    while version < current:
        upcast = upcasters.get((kind, version))
        if upcast is not None:
            data = upcast(data)
        version += 1
    return EVENT_ADAPTER.validate_python(data)


def encode_state(state: RunState) -> str:
    return state.model_dump_json()


def decode_state(payload: str) -> RunState:
    return RunState.model_validate_json(payload)
