"""Tracing: OpenTelemetry spans for runs, rule instances, checks and executor calls
(DESIGN.md 9, "Quan sát"). Attribute names follow the OpenTelemetry GenAI semantic
conventions where they apply, and `chipgraph.*` otherwise.

Off by default: every caller gets a `NoopTracer` unless it explicitly asks for one, and
`configure(..., offline=True)` (or `exporter="none"`) always turns tracing off regardless
of what else is configured (DESIGN.md 9: "offline` turns every export off").

Only `opentelemetry-api` is imported at module level, so importing this module (and
therefore `chipgraph.core`) never requires the OpenTelemetry SDK or an exporter to be
installed. `configure()` imports those lazily, only when they are actually needed, and
raises a clear error if they are missing.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from typing import Literal, Protocol, runtime_checkable

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Span as OtelSpan
from opentelemetry.trace import TracerProvider as OtelTracerProvider

# --- attribute names ----------------------------------------------------------------

RUN_ID = "chipgraph.run.id"
TARGET = "chipgraph.target"
RULE_ID = "chipgraph.rule.id"
RULE_INSTANCE = "chipgraph.rule.instance"
RULE_KIND = "chipgraph.rule.kind"
CHECK_ID = "chipgraph.check.id"
CHECK_STATUS = "chipgraph.check.status"
FAILURE_LABEL = "chipgraph.failure.label"

# Agent/LLM calls (used from M2 onward), per the GenAI semantic conventions.
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# --- span names ------------------------------------------------------------------------

SPAN_RUN = "chipgraph.run"
SPAN_RULE = "chipgraph.rule"
SPAN_CHECK = "chipgraph.check"
SPAN_EXECUTE = "chipgraph.execute"
SPAN_INVOKE_AGENT = "invoke_agent"

AttrValue = str | int | float | bool


@runtime_checkable
class SpanHandle(Protocol):
    """A handle to the currently open span."""

    def set(self, key: str, value: AttrValue) -> None:
        """Set (or overwrite) one attribute on the span."""
        ...

    def error(self, message: str) -> None:
        """Mark the span as failed, with `message` as the error description."""
        ...


@runtime_checkable
class Tracer(Protocol):
    """Opens spans. `NoopTracer` is the default everywhere; `OtelTracer` is real."""

    def span(self, name: str, **attributes: AttrValue) -> AbstractContextManager[SpanHandle]:
        """Open a span named `name`, with `attributes` set on it up front."""
        ...

    def flush(self) -> None:
        """Export every finished span now. Never raises."""
        ...


class _NoopSpanHandle:
    """The `SpanHandle` `NoopTracer` hands out: every call is a no-op."""

    __slots__ = ()

    def set(self, key: str, value: AttrValue) -> None:
        return None

    def error(self, message: str) -> None:
        return None


_NOOP_SPAN_HANDLE = _NoopSpanHandle()


class NoopTracer:
    """A `Tracer` that does nothing, at near-zero cost. The default everywhere."""

    @contextmanager
    def span(self, name: str, **attributes: AttrValue) -> Iterator[SpanHandle]:
        yield _NOOP_SPAN_HANDLE

    def flush(self) -> None:
        return None


class _OtelSpanHandle:
    """The `SpanHandle` `OtelTracer` hands out: wraps a live OpenTelemetry `Span`."""

    __slots__ = ("_span",)

    def __init__(self, span: OtelSpan) -> None:
        self._span = span

    def set(self, key: str, value: AttrValue) -> None:
        self._span.set_attribute(key, value)

    def error(self, message: str) -> None:
        self._span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, message))


class OtelTracer:
    """Wraps `opentelemetry.trace.get_tracer`. Real spans, real cost.

    `provider` lets tests inject a `TracerProvider` wired to an in-memory exporter
    instead of the process-global one.
    """

    def __init__(
        self, service_name: str = "chipgraph", *, provider: OtelTracerProvider | None = None
    ) -> None:
        self._provider = provider
        self._tracer = (
            provider.get_tracer(service_name)
            if provider is not None
            else otel_trace.get_tracer(service_name)
        )

    @contextmanager
    def span(self, name: str, **attributes: AttrValue) -> Iterator[SpanHandle]:
        with self._tracer.start_as_current_span(name) as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)
            yield _OtelSpanHandle(span)

    def flush(self) -> None:
        """Flush the provider's span processors, so `console`/`otlp` output is not
        left to the interpreter's exit. Tracing must never break a build."""
        force_flush = getattr(self._provider, "force_flush", None)
        if force_flush is None:
            return
        with suppress(Exception):
            force_flush()


_SDK_HINT = "the OpenTelemetry SDK is not installed; run `pip install chipgraph[otel]`"


def configure(
    exporter: Literal["none", "console", "otlp"],
    *,
    offline: bool = False,
    endpoint: str | None = None,
) -> Tracer:
    """Build the `Tracer` a run should use.

    `exporter="none"` or `offline=True` always returns `NoopTracer`, regardless of the
    other arguments (DESIGN.md 9: `offline` turns every export off). `"console"` prints
    spans to stdout via the SDK's `ConsoleSpanExporter`. `"otlp"` exports over OTLP/HTTP
    to `endpoint` (default: the `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable).
    """
    if offline or exporter == "none":
        return NoopTracer()

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError as exc:
        raise RuntimeError(f"tracing exporter {exporter!r} requires {_SDK_HINT}") from exc

    provider = SdkTracerProvider(resource=Resource.create({"service.name": "chipgraph"}))

    if exporter == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    elif exporter == "otlp":
        try:
            # Only in the `otel` extra, not a hard or dev dependency: not always installed.
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )
        except ImportError as exc:
            raise RuntimeError(f"tracing exporter 'otlp' requires {_SDK_HINT}") from exc
        target = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=target)))
    else:
        raise ValueError(f"unknown exporter {exporter!r}")

    return OtelTracer(provider=provider)


__all__ = [
    "CHECK_ID",
    "CHECK_STATUS",
    "FAILURE_LABEL",
    "GEN_AI_OPERATION_NAME",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_USAGE_INPUT_TOKENS",
    "GEN_AI_USAGE_OUTPUT_TOKENS",
    "RULE_ID",
    "RULE_INSTANCE",
    "RULE_KIND",
    "RUN_ID",
    "SPAN_CHECK",
    "SPAN_EXECUTE",
    "SPAN_INVOKE_AGENT",
    "SPAN_RULE",
    "SPAN_RUN",
    "TARGET",
    "AttrValue",
    "NoopTracer",
    "OtelTracer",
    "SpanHandle",
    "Tracer",
    "configure",
]
