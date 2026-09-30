"""Tests for `ports_diff` (M1-07 part A): an IP's spec ports vs its top RTL module."""

from __future__ import annotations

import asyncio
from pathlib import Path

from cross_a_helpers import (
    edit_and_reingest,
    make_instance,
    rules,
    run_check,
    stage_model,
    tinysoc_project,
)

from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.checks import PortsDiffCheck
from chipgraph.core.model.entities import ModuleEntity, PortEntity
from chipgraph.core.state.findings import FindingStore

_TOP = {"top": "tiny_{block}"}


def test_ports_diff_passes_on_tinysoc(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    result = run_check(PortsDiffCheck(), root, args=_TOP)
    assert result.status == "pass", [i.msg for i in result.issues]


def test_skips_when_model_is_missing(tmp_path: Path) -> None:
    (tmp_path / ".chipgraph").mkdir()
    result = run_check(PortsDiffCheck(), tmp_path, args=_TOP)
    assert result.status == "skipped"
    assert result.ok


def test_missing_in_spec_when_a_port_is_removed(tmp_path: Path) -> None:
    """Removing `irq` from the timer MAS leaves it in RTL but not in the spec."""
    root = tinysoc_project(tmp_path)
    edit_and_reingest(
        root,
        "doc/specs/TINY_TIMER_MAS.md",
        "| `irq` | out | 1 | level interrupt, high while pending |\n",
        "",
    )
    result = run_check(PortsDiffCheck(), root, args=_TOP)
    assert rules(result) == ["ports_diff.missing_in_spec"]
    issue = result.issues[0]
    assert issue.file == "rtl/tiny_timer.sv"
    assert issue.line == 18
    assert "irq" in issue.msg


def test_width_mismatch(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    edit_and_reingest(
        root,
        "doc/specs/TINY_TIMER_MAS.md",
        "| `addr` | in | 2 | word register address |",
        "| `addr` | in | 4 | word register address |",
    )
    result = run_check(PortsDiffCheck(), root, args=_TOP)
    assert rules(result) == ["ports_diff.width"]
    issue = result.issues[0]
    assert issue.file == "rtl/tiny_timer.sv"
    assert "spec width 4 but RTL width 2" in issue.msg


def test_direction_mismatch(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    edit_and_reingest(
        root,
        "doc/specs/TINY_TIMER_MAS.md",
        "| `rdata` | out | 32 | read data for `addr` |",
        "| `rdata` | in | 32 | read data for `addr` |",
    )
    result = run_check(PortsDiffCheck(), root, args=_TOP)
    assert rules(result) == ["ports_diff.direction"]
    assert "spec direction 'input' but RTL 'output'" in result.issues[0].msg


def test_missing_in_rtl(tmp_path: Path) -> None:
    """A spec port with no matching RTL port is a hand-built model case."""
    entities = [
        ModuleEntity(key="module:tiny_demo", name="tiny_demo", block="block:demo"),
        PortEntity(key="port:spec.demo.extra", name="extra", direction="input", width=1),
        PortEntity(
            key="port:tiny_demo.clk",
            name="clk",
            direction="input",
            width=1,
            module="module:tiny_demo",
        ),
        PortEntity(key="port:spec.demo.clk", name="clk", direction="input", width=1),
    ]
    stage_model(tmp_path, entities)
    result = run_check(PortsDiffCheck(), tmp_path, args={"top": "tiny_{block}"})
    assert rules(result) == ["ports_diff.missing_in_rtl"]
    assert "extra" in result.issues[0].msg


def test_no_top_is_info_not_failure(tmp_path: Path) -> None:
    """A block with a spec port but no identifiable top module gets one `info`, passes."""
    entities = [
        PortEntity(key="port:spec.foo.clk", name="clk", direction="input", width=1),
    ]
    stage_model(tmp_path, entities)
    result = run_check(PortsDiffCheck(), tmp_path)  # no `top` template
    assert result.status == "pass"
    assert rules(result) == ["ports_diff.no_top"]
    assert result.issues[0].severity == "info"


def test_scope_by_block(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    edit_and_reingest(
        root,
        "doc/specs/TINY_TIMER_MAS.md",
        "| `addr` | in | 2 | word register address |",
        "| `addr` | in | 4 | word register address |",
    )
    scoped_gpio = run_check(PortsDiffCheck(), root, args=_TOP, block="gpio")
    assert scoped_gpio.status == "pass"
    scoped_timer = run_check(PortsDiffCheck(), root, args=_TOP, block="timer")
    assert rules(scoped_timer) == ["ports_diff.width"]


def test_findings_recorded_at_layer_1(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    edit_and_reingest(
        root,
        "doc/specs/TINY_TIMER_MAS.md",
        "| `addr` | in | 2 | word register address |",
        "| `addr` | in | 4 | word register address |",
    )
    # Point the profile's ports_diff at the right top template (tinysoc already does).
    ctx = AppContext.load(root)
    runner = ProfileCheckRunner(ctx)
    result = asyncio.run(runner.run("ports_diff", make_instance(block="timer")))
    assert not result.ok
    findings = FindingStore(ctx.layout).list()
    assert findings
    assert all(f.layer == 1 and f.source == "check:ports_diff" for f in findings)
