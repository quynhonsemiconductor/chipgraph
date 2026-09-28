"""Tests for `chipgraph.core.config.templates` (M1-22).

Layer stack used throughout, most specific first: block -> project -> org -> pack.
Fixtures live under `templates_fixtures/{block,project,org,pack,project_whole}/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2.exceptions import SecurityError

from chipgraph.core.config.templates import (
    TemplateLayer,
    TemplateNotFoundError,
    TemplateRenderError,
    TemplateSet,
    TemplateSetError,
)

FIXTURES = Path(__file__).parent / "templates_fixtures"

_MODULE_NAME = "rtl_module.sv.j2"


def _full_stack(*, with_block: bool = True, with_project: bool = True) -> TemplateSet:
    layers = []
    if with_block:
        layers.append(TemplateLayer(name="block", directory=FIXTURES / "block"))
    if with_project:
        layers.append(
            TemplateLayer(
                name="project",
                directory=FIXTURES / "project",
                override={"header": str(FIXTURES / "project" / "header_override.j2")},
            )
        )
    layers.append(TemplateLayer(name="org", directory=FIXTURES / "org"))
    layers.append(TemplateLayer(name="pack", directory=FIXTURES / "pack"))
    return TemplateSet(layers)


def _pack_only() -> TemplateSet:
    return TemplateSet([TemplateLayer(name="pack", directory=FIXTURES / "pack")])


# --------------------------------------------------------------------------------------
# base rendering / snapshot comparisons
# --------------------------------------------------------------------------------------


def test_base_render_uses_pack_defaults_when_nothing_overrides() -> None:
    ts = _pack_only()
    out = ts.render(_MODULE_NAME, {"module_name": "uart_tx"})
    assert "default header" in out
    assert "input  logic clk" in out
    assert "default body" in out
    assert "end of file (pack default)" in out


def test_overriding_header_only_changes_header_region() -> None:
    """Overriding one block must not change any other block's rendered lines."""
    base = _pack_only().render(_MODULE_NAME, {"module_name": "uart_tx"})

    only_header = TemplateSet(
        [
            TemplateLayer(
                name="project",
                override={"header": str(FIXTURES / "project" / "header_override.j2")},
            ),
            TemplateLayer(name="pack", directory=FIXTURES / "pack"),
        ]
    )
    out = only_header.render(_MODULE_NAME, {"module_name": "uart_tx"})

    base_lines = base.splitlines()
    out_lines = out.splitlines()
    assert len(base_lines) == len(out_lines)
    assert "PROJECT header" in out
    assert "default header" not in out

    # Only the header block's own lines may differ; everything else is byte-identical.
    differing = [i for i, (b, o) in enumerate(zip(base_lines, out_lines, strict=True)) if b != o]
    assert differing, "expected the header override to change at least one line"
    for i, (b, o) in enumerate(zip(base_lines, out_lines, strict=True)):
        if i not in differing:
            assert b == o
    # ports/body/footer regions, well away from the header, are untouched.
    assert "  input  logic clk," in out_lines
    assert "  // default body" in out_lines
    assert "// end of file (pack default)" in out_lines


def test_block_layer_override_wins_over_project() -> None:
    ts = _full_stack()
    out = ts.render(_MODULE_NAME, {"module_name": "uart_tx"})
    assert "block_specific_signal" in out
    assert "project_scan_en" not in out


def test_org_layer_used_when_project_has_none() -> None:
    ts = _full_stack()
    out = ts.render(_MODULE_NAME, {"module_name": "uart_tx"})
    assert "end of file (org standard footer)" in out
    assert "end of file (pack default)" not in out


def test_pack_is_the_fallback_for_untouched_blocks() -> None:
    ts = _full_stack()
    out = ts.render(_MODULE_NAME, {"module_name": "uart_tx"})
    # "body" is not overridden anywhere in the full stack.
    assert "default body" in out


def test_full_stack_combines_all_layers_and_only_changes_overridden_regions() -> None:
    ts = _full_stack()
    out = ts.render(_MODULE_NAME, {"module_name": "uart_tx"})
    out_lines = out.splitlines()
    # body (untouched by any override) keeps the pack's exact line.
    assert "  // default body" in out_lines
    assert "PROJECT header" in out
    assert "block_specific_signal" in out
    assert "end of file (org standard footer)" in out


def test_whole_file_replacement_at_project_layer() -> None:
    ts = TemplateSet(
        [
            TemplateLayer(name="project", directory=FIXTURES / "project_whole"),
            TemplateLayer(name="pack", directory=FIXTURES / "pack"),
        ]
    )
    out = ts.render("whole_replace.j2")
    assert out.strip() == "entirely replaced by the project layer, no blocks, no extends"
    assert "pack base" not in out


# --------------------------------------------------------------------------------------
# explain()
# --------------------------------------------------------------------------------------


def test_explain_reports_the_right_layer_per_block() -> None:
    ts = _full_stack()
    explanation = {block: layer for block, layer, _path in ts.explain(_MODULE_NAME)}
    assert explanation == {
        "header": "project",
        "ports": "block",
        "footer": "org",
        "body": "pack",
    }


def test_explain_reports_paths() -> None:
    ts = _full_stack()
    by_block = {block: path for block, _layer, path in ts.explain(_MODULE_NAME)}
    assert by_block["header"] == FIXTURES / "project" / "header_override.j2"
    assert by_block["body"] == FIXTURES / "pack" / _MODULE_NAME


# --------------------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------------------


def test_missing_variable_error_names_template_and_layer() -> None:
    ts = _pack_only()
    with pytest.raises(TemplateRenderError) as excinfo:
        ts.render("needs_var.j2")
    message = str(excinfo.value)
    assert "needs_var.j2" in message
    assert "pack" in message
    assert "required_but_missing" in message


def test_template_not_found_in_any_layer() -> None:
    ts = _pack_only()
    with pytest.raises(TemplateNotFoundError):
        ts.render("does_not_exist.j2")


def test_fragment_without_a_base_layer_raises() -> None:
    ts = TemplateSet([TemplateLayer(name="block", directory=FIXTURES / "block")])
    with pytest.raises(TemplateSetError):
        ts.render(_MODULE_NAME)


def test_sandbox_blocks_dunder_class_tricks() -> None:
    ts = _pack_only()
    with pytest.raises(SecurityError):
        ts.render("sandbox_probe.j2")


# --------------------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------------------


def test_render_is_deterministic() -> None:
    ts = _full_stack()
    first = ts.render(_MODULE_NAME, {"module_name": "spi_core"})
    second = ts.render(_MODULE_NAME, {"module_name": "spi_core"})
    assert first == second
