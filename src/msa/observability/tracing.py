"""OpenTelemetry traces (ADR-015 §3): one span per effect, per agent invocation, per tool call.

Spans are made through the OpenTelemetry *API*, which does nothing until a provider is
configured, so an untraced run pays nothing for them. ``configure`` installs the SDK with
an exporter: ``console`` prints finished spans as JSON (to stderr), and tests install an
in-memory one. Every span carries the run and effect it belongs to, under the ``msa.*``
attribute names below, so a trace can be joined with the journal and the run files.

Nothing secret goes in a span: no prompt, no tool argument, no file content. Only ids,
kinds, roles, models, counts, spend and outcomes.
"""

from __future__ import annotations

import sys
from typing import Literal

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)

NAME = "msa"
RUN, EFFECT, KIND, ROLE, TOOL = "msa.run", "msa.effect", "msa.kind", "msa.role", "msa.tool"
type Exporter = Literal["none", "console"]


def tracer() -> trace.Tracer:
    return trace.get_tracer(NAME)


def configure(exporter: Exporter | SpanExporter) -> TracerProvider | None:
    """Install a provider that sends spans to ``exporter``. ``none`` installs nothing. A
    provider can be set once per process (an OpenTelemetry rule), so call this at start."""
    if exporter == "none":
        return None
    sink = ConsoleSpanExporter(out=sys.stderr) if exporter == "console" else exporter
    provider = TracerProvider(resource=Resource.create({"service.name": NAME}))
    provider.add_span_processor(SimpleSpanProcessor(sink))
    trace.set_tracer_provider(provider)
    return provider
