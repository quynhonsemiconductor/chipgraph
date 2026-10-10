"""M2-06: a module's declaration read with real pyslang, header only, never the body.

On every `examples/tinysoc/rtl/*.sv` (the ports agree with the M1 extractor's), and on
modules with tricky bodies (generate blocks, macros, comments holding tokens, a module
keyword in a comment, `ifdef`s): no body text, comment or line number comes through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chipgraph.adapters.tool.pyslang import PyslangExtractor
from chipgraph.core.model.entities import PortEntity
from chipgraph.packs.dv import declaration as decl_mod
from chipgraph.packs.dv.declaration import (
    MAX_PORTS,
    declaration_in_text,
    declarations_in_text,
    extract_declaration,
)

REPO = Path(__file__).resolve().parents[3]
TINYSOC_RTL = sorted((REPO / "examples" / "tinysoc" / "rtl").glob("*.sv"))

TRICKY = """\
// HEADMARK_FILE_COMMENT before the module
`define WIDTH_M 8
`define BODYMARK_MACRO(x) (x + 1)
module tricky #(
    parameter int WIDTH = `WIDTH_M /* HEADMARK_TRIVIA */,
    parameter type T = logic,
    localparam int LP = 2
) (
    input  wire              clk,      // HEADMARK_TRAILING
    input  logic             rst_n,
`ifdef HEADMARK_DISABLED
    input  logic             headmark_disabled_port,
`endif
    input  logic [WIDTH-1:0] din,
    output logic [3:0][1:0]  dout, dout2,
    input  logic             arr [4]
);
  // BODYMARK_COMMENT: module fake (input BODYMARK_FAKE_PORT); endmodule
  /* BODYMARK_BLOCK_COMMENT
     output logic BODYMARK_IN_COMMENT */
  localparam int BODYMARK_PARAM = `BODYMARK_MACRO(3);
  logic [BODYMARK_PARAM:0] bodymark_signal;
`ifdef BODYMARK_IFDEF
  assign bodymark_signal = '1;
`else
  assign bodymark_signal = '0;
`endif
  generate
    for (genvar g = 0; g < 2; g++) begin : g_bodymark
      logic bodymark_gen;
      always_ff @(posedge clk) bodymark_gen <= din[g];
    end
  endgenerate
  initial $display("BODYMARK_STRING endmodule module x(input y);");
  assign dout = '0;
  assign dout2 = '0;
endmodule

module old_style (a, b, c);
  input  [7:0] a;
  output       b;
  inout  wire  c;
  wire BODYMARK_OLD;
  assign b = ^a;
endmodule
"""


def _extractor_ports(path: Path) -> list[tuple[str, str | None, int | str | None]]:
    model, _ = PyslangExtractor.extract_model([path], root=REPO)
    module = f"module:{path.stem}"
    ports = [p for p in model.by_kind("port") if isinstance(p, PortEntity) and p.module == module]
    ports.sort(key=lambda p: p.source.line or 0)
    return [(p.name, p.direction, p.width) for p in ports]


@pytest.mark.parametrize("path", TINYSOC_RTL, ids=lambda p: p.name)
def test_tinysoc_declarations_match_the_extractor(path: Path) -> None:
    decl = extract_declaration([path], path.stem)
    assert decl is not None and decl.module == path.stem and not decl.truncated
    assert [(p.name, p.direction, p.width) for p in decl.ports] == _extractor_ports(path)


@pytest.mark.parametrize("path", TINYSOC_RTL, ids=lambda p: p.name)
def test_tinysoc_declarations_hold_no_body_text(path: Path) -> None:
    text = path.read_text()
    head, _, body = text.partition(");\n")
    decl = extract_declaration([path], path.stem)
    assert decl is not None
    dumped = json.dumps(decl.model_dump(mode="json"))
    body_words = {w for w in _words(body) if len(w) > 3} - _words(head)
    assert body_words, "the fixture body has words the header does not"
    assert not [w for w in body_words if f'"{w}"' in dumped or w in dumped.split('"')]
    assert "line" not in decl.model_dump() and '"line"' not in dumped


def _words(text: str) -> set[str]:
    import re

    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))


def test_a_tricky_body_never_comes_through() -> None:
    [tricky, old] = declarations_in_text(TRICKY)
    dumped = json.dumps([tricky.model_dump(mode="json"), old.model_dump(mode="json")])
    for marker in ("BODYMARK", "bodymark", "HEADMARK", "headmark", "fake", "always", "assign"):
        assert marker not in dumped, marker
    assert "//" not in dumped and "/*" not in dumped and "`" not in dumped


def test_the_tricky_header_is_read_exactly() -> None:
    decl = declaration_in_text(TRICKY, "tricky")
    assert decl is not None
    assert [(p.name, p.kind, p.type, p.default) for p in decl.parameters] == [
        ("WIDTH", "parameter", "int", "8"),  # the macro, expanded
        ("T", "type", "", "logic"),
        ("LP", "localparam", "int", "2"),
    ]
    ports = {p.name: p for p in decl.ports}
    assert list(ports) == ["clk", "rst_n", "din", "dout", "dout2", "arr"]
    assert (ports["clk"].direction, ports["clk"].type, ports["clk"].width) == ("input", "wire", 1)
    assert (ports["din"].packed, ports["din"].width) == ("[WIDTH-1:0]", None)
    assert (ports["dout"].packed, ports["dout"].width) == ("[3:0][1:0]", 8)
    assert ports["dout2"] == ports["dout"].model_copy(update={"name": "dout2"})
    assert (ports["arr"].unpacked, ports["arr"].width) == ("[4]", 1)


def test_a_non_ansi_header_reads_only_the_port_declarations() -> None:
    decl = declaration_in_text(TRICKY, "old_style")
    assert decl is not None
    assert [(p.name, p.direction, p.packed, p.width) for p in decl.ports] == [
        ("a", "input", "[7:0]", 8),
        ("b", "output", "", 1),
        ("c", "inout", "", 1),
    ]


def test_the_output_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decl_mod, "MAX_PORTS", 2)
    decl = declaration_in_text(TRICKY, "tricky")
    assert decl is not None and decl.truncated and len(decl.ports) == 2
    assert MAX_PORTS >= 256
    long = "module m (input logic [" + "+".join(["1"] * 200) + ":0] a);\nendmodule\n"
    [one] = declarations_in_text(long)
    assert len(one.ports[0].packed) <= decl_mod.MAX_TEXT


def test_extract_declaration_picks_the_file_and_module(tmp_path: Path) -> None:
    other = tmp_path / "other.sv"
    other.write_text("module other (input logic a);\nendmodule\n")
    tricky = tmp_path / "tricky.sv"
    tricky.write_text(TRICKY)
    missing = tmp_path / "missing.sv"
    found = extract_declaration([missing, other, tricky], "old_style")
    assert found is not None and found.module == "old_style"
    only = extract_declaration([other], None)
    assert only is not None and only.module == "other"
    assert extract_declaration([tricky], None) is None  # two modules: which one?
    assert extract_declaration([other], "nowhere") is None


def test_a_broken_file_still_gives_its_header(tmp_path: Path) -> None:
    path = tmp_path / "broken.sv"
    path.write_text(
        '`include "missing_defs.svh"\n'
        "module broken (input logic clk, output logic [1:0] q);\n"
        "  assign q = ;  // BODYMARK_BROKEN\n"
        "endmodule\n"
    )
    decl = extract_declaration([path], "broken")
    assert decl is not None
    assert [(p.name, p.width) for p in decl.ports] == [("clk", 1), ("q", 2)]
    assert "BODYMARK" not in decl.model_dump_json()
