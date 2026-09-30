"""Tests for `cross_chip` (M1-07 part A): chip-wide cross-artifact consistency."""

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
from chipgraph.checks import CrossChipCheck
from chipgraph.core.model.entities import (
    BlockEntity,
    InterruptEntity,
    MemoryRegionEntity,
    PortEntity,
)
from chipgraph.core.model.relations import Relation
from chipgraph.core.state.findings import FindingStore

_GPIO_BLOCK = """  - name: gpio
    bus: regbus
    base: 0x4
    size: 4
    clock: clk
    reset: rst_n"""


def test_cross_chip_passes_on_tinysoc(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    result = run_check(CrossChipCheck(), root)
    assert result.status == "pass", [i.msg for i in result.issues]


def test_errors_when_model_is_missing(tmp_path: Path) -> None:
    (tmp_path / ".chipgraph").mkdir()
    result = run_check(CrossChipCheck(), tmp_path)
    assert result.status == "error"
    assert not result.ok
    assert "ingest" in result.issues[0].msg


def test_address_overlap(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    edit_and_reingest(root, "chip.yml", "base: 0x4", "base: 0x0")
    result = run_check(CrossChipCheck(), root)
    assert "address.overlap" in rules(result)
    overlap = next(i for i in result.issues if i.rule == "address.overlap")
    assert overlap.file == "chip.yml"
    assert overlap.severity == "error"
    assert "memory_region:timer" in overlap.msg


def test_interrupt_line_shared(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    edit_and_reingest(
        root,
        "chip.yml",
        _GPIO_BLOCK,
        _GPIO_BLOCK + "\n    interrupts:\n      - name: irq\n        line: 0",
    )
    result = run_check(CrossChipCheck(), root)
    assert rules(result) == ["interrupt.line_shared"]
    issue = result.issues[0]
    assert issue.file == "chip.yml"
    assert "line 0" in issue.msg


def test_clock_unknown(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    edit_and_reingest(
        root,
        "chip.yml",
        "  - name: timer\n    bus: regbus\n    base: 0x0\n    size: 4\n    clock: clk",
        "  - name: timer\n    bus: regbus\n    base: 0x0\n    size: 4\n    clock: nope_clk",
    )
    result = run_check(CrossChipCheck(), root)
    assert "clock.unknown" in rules(result)
    issue = next(i for i in result.issues if i.rule == "clock.unknown")
    assert "nope_clk" in issue.msg


def _ip_with_interrupt(want_width: int, instance_lines: int) -> list[object]:
    """An IP `foo` whose spec declares `want_width` int lines, with one instance `foo_0`
    the contract gives `instance_lines` sources on one line."""
    return [
        BlockEntity(key="block:foo", name="foo", attrs={"role": "ip"}),
        BlockEntity(key="block:foo_0", name="foo_0"),
        PortEntity(
            key="port:spec.foo.o_int_foo",
            name="o_int_foo",
            direction="output",
            width=want_width,
        ),
        InterruptEntity(
            key="interrupt:foo_0",
            name="foo_0",
            block="block:foo_0",
            line=0,
            attrs={"sources": instance_lines},
        ),
        MemoryRegionEntity(
            key="memory_region:foo_0", name="foo_0", block="block:foo_0", base=0, size=16
        ),
    ]


def test_interrupt_count_mismatch(tmp_path: Path) -> None:
    entities = _ip_with_interrupt(want_width=2, instance_lines=1)
    relations = [Relation(kind="instance_of", src="block:foo_0", dst="block:foo")]
    stage_model(tmp_path, entities, relations)
    result = run_check(CrossChipCheck(), tmp_path)
    assert rules(result) == ["interrupt.count"]
    assert "block:foo_0" in result.issues[0].msg
    assert "declares 2" in result.issues[0].msg


def test_interrupt_count_matches_when_sources_equal_width(tmp_path: Path) -> None:
    entities = _ip_with_interrupt(want_width=4, instance_lines=4)
    relations = [Relation(kind="instance_of", src="block:foo_0", dst="block:foo")]
    stage_model(tmp_path, entities, relations)
    result = run_check(CrossChipCheck(), tmp_path)
    assert result.status == "pass"


def test_interrupt_no_block_suggests_exact_match(tmp_path: Path) -> None:
    entities = [
        BlockEntity(key="block:dma", name="dma", attrs={"role": "ip"}),
        InterruptEntity(key="interrupt:dma", name="dma", line=0),  # no block
        InterruptEntity(key="interrupt:mystery", name="mystery", line=1),  # no block, no match
    ]
    stage_model(tmp_path, entities)
    result = run_check(CrossChipCheck(), tmp_path)
    assert rules(result) == ["interrupt.no_block", "interrupt.no_block"]
    by_key = {i.msg.split("'")[1]: i.msg for i in result.issues}
    assert "block:dma" in by_key["interrupt:dma"]  # suggested
    assert "may belong" not in by_key["interrupt:mystery"]  # not guessed


def test_instance_no_region(tmp_path: Path) -> None:
    """An instance_of declared but with no memory region in the contract."""
    entities = [
        BlockEntity(key="block:timer", name="timer", attrs={"role": "ip"}),
        BlockEntity(key="block:timer_9", name="timer_9"),  # no memory region
    ]
    relations = [Relation(kind="instance_of", src="block:timer_9", dst="block:timer")]
    stage_model(tmp_path, entities, relations)
    result = run_check(CrossChipCheck(), tmp_path)
    assert rules(result) == ["instance.no_region"]
    assert "block:timer_9" in result.issues[0].msg


def test_block_unknown(tmp_path: Path) -> None:
    entities = [
        MemoryRegionEntity(
            key="memory_region:ghost", name="ghost", block="block:ghost", base=0, size=16
        ),
    ]
    stage_model(tmp_path, entities)
    result = run_check(CrossChipCheck(), tmp_path)
    assert rules(result) == ["block.unknown"]
    assert "block:ghost" in result.issues[0].msg


def test_scope_by_block(tmp_path: Path) -> None:
    entities = [
        BlockEntity(key="block:dma", name="dma", attrs={"role": "ip"}),
        InterruptEntity(key="interrupt:dma", name="dma", line=0),
        InterruptEntity(key="interrupt:gpio", name="gpio", line=1),
        BlockEntity(key="block:gpio", name="gpio", attrs={"role": "ip"}),
    ]
    stage_model(tmp_path, entities)
    scoped = run_check(CrossChipCheck(), tmp_path, block="dma")
    assert [i.msg.split("'")[1] for i in scoped.issues] == ["interrupt:dma"]


def test_bad_interrupt_regex_is_error(tmp_path: Path) -> None:
    stage_model(tmp_path, [])
    result = run_check(CrossChipCheck(), tmp_path, args={"interrupt_port_regex": "["})
    assert result.status == "error"


def test_findings_recorded_at_layer_1(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    edit_and_reingest(root, "chip.yml", "base: 0x4", "base: 0x0")
    ctx = AppContext.load(root)
    runner = ProfileCheckRunner(ctx)
    result = asyncio.run(runner.run("cross_chip", make_instance()))
    assert not result.ok
    findings = FindingStore(ctx.layout).list()
    assert findings
    assert all(f.layer == 1 for f in findings)
    assert all(f.source == "check:cross_chip" for f in findings)
