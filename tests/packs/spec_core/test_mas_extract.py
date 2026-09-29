"""Tests for the ``mas-markdown`` extractor against the tinysoc sample MAS and units."""

from __future__ import annotations

from pathlib import Path

from chipgraph.adapters.tool.pyslang import PyslangExtractor
from chipgraph.core.config.models import RequirementsCfg
from chipgraph.core.model import DesignModel
from chipgraph.packs.spec_core.extract.mas import MasExtractor, MasTemplate

_TINYSOC = Path(__file__).resolve().parents[3] / "examples" / "tinysoc"


def _model(name: str, block: str, **kw: object) -> tuple[DesignModel, tuple[object, ...]]:
    path = _TINYSOC / "doc" / "specs" / f"{name}_MAS.md"
    return MasExtractor.extract_model(path, block=block, root=_TINYSOC, **kw)  # type: ignore[arg-type]


def test_tinysoc_timer_ports() -> None:
    model, diags = _model("TINY_TIMER", "timer")
    assert not [d for d in diags if d.severity == "error"]
    ports = {p.name: p for p in model.by_kind("port")}
    assert set(ports) == {"clk", "rst_n", "addr", "wr_en", "wdata", "rdata", "irq"}
    assert ports["clk"].direction == "input"
    assert ports["clk"].width == 1
    assert ports["addr"].width == 2
    assert ports["wdata"].direction == "input"
    assert ports["wdata"].width == 32
    assert ports["irq"].direction == "output"
    # Spec ports keep the `spec` part and carry their block/origin.
    assert ports["irq"].key == "port:spec.timer.irq"
    assert ports["irq"].module is None
    assert ports["irq"].attrs["origin"] == "spec"
    assert ports["irq"].attrs["block"] == "block:timer"


def test_tinysoc_timer_registers_and_fields() -> None:
    model, _ = _model("TINY_TIMER", "timer")
    regs = {r.name: r for r in model.by_kind("register")}
    assert set(regs) == {"COUNT", "COMPARE", "CTRL"}
    assert regs["COUNT"].key == "register:timer.COUNT"
    assert regs["COUNT"].offset == 0
    assert regs["CTRL"].offset == 2
    assert regs["COUNT"].access == "rw"
    fields = {f.name: f for f in model.by_kind("field")}
    assert regs["CTRL"].block == "block:timer"
    assert fields["EN"].register == "register:timer.CTRL"
    assert (fields["EN"].msb, fields["EN"].lsb) == (0, 0)
    assert (fields["COUNT"].msb, fields["COUNT"].lsb) == (31, 0)
    assert fields["IRQ_CLR"].access == "w1c"
    # A `contains` relation joins each field to its register.
    contains = model.get_relations(kind="contains", src="register:timer.CTRL")
    dsts = {r.dst for r in contains}
    assert "field:timer.CTRL.EN" in dsts
    assert "field:timer.CTRL.IRQ_CLR" in dsts


def test_tinysoc_timer_requirements_declared() -> None:
    model, diags = _model("TINY_TIMER", "timer")
    assert not [d for d in diags if d.severity == "error"]
    reqs = {r.name: r for r in model.by_kind("requirement")}
    assert set(reqs) == {f"REQ-TIM-00{n}" for n in range(1, 6)}
    r1 = reqs["REQ-TIM-001"]
    assert r1.key == "requirement:REQ-TIM-001"
    assert r1.attrs["id_source"] == "declared"
    assert r1.attrs["block"] == "block:timer"
    assert r1.text is not None and "increments by one every clock" in r1.text
    # The ID token itself is stripped from the requirement text.
    assert not r1.text.startswith("REQ-TIM-001")


def test_tinysoc_timer_provenance_lines() -> None:
    model, _ = _model("TINY_TIMER", "timer")
    clk = model.get("port:spec.timer.clk")
    assert clk is not None
    assert clk.source.file == "doc/specs/TINY_TIMER_MAS.md"
    assert clk.source.line is not None and clk.source.line >= 1
    assert clk.source.extractor == "mas-markdown"
    # The artifact hash is a 64-char sha256 hex digest of the file.
    assert clk.source.artifact is not None and len(clk.source.artifact) == 64


def test_tinysoc_timer_open_items() -> None:
    model, _ = _model("TINY_TIMER", "timer")
    opens = model.by_kind("open_item")
    types = sorted(o.attrs["type"] for o in opens)
    assert types == ["dependency", "open"]
    dep = next(o for o in opens if o.attrs["type"] == "dependency")
    assert dep.status == "open"
    assert dep.attrs["owner"] == "bus owner"
    assert dep.attrs["blocks"] == "register decode"
    prose = next(o for o in opens if o.attrs["type"] == "open")
    assert "interrupt vector" in prose.name


def test_tinysoc_gpio_matches_rtl() -> None:
    model, diags = _model("TINY_GPIO", "gpio")
    assert not [d for d in diags if d.severity == "error"]
    ports = {p.name: p for p in model.by_kind("port")}
    assert ports["pin_in"].direction == "input"
    assert ports["pin_in"].width == 8
    assert ports["pin_out"].direction == "output"
    assert ports["pin_dir"].width == 8
    regs = {r.name: r for r in model.by_kind("register")}
    assert regs["DATA_IN"].access == "ro"
    assert regs["DIR"].offset == 2
    reqs = {r.name for r in model.by_kind("requirement")}
    assert reqs == {f"REQ-GPIO-00{n}" for n in range(1, 4)}


def test_merge_with_pyslang_no_conflict() -> None:
    """The MAS model and the pyslang RTL model of the same block merge cleanly."""
    spec_model, _ = _model("TINY_TIMER", "timer")
    rtl_model, _ = PyslangExtractor.extract_model(
        [_TINYSOC / "rtl" / "tiny_timer.sv"], block="block:timer", root=_TINYSOC
    )
    merged = spec_model.merge(rtl_model)
    # Spec ports (port:spec.timer.*) and RTL ports (port:tiny_timer.*) do not collide.
    assert merged.get("port:spec.timer.irq") is not None
    assert merged.get("port:tiny_timer.irq") is not None


def test_extract_protocol_returns_mappings() -> None:
    """The `Extractor` protocol returns JSON-ready mappings and guesses the block."""
    path = _TINYSOC / "doc" / "specs" / "TINY_TIMER_MAS.md"
    facts = list(MasExtractor().extract(path))
    kinds = {f["kind"] for f in facts}
    assert "requirement" in kinds
    assert "port" in kinds
    # The block is guessed as `tiny_timer` from the file name `TINY_TIMER_MAS.md`.
    ports = [f for f in facts if f["kind"] == "port"]
    assert any(f["key"] == "port:spec.tiny_timer.clk" for f in ports)


def test_custom_template_titles(tmp_path: Path) -> None:
    text = (
        "# 5. Pins\n\n"
        "| Signal | Dir | Width | Description |\n"
        "|---|---|---|---|\n"
        "| `a` | in | 1 | first |\n"
    )
    path = tmp_path / "X_MAS.md"
    path.write_text(text)
    template = MasTemplate(interface="Pins")
    model, _ = MasExtractor.extract_model(path, block="x", template=template)
    assert [p.name for p in model.by_kind("port")] == ["a"]
    # With the default template the "Pins" section is not the interface.
    model2, _ = MasExtractor.extract_model(path, block="x")
    assert model2.by_kind("port") == ()


def test_requirements_config_default_pattern() -> None:
    cfg = RequirementsCfg()
    assert cfg.id_regex().fullmatch("REQ-TIM-004")
    assert not cfg.id_regex().fullmatch("TIM_004")
