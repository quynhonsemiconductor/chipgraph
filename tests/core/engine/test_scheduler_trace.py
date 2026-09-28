"""Tests for the tracing hooks the scheduler adds around runs, rule instances, checks and
executor calls (M0-15). Reuses the fixtures in `scheduler_fixtures.py`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from scheduler_fixtures import (
    FailingExecutor,
    FakeCheckRunner,
    UppercaseExecutor,
    make_check_result,
)

from chipgraph.core.contracts import InputSpec, RuleSpec, RunSpec
from chipgraph.core.engine.graph import StaticForeach, build_graph
from chipgraph.core.engine.scheduler import Scheduler
from chipgraph.core.state import trace as tracing
from chipgraph.core.state.artifacts import ArtifactStore, LabelRules
from chipgraph.core.state.layout import StateLayout
from chipgraph.core.state.trace import (
    SPAN_CHECK,
    SPAN_EXECUTE,
    SPAN_RULE,
    SPAN_RUN,
    NoopTracer,
    OtelTracer,
)


def _rule(
    id_: str,
    outputs: tuple[str, ...],
    *,
    inputs: tuple[InputSpec, ...] = (),
    checks: tuple[str, ...] = (),
) -> RuleSpec:
    return RuleSpec(
        id=id_,
        kind="gen",
        outputs=outputs,
        inputs=inputs,
        checks=checks,
        run=RunSpec(use="cmd"),
    )


def _traced_scheduler(
    tmp_path: Path, rules: list[RuleSpec], executor: object, *, checks: object | None = None
) -> tuple[Scheduler, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = OtelTracer(provider=provider)

    graph = build_graph(rules, StaticForeach({}))
    store = ArtifactStore(tmp_path, LabelRules())
    layout = StateLayout(tmp_path)
    scheduler = Scheduler(
        graph,
        layout=layout,
        store=store,
        executors={"gen": executor},  # type: ignore[arg-type]
        checks=checks,  # type: ignore[arg-type]
        tracer=tracer,
    )
    return scheduler, exporter


def _spans_named(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [span for span in exporter.get_finished_spans() if span.name == name]


def test_default_scheduler_uses_noop_tracer(tmp_path: Path) -> None:
    graph = build_graph([_rule("p/a", ("a.txt",))], StaticForeach({}))
    store = ArtifactStore(tmp_path, LabelRules())
    layout = StateLayout(tmp_path)
    scheduler = Scheduler(
        graph, layout=layout, store=store, executors={"gen": UppercaseExecutor(tmp_path)}
    )
    assert isinstance(scheduler.tracer, NoopTracer)


def test_run_emits_one_run_span_and_one_rule_span_per_instance(tmp_path: Path) -> None:
    rules = [
        _rule("p/a", ("a.txt",)),
        _rule("p/b", ("b.txt",), inputs=(InputSpec(source="path", selector="a.txt"),)),
    ]
    executor = UppercaseExecutor(tmp_path)
    scheduler, exporter = _traced_scheduler(tmp_path, rules, executor)

    summary = asyncio.run(scheduler.run("*"))
    assert summary.ok

    run_spans = _spans_named(exporter, SPAN_RUN)
    assert len(run_spans) == 1
    assert run_spans[0].attributes is not None
    assert run_spans[0].attributes[tracing.RUN_ID] == summary.run_id
    assert run_spans[0].attributes[tracing.TARGET] == "*"

    rule_spans = _spans_named(exporter, SPAN_RULE)
    assert {s.attributes[tracing.RULE_INSTANCE] for s in rule_spans if s.attributes} == {
        "p/a[]",
        "p/b[]",
    }
    for span in rule_spans:
        assert span.attributes is not None
        assert span.attributes[tracing.RULE_KIND] == "gen"
        assert span.status.status_code != StatusCode.ERROR

    execute_spans = _spans_named(exporter, SPAN_EXECUTE)
    assert len(execute_spans) == 2


def test_checks_get_their_own_span_with_status(tmp_path: Path) -> None:
    rule = _rule("p/a", ("a.txt",), checks=("chk/one",))
    executor = UppercaseExecutor(tmp_path)
    checks = FakeCheckRunner({"chk/one": make_check_result("chk/one", ok=True)})
    scheduler, exporter = _traced_scheduler(tmp_path, [rule], executor, checks=checks)

    summary = asyncio.run(scheduler.run("*"))
    assert summary.ok

    check_spans = _spans_named(exporter, SPAN_CHECK)
    assert len(check_spans) == 1
    assert check_spans[0].attributes is not None
    assert check_spans[0].attributes[tracing.CHECK_ID] == "chk/one"
    assert check_spans[0].attributes[tracing.CHECK_STATUS] == "pass"
    assert check_spans[0].status.status_code != StatusCode.ERROR


def test_failing_instance_marks_its_rule_span_as_error(tmp_path: Path) -> None:
    rule = _rule("p/a", ("a.txt",))
    executor = FailingExecutor(failure_label="verification", message="did not compile")
    scheduler, exporter = _traced_scheduler(tmp_path, [rule], executor)

    summary = asyncio.run(scheduler.run("*"))
    assert summary.failed == ("p/a[]",)

    (rule_span,) = _spans_named(exporter, SPAN_RULE)
    assert rule_span.status.status_code == StatusCode.ERROR
    assert rule_span.status.description == "did not compile"
    assert rule_span.attributes is not None
    assert rule_span.attributes[tracing.FAILURE_LABEL] == "verification"

    (execute_span,) = _spans_named(exporter, SPAN_EXECUTE)
    assert execute_span.status.status_code == StatusCode.ERROR
