"""Tests for `CdcStructCheck` (M1-19): structural clock-domain-crossing detection.

Accept criteria:

* tinysoc PASSES (it has a single clock `clk`, so nothing crosses a domain).
* A flop in a second domain `clk_b` sampling a `clk` flop directly is caught
  (`cdc.unsynchronised`) at the destination flop's line.
* The same crossing through a 2-flop synchroniser listed in `sync_cells` PASSES.
* The same crossing through a combinational `assign` is still an error.
* A synchroniser NOT listed in `sync_cells` is an error.
* Pure-SV fixtures exercise the one-level hierarchy case (a crossing wired between two
  sibling instances through the parent, and the same-domain case that must not fire).
* A missing model is a whole-check `error`.
* Findings are stored at layer 1 through `ProfileCheckRunner`.

Each fail case is seeded on a throwaway copy of `examples/tinysoc`, never touching the
source tree.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from cdc_helpers import (
    analyse_fixture,
    assert_findings_layer,
    find_issue,
    make_ctx,
    make_project,
    run_check,
)

from chipgraph.checks import BUILTIN_CHECKS, CdcStructCheck
from chipgraph.checks._cdc_analyze import Kind
from chipgraph.core.contracts import CheckSpec
from chipgraph.core.plugin_api import Registry
from chipgraph.core.plugin_api.protocols import Check

_SYNC_MODULE = (
    "module tiny_sync2 (\n"
    "    input  logic clk,\n"
    "    input  logic d,\n"
    "    output logic q\n"
    ");\n"
    "  logic meta_q;\n"
    "  always_ff @(posedge clk) begin\n"
    "    meta_q <= d;\n"
    "    q      <= meta_q;\n"
    "  end\n"
    "endmodule\n\n"
)


def _add_second_clock(text: str) -> str:
    """Give `tiny_timer` a second clock input `clk_b`."""
    marker = "    input  logic        clk,\n"
    assert marker in text
    return text.replace(marker, marker + "    input  logic        clk_b,\n", 1)


def _insert_decl(text: str, decl: str) -> str:
    marker = "  logic        irq_q;\n"
    assert marker in text
    return text.replace(marker, marker + decl, 1)


def _insert_block(text: str, block: str) -> str:
    marker = "  always_comb begin"
    assert marker in text
    return text.replace(marker, block + "\n" + marker, 1)


def _edit_direct(root: Path) -> None:
    """A `clk_b` flop that samples the `clk`-domain `count_q` directly (a crossing)."""
    path = root / "rtl" / "tiny_timer.sv"
    text = _add_second_clock(path.read_text())
    text = _insert_decl(text, "  logic [31:0] bad_q;\n")
    text = _insert_block(text, "  always_ff @(posedge clk_b) begin\n    bad_q <= count_q;\n  end\n")
    path.write_text(text)


def _edit_through_comb(root: Path) -> None:
    """A crossing where the source passes through a combinational `assign` first."""
    path = root / "rtl" / "tiny_timer.sv"
    text = _add_second_clock(path.read_text())
    text = _insert_decl(text, "  logic [31:0] bad_q;\n  logic [31:0] comb_c;\n")
    text = _insert_block(
        text,
        "  assign comb_c = count_q ^ 32'd0;\n"
        "  always_ff @(posedge clk_b) begin\n    bad_q <= comb_c;\n  end\n",
    )
    path.write_text(text)


def _edit_through_sync(root: Path) -> None:
    """A crossing that goes through a 2-flop synchroniser cell (`tiny_sync2`)."""
    path = root / "rtl" / "tiny_timer.sv"
    text = _add_second_clock(path.read_text())
    text = _insert_decl(text, "  logic        bad_q;\n  logic        synced;\n")
    text = _insert_block(
        text,
        "  tiny_sync2 u_sync (.clk (clk_b), .d (count_q[0]), .q (synced));\n"
        "  always_ff @(posedge clk_b) begin\n    bad_q <= synced;\n  end\n",
    )
    path.write_text(_SYNC_MODULE + text)


# --------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------


def test_is_a_registered_check() -> None:
    assert isinstance(CdcStructCheck(), Check)
    assert BUILTIN_CHECKS["cdc_struct"] is CdcStructCheck
    registry = Registry()
    registry.discover()
    assert "cdc_struct" in registry.names("check")
    assert registry.get("check", "cdc_struct").id == "cdc_struct"


# --------------------------------------------------------------------------------------
# accept: tinysoc passes; seeded crossings on a copy
# --------------------------------------------------------------------------------------


def test_tinysoc_passes(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    for block in ("timer", "gpio", "top"):
        result = run_check(root, block=block)
        assert result.status == "pass", (block, [i.msg for i in result.issues])
        assert all(i.severity != "error" for i in result.issues)


def test_direct_crossing_is_unsynchronised(tmp_path: Path) -> None:
    root = make_project(tmp_path, edit=_edit_direct)
    result = run_check(root, block="timer")
    assert result.status == "fail", [i.msg for i in result.issues]
    issue = find_issue(result, "cdc.unsynchronised")
    assert issue is not None
    assert issue.severity == "error"
    assert issue.file == "rtl/tiny_timer.sv"
    assert issue.line is not None  # the destination flop's line
    text = (root / "rtl" / "tiny_timer.sv").read_text().splitlines()
    assert "bad_q <= count_q" in text[issue.line - 1]


def test_crossing_through_comb_is_still_an_error(tmp_path: Path) -> None:
    root = make_project(tmp_path, edit=_edit_through_comb)
    result = run_check(root, block="timer")
    assert result.status == "fail", [i.msg for i in result.issues]
    issue = find_issue(result, "cdc.unsynchronised")
    assert issue is not None
    assert issue.file == "rtl/tiny_timer.sv"


def test_crossing_through_listed_sync_passes(tmp_path: Path) -> None:
    root = make_project(tmp_path, edit=_edit_through_sync)
    result = run_check(root, block="timer", sync_cells=["tiny_sync2"])
    assert result.status == "pass", [i.msg for i in result.issues if i.severity == "error"]
    assert find_issue(result, "cdc.unsynchronised") is None


def test_unlisted_sync_is_an_error(tmp_path: Path) -> None:
    root = make_project(tmp_path, edit=_edit_through_sync)
    # The same design, but the synchroniser is not declared: it is just another flop, so
    # the crossing surfaces (at the sync cell's output flop).
    result = run_check(root, block="timer", sync_cells=["some_other_cell"])
    assert result.status == "fail", [i.msg for i in result.issues]
    assert find_issue(result, "cdc.unsynchronised") is not None


# --------------------------------------------------------------------------------------
# accept: one-level hierarchy, via pure-SV fixtures
# --------------------------------------------------------------------------------------


def test_hierarchy_sibling_crossing_is_caught() -> None:
    crossings = analyse_fixture("hier_cross.sv")
    bad = [c for c in crossings if c.kind is Kind.UNSYNCHRONISED]
    assert len(bad) == 1, [(c.kind.value, c.dst_name) for c in crossings]
    assert bad[0].dst_domain == "clk_b"
    assert "u_dst" in bad[0].dst_name


def test_hierarchy_same_domain_is_clean() -> None:
    crossings = analyse_fixture("hier_same.sv")
    assert [c for c in crossings if c.kind is Kind.UNSYNCHRONISED] == []


# --------------------------------------------------------------------------------------
# accept: errors and findings wiring
# --------------------------------------------------------------------------------------


def test_missing_model_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    spec = CheckSpec(
        id="cdc_struct",
        capability="cdc_struct",
        adapter="cdc_struct",
        args={"sync_cells": ["tiny_sync2"]},
    )
    result = asyncio.run(CdcStructCheck().run(spec, make_ctx(root, block="timer")))
    assert result.status == "error"
    assert result.issues[0].rule == "model"


def test_missing_sync_cells_arg_is_an_error(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    spec = CheckSpec(id="cdc_struct", capability="cdc_struct", adapter="cdc_struct", args={})
    result = asyncio.run(CdcStructCheck().run(spec, make_ctx(root, block="timer")))
    assert result.status == "error"
    assert result.issues[0].rule == "args"


def test_findings_stored_at_layer_1(tmp_path: Path) -> None:
    root = make_project(tmp_path, edit=_edit_direct)
    assert_findings_layer(root, block="timer", expected_layer=1)
