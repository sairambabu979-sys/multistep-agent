"""Effect spans for the interpreter (ADR-015 §3).

``Traced`` wraps any ``Observer``: each effect gets a span from the moment its executor
starts until its completion is journaled, named for its kind and tagged with the run, the
effect, the event that completed it and the phase it moved the run to. ``effect_started``
runs inside the effect's own task, so the gateway's invocation and tool spans made while
the executor works become children of the effect's span. A cancelled effect's span ends
when the cancellation is journaled.
"""

from __future__ import annotations

from opentelemetry import context as otel_context
from opentelemetry import trace

from msa.core import Step, TransitionRejected
from msa.core.effects import CancelEffects, Effect
from msa.core.events import AnyEvent, Completion
from msa.core.ids import EffectId
from msa.observability import tracing
from msa.runtime.interpreter import Observer


class Traced(Observer):
    def __init__(self, inner: Observer | None = None) -> None:
        self.inner = inner or Observer()
        self._spans: dict[EffectId, trace.Span] = {}

    def effect_started(self, effect: Effect) -> None:
        span = tracing.tracer().start_span(f"effect {effect.command.kind}")
        span.set_attribute(tracing.RUN, effect.id.split(":")[0])
        span.set_attribute(tracing.EFFECT, effect.id)
        span.set_attribute(tracing.KIND, effect.command.kind)
        if effect.budget_usd_micros is not None:
            span.set_attribute("msa.reserved_usd_micros", effect.budget_usd_micros)
        self._spans[effect.id] = span
        # This task's context only: what the executor starts is a child of this span.
        otel_context.attach(trace.set_span_in_context(span))
        self.inner.effect_started(effect)

    def before_commit(self, event: AnyEvent, step: Step) -> None:
        self.inner.before_commit(event, step)

    def after_commit(self, event: AnyEvent, step: Step) -> None:
        if isinstance(event, Completion) and (span := self._spans.pop(event.effect_id, None)):
            span.set_attribute("msa.event", type(event).__name__)
            span.set_attribute("msa.phase", step.state.phase.value)
            span.end()
        for effect in step.effects:
            if isinstance(effect.command, CancelEffects):
                for cancelled in effect.command.effect_ids:
                    if span := self._spans.pop(cancelled, None):
                        span.set_attribute("msa.event", "cancelled")
                        span.end()
        self.inner.after_commit(event, step)

    def rejected(self, event: AnyEvent, error: TransitionRejected) -> None:
        self.inner.rejected(event, error)
