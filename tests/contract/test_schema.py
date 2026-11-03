"""Schema contracts: unions are complete, kinds are unique, everything survives JSON.

These checks get the journal ready for M2. Any event, effect, or state the core can
produce must persist and load back to an identical value.
"""

from __future__ import annotations

import typing

from pydantic import TypeAdapter

from msa.core import RunState
from msa.core.effects import COMMAND_TYPES, SPENDING_KINDS, AnyCommand, Effect, command_kind
from msa.core.events import (
    EVENT_ADAPTER,
    EVENT_TYPES,
    AgentFailed,
    AgentRefused,
    AnyEvent,
    CancelRequested,
    Completion,
    DeadlineExceeded,
    RunStarted,
    SpendingCompletion,
)
from msa.core.transitions import TABLE
from tests.support.explorer import MODEL_CHECK_CONFIGS, explore_config


def _union_members(annotated: object) -> set[type]:
    union = typing.get_args(annotated)[0]
    return set(typing.get_args(union))


def test_command_union_and_registry_agree() -> None:
    assert _union_members(AnyCommand) == set(COMMAND_TYPES)
    kinds = [command_kind(c) for c in COMMAND_TYPES]
    assert len(kinds) == len(set(kinds))


def test_event_union_and_registry_agree() -> None:
    assert _union_members(AnyEvent) == set(EVENT_TYPES)
    kinds = [t.model_fields["kind"].default for t in EVENT_TYPES]
    assert len(kinds) == len(set(kinds))


def test_every_awaited_command_can_be_completed_and_every_completion_targets_real_commands() -> (
    None
):
    awaited = {command_kind(c) for c in COMMAND_TYPES if c.awaits}
    completions = [t for t in EVENT_TYPES if issubclass(t, Completion)]
    completable = set().union(*(t.completes for t in completions))
    assert awaited <= completable
    assert completable <= awaited


def test_spending_commands_are_completed_by_spending_events() -> None:
    """If an LLM-backed effect could finish without reporting spend, the ledger would lie."""
    for event_type in EVENT_TYPES:
        if issubclass(event_type, Completion) and event_type.completes & SPENDING_KINDS:
            assert issubclass(event_type, SpendingCompletion), event_type.__name__


def test_every_completion_has_a_rule_or_is_a_global_escape() -> None:
    ruled = {rule.event for rule in TABLE}
    escapes = {RunStarted, CancelRequested, DeadlineExceeded, AgentFailed, AgentRefused}
    assert set(EVENT_TYPES) - ruled == escapes


def test_every_reachable_state_and_effect_survives_a_json_round_trip() -> None:
    state_adapter = TypeAdapter(RunState)
    effect_adapter = TypeAdapter(Effect)
    exploration = explore_config(*MODEL_CHECK_CONFIGS[-1])
    for state in exploration.states.values():
        assert state_adapter.validate_json(state_adapter.dump_json(state)) == state
        for effect in state.pending:
            assert effect_adapter.validate_json(effect_adapter.dump_json(effect)) == effect


def test_event_adapter_dispatches_on_kind() -> None:
    raw = b'{"kind":"deadline_exceeded","at":"2026-01-01T09:00:00Z"}'
    assert isinstance(EVENT_ADAPTER.validate_json(raw), DeadlineExceeded)
