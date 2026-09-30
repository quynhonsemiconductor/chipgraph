"""Tests for `spec_schema` (M1-07 part A): the spec side of the Design Model."""

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
from chipgraph.checks import SpecSchemaCheck
from chipgraph.core.model.entities import FieldEntity, RegisterEntity
from chipgraph.core.state.findings import FindingStore


def test_spec_schema_passes_on_tinysoc(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    result = run_check(SpecSchemaCheck(), root)
    assert result.status == "pass", [i.msg for i in result.issues]


def test_errors_when_model_is_missing(tmp_path: Path) -> None:
    (tmp_path / ".chipgraph").mkdir()
    result = run_check(SpecSchemaCheck(), tmp_path)
    assert result.status == "error"
    assert not result.ok
    assert "ingest" in result.issues[0].msg


def test_register_without_access_fails(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    edit_and_reingest(
        root,
        "doc/specs/TINY_TIMER_MAS.md",
        "| `0x0` | `COUNT` | `COUNT` | 31:0 | RW | 0 | free-running counter; a write loads it |",
        "| `0x0` | `COUNT` | `COUNT` | 31:0 |  | 0 | free-running counter; a write loads it |",
    )
    result = run_check(SpecSchemaCheck(), root)
    assert result.status == "fail"
    assert rules(result) == ["register.no_access"]
    issue = result.issues[0]
    assert issue.file == "doc/specs/TINY_TIMER_MAS.md"
    assert issue.line == 59
    assert "register:timer.COUNT" in issue.msg


def test_field_out_of_range(tmp_path: Path) -> None:
    """A field msb past the register's (default 32-bit) width is out of range."""
    reg = RegisterEntity(
        key="register:demo.R", name="R", block="block:demo", access="rw", reset_value=0
    )
    field = FieldEntity(key="field:demo.R.F", name="F", register="register:demo.R", lsb=0, msb=40)
    stage_model(tmp_path, [reg, field])
    result = run_check(SpecSchemaCheck(), tmp_path)
    assert rules(result) == ["field.out_of_range"]


def test_field_overlap(tmp_path: Path) -> None:
    reg = RegisterEntity(
        key="register:demo.R", name="R", block="block:demo", access="rw", reset_value=0
    )
    f1 = FieldEntity(key="field:demo.R.A", name="A", register="register:demo.R", lsb=0, msb=3)
    f2 = FieldEntity(key="field:demo.R.B", name="B", register="register:demo.R", lsb=2, msb=5)
    stage_model(tmp_path, [reg, f1, f2])
    result = run_check(SpecSchemaCheck(), tmp_path)
    assert rules(result) == ["field.overlap"]
    assert "field:demo.R.B" in result.issues[0].msg


def test_field_msb_lt_lsb(tmp_path: Path) -> None:
    reg = RegisterEntity(
        key="register:demo.R", name="R", block="block:demo", access="rw", reset_value=0
    )
    field = FieldEntity(key="field:demo.R.F", name="F", register="register:demo.R", lsb=5, msb=2)
    stage_model(tmp_path, [reg, field])
    result = run_check(SpecSchemaCheck(), tmp_path)
    assert rules(result) == ["field.msb_lt_lsb"]


def test_scope_by_block_limits_to_that_ip(tmp_path: Path) -> None:
    """A `--block gpio` run reports gpio's issue, not timer's."""
    timer_reg = RegisterEntity(
        key="register:timer.T", name="T", block="block:timer", reset_value=0
    )  # no access
    gpio_reg = RegisterEntity(
        key="register:gpio.G", name="G", block="block:gpio", reset_value=0
    )  # no access
    stage_model(tmp_path, [timer_reg, gpio_reg])
    scoped = run_check(SpecSchemaCheck(), tmp_path, block="gpio")
    assert [i.msg for i in scoped.issues] == ["register 'register:gpio.G' has no access mode"]
    unscoped = run_check(SpecSchemaCheck(), tmp_path)
    assert len(unscoped.issues) == 2


def test_findings_recorded_at_layer_1(tmp_path: Path) -> None:
    """A failing `spec_schema` issue is stored as a layer-1 finding via ProfileCheckRunner."""
    root = tinysoc_project(tmp_path)
    edit_and_reingest(
        root,
        "doc/specs/TINY_TIMER_MAS.md",
        "| `0x0` | `COUNT` | `COUNT` | 31:0 | RW | 0 | free-running counter; a write loads it |",
        "| `0x0` | `COUNT` | `COUNT` | 31:0 |  | 0 | free-running counter; a write loads it |",
    )
    ctx = AppContext.load(root)
    runner = ProfileCheckRunner(ctx)

    result = asyncio.run(runner.run("spec_schema", make_instance()))
    assert not result.ok

    findings = FindingStore(ctx.layout).list()
    assert len(findings) == 1
    finding = findings[0]
    assert finding.layer == 1
    assert finding.source == "check:spec_schema"
    assert finding.evidence[0].file == "doc/specs/TINY_TIMER_MAS.md"
    assert finding.evidence[0].line == 59
