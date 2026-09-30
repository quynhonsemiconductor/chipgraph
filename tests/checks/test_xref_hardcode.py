"""Tests for `HardcodeCheck` (M1-07 part B): no RTL literal duplicating a contract number.

Accept criteria (seeded on a throwaway copy of `examples/tinysoc`):

* tinysoc PASSES: its addresses (0x0/0x4) are below the default `min_value`, so no RTL
  literal is flagged.
* `hardcode.literal`: a base address of 0x4000 in `chip.yml` plus `32'h4000` typed in RTL
  is caught, at the literal's line, naming the memory-region key.
* the same literal with a `// contract:` comment on its line passes.
* end address and size are shared numbers too.
* an interrupt line counts only as an index on the interrupt vector net.
* a missing model is a whole-check `error`.
* findings are stored at layer 1 through `ProfileCheckRunner`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from xref_b_helpers import (
    assert_findings_layer,
    find_issue,
    make_ctx,
    make_project,
    replace_in_file,
    run_check,
)

from chipgraph.checks import HardcodeCheck
from chipgraph.core.contracts import CheckSpec
from chipgraph.core.plugin_api import Registry
from chipgraph.core.plugin_api.protocols import Check

_SCOPE = {"scope": ["rtl/**/*.sv"]}


def test_is_a_registered_check() -> None:
    assert isinstance(HardcodeCheck(), Check)
    registry = Registry()
    registry.discover()
    assert "hardcode" in registry.names("check")
    assert registry.get("check", "hardcode").id == "hardcode"


def test_tinysoc_passes(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    result = run_check(HardcodeCheck(), _SCOPE, make_ctx(root))
    assert result.status == "pass", [i.msg for i in result.issues]
    assert result.issues == ()


def _seed_large_base(root: Path, literal: str) -> None:
    """Give the timer a 0x4000 base and type `literal` for its counter reset in RTL."""
    replace_in_file(root, "chip.yml", "base: 0x0", "base: 0x4000")
    replace_in_file(root, "rtl/tiny_timer.sv", "count_q   <= 32'd0;", f"count_q   <= {literal};")


def test_hardcoded_base_address_fails(tmp_path: Path) -> None:
    root = make_project(tmp_path, edit=lambda r: _seed_large_base(r, "32'h4000"))
    result = run_check(HardcodeCheck(), _SCOPE, make_ctx(root))
    assert result.status == "fail"
    issue = find_issue(result, "hardcode.literal")
    assert issue is not None
    assert issue.file == "rtl/tiny_timer.sv"
    assert issue.line is not None
    assert "memory_region:timer.base" in issue.msg


def test_contract_comment_exempts_the_line(tmp_path: Path) -> None:
    def edit(root: Path) -> None:
        replace_in_file(root, "chip.yml", "base: 0x0", "base: 0x4000")
        replace_in_file(
            root,
            "rtl/tiny_timer.sv",
            "count_q   <= 32'd0;",
            "count_q   <= 32'h4000;  // contract: memory_region:timer.base",
        )

    root = make_project(tmp_path, edit=edit)
    result = run_check(HardcodeCheck(), _SCOPE, make_ctx(root))
    assert result.status == "pass", [i.msg for i in result.issues]


def test_end_address_is_a_shared_number(tmp_path: Path) -> None:
    # base 0x4000, size 4 -> end 0x4004. Type the end address in RTL.
    root = make_project(tmp_path, edit=lambda r: _seed_large_base(r, "32'h4004"))
    result = run_check(HardcodeCheck(), _SCOPE, make_ctx(root))
    assert result.status == "fail"
    issue = find_issue(result, "hardcode.literal")
    assert issue is not None
    assert "memory_region:timer.end" in issue.msg


def test_min_value_can_be_raised_to_ignore_a_number(tmp_path: Path) -> None:
    root = make_project(tmp_path, edit=lambda r: _seed_large_base(r, "32'h4000"))
    # 0x4000 == 16384; a min_value above it drops the shared number entirely.
    result = run_check(HardcodeCheck(), {**_SCOPE, "min_value": 0x8000}, make_ctx(root))
    assert result.status == "pass", [i.msg for i in result.issues]


def test_interrupt_line_only_counts_as_a_vector_index(tmp_path: Path) -> None:
    # tinysoc's single interrupt is line 0. A bare 0 in RTL (there are many) is NOT a
    # hardcode; only `o_int_vec[0]` would be. With no such index, nothing fires.
    root = make_project(tmp_path)
    result = run_check(HardcodeCheck(), {**_SCOPE, "irq_vector": "o_int_vec"}, make_ctx(root))
    assert result.status == "pass", [i.msg for i in result.issues]


def test_interrupt_vector_index_is_flagged(tmp_path: Path) -> None:
    def edit(root: Path) -> None:
        # Add a top-level vector net and index it by the timer's line (0) with a literal.
        path = root / "rtl" / "tiny_top.sv"
        text = path.read_text()
        inject = "  logic [3:0] o_int_vec;\n  assign o_int_vec[0] = timer_irq;\n"
        path.write_text(text.replace("  assign block_sel", inject + "  assign block_sel", 1))

    root = make_project(tmp_path, edit=edit)
    result = run_check(
        HardcodeCheck(), {"scope": ["rtl/**/*.sv"], "irq_vector": "o_int_vec"}, make_ctx(root)
    )
    assert result.status == "fail"
    issue = find_issue(result, "hardcode.literal")
    assert issue is not None
    assert issue.file == "rtl/tiny_top.sv"
    assert "interrupt:timer.irq" in issue.msg


def test_generated_file_is_skipped(tmp_path: Path) -> None:
    root = make_project(tmp_path, edit=lambda r: _seed_large_base(r, "32'h4000"))
    result = run_check(
        HardcodeCheck(),
        {**_SCOPE, "generated": ["rtl/tiny_timer.sv"]},
        make_ctx(root),
    )
    assert result.status == "pass", [i.msg for i in result.issues]


def test_missing_model_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    spec = CheckSpec(id="hardcode", capability="hardcode", adapter="hardcode", args=_SCOPE)
    result = asyncio.run(HardcodeCheck().run(spec, make_ctx(root)))
    assert result.status == "error"
    assert result.issues[0].rule == "model"


def test_findings_stored_at_layer_1(tmp_path: Path) -> None:
    root = make_project(tmp_path, edit=lambda r: _seed_large_base(r, "32'h4000"))
    assert_findings_layer(root, "hardcode", expected_layer=1)
