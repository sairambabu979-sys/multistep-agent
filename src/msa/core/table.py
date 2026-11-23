"""The transition table data structure.

A ``Rule`` says: in ``source`` phase, event type ``event`` is handled by ``handle``, and the
handler may only move the run to one of ``targets``. The dispatcher checks that promise on
every step. So the table is not documentation that can drift from the code: it *is* the
code, and the diagram, the static analysis, and the model checker are all derived from it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from msa.core.effects import AnyCommand, Effect
from msa.core.events import EventBase
from msa.core.state import RunState
from msa.core.values import Phase


@dataclass(frozen=True, slots=True)
class Decision:
    """What a handler decided: the next state (with its phase set) and commands to issue.

    Commands are not yet effects: the dispatcher assigns their ids and budget reservations.
    """

    state: RunState
    commands: tuple[AnyCommand, ...] = ()


@dataclass(frozen=True, slots=True)
class Rule[E: EventBase]:
    source: Phase
    event: type[E]
    targets: frozenset[Phase]
    handle: Callable[[RunState, E], Decision] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Edge:
    """One observed transition, for tracing, coverage, and model checking.

    ``escape`` marks transitions taken by a global guard (cancel, deadline, budget, agent
    failure) rather than by a rule of the table.
    """

    source: Phase | None  # None for the run's very first event
    event: str
    target: Phase
    escape: bool = False


@dataclass(frozen=True, slots=True)
class Step:
    state: RunState
    effects: tuple[Effect, ...]
    edge: Edge


class TransitionTable:
    """An immutable, duplicate-free index of rules keyed by ``(phase, event type)``."""

    __slots__ = ("_index", "_rules")

    def __init__(self, rules: tuple[Rule[Any], ...]) -> None:
        index: dict[tuple[Phase, type[EventBase]], Rule[Any]] = {}
        for rule in rules:
            key = (rule.source, rule.event)
            if key in index:
                raise ValueError(f"duplicate rule for {rule.source} x {rule.event.__name__}")
            if not rule.targets:
                raise ValueError(f"rule {rule.source} x {rule.event.__name__} has no targets")
            index[key] = rule
        self._rules = rules
        self._index = index

    def get(self, phase: Phase, event: type[EventBase]) -> Rule[Any] | None:
        return self._index.get((phase, event))

    def __iter__(self) -> Iterator[Rule[Any]]:
        return iter(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def declared_edges(self) -> frozenset[tuple[Phase, str, Phase]]:
        """Every ``(source, event name, target)`` triple a rule permits."""
        return frozenset(
            (rule.source, rule.event.__name__, target)
            for rule in self._rules
            for target in rule.targets
        )
