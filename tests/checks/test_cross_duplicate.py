"""Tests for `duplicate` (M1-07 part A): duplicate module names and identical RTL files."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from cross_a_helpers import make_instance, rules, run_check, tinysoc_project

from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.checks import DuplicateCheck
from chipgraph.core.state.findings import FindingStore


def test_duplicate_passes_on_tinysoc(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    result = run_check(DuplicateCheck(), root)
    assert result.status == "pass", [i.msg for i in result.issues]


def test_duplicate_module_name_in_a_new_file(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    (root / "rtl" / "tiny_gpio_copy.sv").write_text(
        "module tiny_gpio (input logic clk); endmodule\n", encoding="utf-8"
    )
    result = run_check(DuplicateCheck(), root)
    assert rules(result) == ["duplicate.module"]
    issue = result.issues[0]
    assert issue.file == "rtl/tiny_gpio_copy.sv"
    assert issue.line == 1
    assert "rtl/tiny_gpio.sv:12" in issue.msg


def test_byte_identical_files(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    shutil.copy(root / "rtl" / "tiny_gpio.sv", root / "rtl" / "vendor_copy.sv")
    result = run_check(DuplicateCheck(), root)
    # A byte-identical copy also redeclares the module, so both rules fire.
    assert set(rules(result)) == {"duplicate.file", "duplicate.module"}
    dup_file = next(i for i in result.issues if i.rule == "duplicate.file")
    assert dup_file.file == "rtl/vendor_copy.sv"
    assert "byte-identical to rtl/tiny_gpio.sv" in dup_file.msg


def test_ignore_globs_exclude_vendor(tmp_path: Path) -> None:
    """A duplicate under a `vendor` path is ignored by default."""
    root = tinysoc_project(tmp_path)
    (root / "vendor").mkdir()
    (root / "vendor" / "tiny_gpio.sv").write_text(
        "module tiny_gpio (input logic clk); endmodule\n", encoding="utf-8"
    )
    result = run_check(DuplicateCheck(), root)
    assert result.status == "pass"


def test_scope_glob_can_be_narrowed(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    (root / "rtl" / "tiny_gpio_copy.v").write_text(
        "module tiny_gpio (input logic clk); endmodule\n", encoding="utf-8"
    )
    # Restrict scope to .sv only: the .v duplicate is out of scope, so it passes.
    result = run_check(DuplicateCheck(), root, args={"scope": ["**/*.sv"]})
    assert result.status == "pass"


def test_findings_recorded_at_layer_1(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    (root / "rtl" / "tiny_gpio_copy.sv").write_text(
        "module tiny_gpio (input logic clk); endmodule\n", encoding="utf-8"
    )
    ctx = AppContext.load(root)
    runner = ProfileCheckRunner(ctx)
    result = asyncio.run(runner.run("duplicate", make_instance()))
    assert not result.ok
    findings = FindingStore(ctx.layout).list()
    assert findings
    assert all(f.layer == 1 and f.source == "check:duplicate" for f in findings)
    assert findings[0].evidence[0].file == "rtl/tiny_gpio_copy.sv"
