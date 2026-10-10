"""M2-06: `interface_for`, the interface a testbench is written against: the order of its
sources (spec ports, the model's RTL ports, the RTL declaration), the conflict note, and
the spec slice of a block (no module, RTL port or RTL file)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chipgraph.core.model import (
    BlockEntity,
    DesignModel,
    ModuleEntity,
    PortEntity,
    RegisterEntity,
    Relation,
    RequirementEntity,
    ResetEntity,
)
from chipgraph.core.model.entities import FieldEntity, ParameterEntity
from chipgraph.core.model.provenance import Provenance
from chipgraph.packs.dv.declaration import ModuleDeclaration, declaration_in_text
from chipgraph.packs.dv.interface import (
    MISSING_IN_RTL,
    RTL_DIFFERS,
    interface_for,
    project_interface,
    rtl_files,
    spec_slice,
    top_module,
)

SPEC = "doc/specs/GPIO.md"
RTL = "rtl/tiny_gpio.sv"
DECL_TEXT = """\
module tiny_gpio #(parameter int W = 8) (
  input logic clk, input logic rst_n, input logic [1:0] addr,
  output logic [15:0] rdata, input logic zz_decl_only
);
  logic ZZ_BODY;
endmodule
"""


def _spec_port(name: str, direction: str, width: int, line: int) -> PortEntity:
    return PortEntity(
        key=f"port:spec.gpio.{name}",
        name=name,
        direction=direction,  # type: ignore[arg-type]
        width=width,
        source=Provenance(file=SPEC, line=line, extractor="mas-markdown"),
        attrs={"block": "block:gpio", "origin": "spec", "description": f"the {name} port"},
    )


def _rtl_port(name: str, direction: str, width: int, line: int) -> PortEntity:
    return PortEntity(
        key=f"port:tiny_gpio.{name}",
        name=name,
        direction=direction,  # type: ignore[arg-type]
        width=width,
        module="module:tiny_gpio",
        source=Provenance(file=RTL, line=line, extractor="pyslang"),
    )


SPEC_PORTS = [
    _spec_port("clk", "input", 1, 10),
    _spec_port("rst_n", "input", 1, 11),
    _spec_port("addr", "input", 2, 12),
    _spec_port("rdata", "output", 32, 13),
    _spec_port("wr_en", "input", 1, 14),
]
RTL_PORTS = [
    _rtl_port("clk", "input", 1, 3),
    _rtl_port("rst_n", "input", 1, 4),
    _rtl_port("addr", "input", 2, 5),
    _rtl_port("rdata", "output", 16, 6),  # differs from the spec
    _rtl_port("zz_extra", "input", 1, 7),  # not in the spec
]


def _model(*, spec: bool = True, rtl: bool = True, module: bool = True) -> DesignModel:
    entities: list = [
        BlockEntity(key="block:gpio", name="gpio", attrs={"clock": "clk", "reset": "rst_n"}),
        ResetEntity(key="reset:rst_n", name="rst_n", active_low=True),
        RequirementEntity(
            key="requirement:REQ-GPIO-001",
            name="REQ-GPIO-001",
            text="DATA_OUT drives pin_out.",
            source=Provenance(file=SPEC, line=40),
            attrs={"block": "block:gpio", "id_source": "declared"},
        ),
        RegisterEntity(key="register:gpio.DATA", name="DATA", block="block:gpio", offset=0),
        FieldEntity(key="field:gpio.DATA.D", name="D", register="register:gpio.DATA", lsb=0),
    ]
    relations: list[Relation] = []
    if module:
        entities.append(
            ModuleEntity(key="module:tiny_gpio", name="tiny_gpio", file=RTL, block="block:gpio")
        )
        entities.append(
            ParameterEntity(
                key="parameter:tiny_gpio.ZZ_BODY_LP", name="ZZ_BODY_LP", module="module:tiny_gpio"
            )
        )
        relations.append(Relation(kind="contains", src="block:gpio", dst="module:tiny_gpio"))
    if spec:
        entities.extend(SPEC_PORTS)
    if rtl:
        entities.extend(RTL_PORTS)
    return DesignModel.build(entities, relations)


class _Loader:
    def __init__(self, decl: ModuleDeclaration | None) -> None:
        self.decl = decl
        self.calls: list[str | None] = []

    def __call__(self, module: str | None) -> ModuleDeclaration | None:
        self.calls.append(module)
        return self.decl


DECL = declaration_in_text(DECL_TEXT, "tiny_gpio")


def test_1_the_spec_ports_come_first() -> None:
    loader = _Loader(DECL)
    iface = interface_for(None, model=_model(), block="gpio", declaration=loader)
    assert iface.source == "spec" and iface.module == "tiny_gpio"
    assert [p.name for p in iface.ports] == ["clk", "rst_n", "addr", "rdata", "wr_en"]
    assert {p.source for p in iface.ports} == {"spec"}
    assert iface.ports[0].description == "the clk port"
    assert (iface.clock, iface.reset, iface.reset_active_low) == ("clk", "rst_n", True)
    assert loader.calls == []  # the model has the RTL ports: no RTL is read


def test_a_conflict_with_the_model_s_rtl_ports_is_noted_with_the_rtl_value() -> None:
    iface = interface_for("tiny_gpio", model=_model(), declaration=_Loader(DECL))
    ports = {p.name: p for p in iface.ports}
    assert ports["rdata"].width == 32  # the spec's value is the interface
    assert ports["rdata"].note == RTL_DIFFERS
    assert ports["rdata"].rtl == {"direction": "output", "width": 16}
    assert ports["wr_en"].note == MISSING_IN_RTL and ports["wr_en"].rtl is None
    assert ports["clk"].note is None
    assert any("zz_extra" in n and "ports_diff" in n for n in iface.notes)


def test_2_the_model_s_rtl_ports_when_the_spec_has_none() -> None:
    loader = _Loader(DECL)
    iface = interface_for(None, model=_model(spec=False), block="gpio", declaration=loader)
    assert iface.source == "model_rtl"
    assert [(p.name, p.width, p.source) for p in iface.ports] == [
        ("clk", 1, "model_rtl"),
        ("rst_n", 1, "model_rtl"),
        ("addr", 2, "model_rtl"),
        ("rdata", 16, "model_rtl"),
        ("zz_extra", 1, "model_rtl"),
    ]
    assert iface.parameters == ()  # the model's parameters may be body localparams
    assert "ZZ_BODY_LP" not in json.dumps(iface.view())
    assert loader.calls == []


def test_3_the_declaration_when_neither_has_them() -> None:
    loader = _Loader(DECL)
    iface = interface_for(
        None, model=_model(spec=False, rtl=False), block="gpio", declaration=loader
    )
    assert loader.calls == ["tiny_gpio"]
    assert iface.source == "rtl_declaration"
    assert [(p.name, p.width, p.packed) for p in iface.ports] == [
        ("clk", 1, ""),
        ("rst_n", 1, ""),
        ("addr", 2, "[1:0]"),
        ("rdata", 16, "[15:0]"),
        ("zz_decl_only", 1, ""),
    ]
    assert [(p.name, p.default) for p in iface.parameters] == [("W", "8")]
    assert "ZZ_BODY" not in json.dumps(iface.view())


def test_3_without_any_model() -> None:
    iface = interface_for("tiny_gpio", declaration=DECL)
    assert iface.source == "rtl_declaration" and iface.block is None
    assert iface.clock == "clk" and iface.reset == "rst_n" and iface.reset_active_low


def test_a_conflict_with_the_declaration_is_noted_without_the_rtl_value() -> None:
    loader = _Loader(DECL)
    iface = interface_for(None, model=_model(rtl=False), block="gpio", declaration=loader)
    assert loader.calls == ["tiny_gpio"]  # read only to compare
    ports = {p.name: p for p in iface.ports}
    assert iface.source == "spec" and ports["rdata"].width == 32
    assert ports["rdata"].note == RTL_DIFFERS and ports["rdata"].rtl is None
    assert ports["wr_en"].note == MISSING_IN_RTL
    dumped = json.dumps(iface.view())
    assert "16" not in dumped and "[15:0]" not in dumped  # no RTL value
    assert any("1 port(s) the spec does not list" in n for n in iface.notes)
    assert "zz_decl_only" not in dumped


def test_no_interface_at_all_says_so() -> None:
    iface = interface_for("nowhere", model=_model(spec=False, rtl=False, module=False))
    assert iface.source is None and iface.ports == ()
    assert "needs_human" in iface.notes[0]


def test_top_module() -> None:
    assert top_module(_model(), "gpio") == "tiny_gpio"
    assert top_module(_model(module=False), "gpio") is None


def test_the_spec_slice_has_no_rtl() -> None:
    sl = spec_slice(_model(), "gpio")
    assert [r["id"] for r in sl["requirements"]] == ["REQ-GPIO-001"]
    assert sl["requirements"][0]["where"] == f"{SPEC}:40"
    assert sl["registers"][0]["name"] == "DATA" and sl["registers"][0]["fields"][0]["name"] == "D"
    assert (sl["clock"], sl["reset"]) == ("clk", "rst_n")
    dumped = json.dumps(sl)
    for rtl_thing in ("tiny_gpio", "module", RTL, "zz_extra", "ZZ_BODY_LP"):
        assert rtl_thing not in dumped, rtl_thing


def test_project_interface_finds_the_declaration_through_the_filelist(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "tiny_gpio.sv").write_text(DECL_TEXT)
    (tmp_path / "rtl" / "other.sv").write_text("module other (input a);\nendmodule\n")
    (tmp_path / "filelists").mkdir()
    (tmp_path / "filelists" / "gpio.f").write_text("rtl/other.sv\nrtl/tiny_gpio.sv\n")
    layout = {"filelist": "filelists/{block}.f"}
    iface = project_interface(
        tmp_path, model=None, block="gpio", params={"block": "gpio"},
        args={"top": "tiny_{block}"}, layout=layout,
    )  # fmt: skip
    assert iface.source == "rtl_declaration" and iface.module == "tiny_gpio"
    assert "zz_decl_only" in {p.name for p in iface.ports}
    files = rtl_files(
        tmp_path, model=None, module=None, block="gpio", args={"files": ["rtl/*.sv"]}, layout={}
    )
    assert [f.name for f in files] == ["other.sv", "tiny_gpio.sv"]


def test_project_interface_prefers_the_module_param(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "a.sv").write_text(
        "module sub (input logic s);\nendmodule\nmodule tiny_gpio (input logic t);\nendmodule\n"
    )
    iface = project_interface(
        tmp_path,
        model=None,
        block="gpio",
        params={"block": "gpio", "module": "sub"},
        args={"top": "tiny_{block}"},
        layout={"rtl": "rtl/*.sv"},
    )
    assert iface.module == "sub" and [p.name for p in iface.ports] == ["s"]


@pytest.mark.parametrize("missing", [{"filelist": "filelists/none.f"}, {}])
def test_project_interface_without_any_source(tmp_path: Path, missing: dict[str, str]) -> None:
    iface = project_interface(
        tmp_path, model=None, block="gpio", params={}, args={"top": "tiny_{block}"}, layout=missing
    )
    assert iface.source is None and iface.module == "tiny_gpio"
