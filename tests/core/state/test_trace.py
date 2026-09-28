"""Tests for `chipgraph.core.state.trace`."""

from __future__ import annotations

import builtins
import sys

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from chipgraph.core.state import trace


def _provider_with_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


# --- NoopTracer ------------------------------------------------------------------------


def test_noop_tracer_is_a_working_context_manager() -> None:
    tracer = trace.NoopTracer()
    with tracer.span("some.span", a=1, b="x", c=True) as handle:
        handle.set("k", "v")
        handle.error("boom")
    # nothing to assert: it must simply not raise


def test_configure_none_returns_noop() -> None:
    assert isinstance(trace.configure("none"), trace.NoopTracer)


def test_configure_offline_returns_noop_even_for_console() -> None:
    assert isinstance(trace.configure("console", offline=True), trace.NoopTracer)


def test_configure_offline_returns_noop_even_for_otlp() -> None:
    assert isinstance(trace.configure("otlp", offline=True), trace.NoopTracer)


# --- OtelTracer --------------------------------------------------------------------------


def test_otel_tracer_records_span_name_and_attributes() -> None:
    provider, exporter = _provider_with_exporter()
    tracer = trace.OtelTracer(provider=provider)

    with tracer.span(trace.SPAN_RULE, **{trace.RULE_ID: "p/a", trace.RULE_KIND: "gen"}):
        pass

    (span,) = exporter.get_finished_spans()
    assert span.name == trace.SPAN_RULE
    assert span.attributes is not None
    assert span.attributes[trace.RULE_ID] == "p/a"
    assert span.attributes[trace.RULE_KIND] == "gen"


def test_otel_tracer_set_adds_an_attribute() -> None:
    provider, exporter = _provider_with_exporter()
    tracer = trace.OtelTracer(provider=provider)

    with tracer.span(trace.SPAN_CHECK) as handle:
        handle.set(trace.CHECK_STATUS, "pass")

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes[trace.CHECK_STATUS] == "pass"


def test_otel_tracer_error_sets_error_status() -> None:
    provider, exporter = _provider_with_exporter()
    tracer = trace.OtelTracer(provider=provider)

    with tracer.span(trace.SPAN_RULE) as handle:
        handle.error("it broke")

    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "it broke"


# --- missing SDK ---------------------------------------------------------------------


def test_configure_console_without_sdk_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("opentelemetry.sdk"):
            raise ImportError(f"no module named {name!r} (simulated)")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    for mod in list(sys.modules):
        if mod.startswith("opentelemetry.sdk"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with pytest.raises(RuntimeError, match=r"chipgraph\[otel\]"):
        trace.configure("console")
