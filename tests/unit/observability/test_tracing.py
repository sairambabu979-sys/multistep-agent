"""Traces (ADR-015 §3): a span per effect, per invocation and per tool call, nested as the
work is (effect > invocation > tool), tagged with the run and effect, and holding no
prompt, argument or file content."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from msa.core.effects import Effect
from msa.core.events import AnyEvent
from msa.llm import fake
from msa.llm.gateway import Gateway
from msa.observability import tracing
from msa.runtime.clock import LogicalClock
from msa.runtime.interpreter import Interpreter
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import Profile, SimulatedEnvironment, SimulationConfig, digest
from msa.runtime.telemetry import Traced
from tests.support.factory import limits
from tests.support.llm import VERDICT, config, invocation

_EXPORTER = InMemorySpanExporter()
_CONFIGURED: list[bool] = []
HAPPY = SimulationConfig(seed=0, profile=Profile.HAPPY)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """One provider per process (an OpenTelemetry rule): installed once, cleared per test."""
    if not _CONFIGURED:
        tracing.configure(_EXPORTER)
        _CONFIGURED.append(True)
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def named(found: tuple[ReadableSpan, ...] | list[ReadableSpan], prefix: str) -> list[ReadableSpan]:
    return [s for s in found if s.name.startswith(prefix)]


def test_none_installs_nothing() -> None:
    assert tracing.configure("none") is None


def test_an_invocation_and_its_tool_calls_are_spans_without_content(
    spans: InMemorySpanExporter,
) -> None:
    client = fake.ScriptedClient(
        fake.message(
            fake.thinking(),
            fake.tool_use("a", "read_file", {"path": "secret_plans.py"}),
            stop_reason="tool_use",
        ),
        fake.answer(VERDICT),
    )
    asyncio.run(Gateway(client, config()).invoke(invocation()))
    finished = spans.get_finished_spans()
    (invoke,) = named(finished, "invoke ")
    (tool,) = named(finished, "tool ")
    assert invoke.name == "invoke reviewer"
    assert invoke.attributes is not None
    assert (invoke.attributes["msa.outcome"], invoke.attributes["msa.turns"]) == ("ok", 2)
    assert invoke.attributes["msa.key"] == "run-1:9:0"
    assert tool.attributes is not None
    assert (tool.name, tool.attributes["msa.error"]) == ("tool read_file", False)
    assert tool.parent is not None
    assert tool.parent.span_id == invoke.context.span_id  # the tool ran inside the invocation
    everything = " ".join(str(dict(s.attributes or {})) for s in finished)
    assert "secret_plans.py" not in everything  # no argument reaches a span
    assert "You review code." not in everything  # nor any prompt


class Nested:
    """The simulator, with a span of its own inside each executor: as a gateway call is."""

    def __init__(self) -> None:
        self.happy = SimulatedEnvironment(HAPPY)

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        with tracing.tracer().start_as_current_span(f"work {effect.command.kind}"):
            return await self.happy.execute(effect, now=now)


def test_every_effect_is_a_span_and_the_work_inside_it_is_its_child(
    spans: InMemorySpanExporter, tmp_path: Path
) -> None:
    with SqliteJournal(tmp_path / "runs.db") as journal:
        interpreter = Interpreter(journal, Nested(), clock=LogicalClock(), observer=Traced())
        final = asyncio.run(
            interpreter.start(
                run_id="run-1",  # type: ignore[arg-type]
                task_digest=digest("t"),
                limits=limits(),
                environment=HAPPY.model_dump(mode="json"),
            )
        )
    assert final.phase.value == "completed"
    finished = spans.get_finished_spans()
    effects = named(finished, "effect ")
    kinds = [s.name.removeprefix("effect ") for s in effects]
    assert kinds[:3] == ["admit_run", "validate_request", "load_context"]
    assert kinds[-1] == "publish_release"
    by_id = {s.context.span_id: s for s in effects}
    for work in named(finished, "work "):
        assert work.parent is not None
        parent = by_id[work.parent.span_id]
        assert parent.name == work.name.replace("work ", "effect ")
    admit = effects[0]
    assert admit.attributes is not None
    assert admit.attributes[tracing.RUN] == "run-1"
    assert admit.attributes[tracing.EFFECT] == "run-1:1:0"
    assert (admit.attributes["msa.event"], admit.attributes["msa.phase"]) == (
        "RunAdmitted",
        "validating",
    )
