"""Tests for the ``mas-markdown`` extractor over verbatim QSoC MAS fixtures.

The fixtures are copied unchanged from ``quynhonsemiconductor/vlsi_deep_training`` at
commit ``7a917d1`` (public, Apache-2.0), each with a leading provenance comment. Counts
and a few concrete entities are asserted per file. QSoC MAS files carry no ``REQ-...``
IDs today, so these run with ``id_pattern="{BLOCK}_\\d{3}"`` and ``infer="verification"``.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from chipgraph.core.config.models import RequirementsCfg
from chipgraph.packs.spec_core.extract.mas import MasDiagnostic, MasExtractor

_CFG = RequirementsCfg(id_pattern="{BLOCK}_\\d{3}", infer="verification")


def _run(mas_fixtures: Path, name: str, block: str) -> tuple[object, tuple[MasDiagnostic, ...]]:
    path = mas_fixtures / name
    return MasExtractor.extract_model(path, block=block, requirements=_CFG, root=mas_fixtures)


def _counts(model: object) -> dict[str, int]:
    reqs = model.by_kind("requirement")  # type: ignore[attr-defined]
    return {
        "ports": len(model.by_kind("port")),  # type: ignore[attr-defined]
        "registers": len(model.by_kind("register")),  # type: ignore[attr-defined]
        "fields": len(model.by_kind("field")),  # type: ignore[attr-defined]
        "declared": len([r for r in reqs if r.attrs.get("id_source") == "declared"]),
        "inferred": len([r for r in reqs if r.attrs.get("id_source") == "inferred"]),
        "open": len(model.by_kind("open_item")),  # type: ignore[attr-defined]
    }


def _severity(diags: tuple[MasDiagnostic, ...]) -> dict[str, int]:
    return dict(Counter(d.severity for d in diags))


def test_timer_fixture(mas_fixtures: Path) -> None:
    model, diags = _run(mas_fixtures, "QNSC_TIMER_MAS.md", "TIMER")
    counts = _counts(model)
    assert counts == {
        "ports": 13,
        "registers": 10,
        "fields": 14,
        "declared": 0,
        "inferred": 11,
        "open": 2,
    }
    assert _severity(diags).get("error", 0) == 0
    regs = {r.name: r for r in model.by_kind("register")}
    # Field/Bits form, offset ranges reserved, `bits 0-15` non-identifier field.
    assert regs["CFG_REG_LO"].offset == 0
    assert regs["TIMER_START_LO"].access == "wo"
    fields = {f.name for f in model.by_kind("field")}
    assert "ENABLE" in fields and "PRESC" in fields
    # An `Open:` prose line becomes one open item.
    assert any("gate count" in o.name for o in model.by_kind("open_item"))


def test_dma_fixture(mas_fixtures: Path) -> None:
    model, diags = _run(mas_fixtures, "QNSC_DMA_MAS.md", "DMA")
    counts = _counts(model)
    assert counts["declared"] == 13
    assert counts["fields"] == 0  # no Field column in the DMA register table
    assert counts["registers"] == 11
    regs = {r.name: r for r in model.by_kind("register")}
    # `RO, read has effect` -> access `ro`, the rest kept as a note.
    assert regs["NEXT_ID"].access == "ro"
    assert regs["NEXT_ID"].attrs["access_note"] == "read has effect"
    # `DMA_001`-style tags are declared requirements.
    declared = {r.name for r in model.by_kind("requirement")}
    assert "DMA_001" in declared and "DMA_013" in declared
    # Bullet open items under "Open items, DMA owner:".
    assert counts["open"] >= 3
    assert _severity(diags).get("error", 0) == 0


def test_uart_fixture_dlab_table_warns(mas_fixtures: Path) -> None:
    model, diags = _run(mas_fixtures, "QNSC_UART_MAS.md", "UART")
    counts = _counts(model)
    assert counts["declared"] == 9
    assert counts["registers"] == 0  # DLAB bank table is not template form
    assert counts["ports"] == 15
    form_warnings = [d for d in diags if d.code == "register.table_form"]
    assert form_warnings, "the DLAB bank table should warn"
    assert all(d.severity == "warning" for d in form_warnings)
    assert _severity(diags).get("error", 0) == 0


def test_syscsr_fixture_interface_extras(mas_fixtures: Path) -> None:
    model, diags = _run(mas_fixtures, "QNSC_SYSCSR_MAS.md", "SYSCSR")
    counts = _counts(model)
    assert counts["declared"] == 10
    assert counts["registers"] == 3  # no-Field form, extra "Connected to" column ignored
    ports = {p.name for p in model.by_kind("port")}
    # Multi-signal cell split into one port each.
    assert {"i_bus_apb_psel", "i_bus_apb_penable", "i_bus_apb_pwrite"} <= ports
    assert {"o_bus_apb_prdata", "o_bus_apb_pready", "o_bus_apb_pslverr"} <= ports
    regs = {r.name: r for r in model.by_kind("register")}
    assert regs["RESET_CAUSE"].access == "w1c"
    assert regs["CHIP_ID_REV"].reset_value == 0x5153_4F43
    assert _severity(diags).get("error", 0) == 0


def test_scrc_fixture_odd_columns_and_bold_bullets(mas_fixtures: Path) -> None:
    model, diags = _run(mas_fixtures, "QNSC_SCRC_MAS.md", "SCRC")
    counts = _counts(model)
    # The register table has Ibex/MCPU access columns: not template form, no registers.
    assert counts["registers"] == 0
    form_warnings = [d for d in diags if d.code == "register.table_form"]
    assert form_warnings
    # Bold bullet open items ("**APB BUS name clash.** ...").
    opens = model.by_kind("open_item")
    assert any("APB BUS name clash" in o.name for o in opens)
    # SCRC verification uses SCRC_CLK_001-style tags, which do not match SCRC_\d{3}:
    # they are inferred, not declared.
    assert counts["declared"] == 0
    assert counts["inferred"] > 0
    assert _severity(diags).get("error", 0) == 0


def test_interrupt_map_fixture_no_register_table(mas_fixtures: Path) -> None:
    model, diags = _run(mas_fixtures, "QNSC_Interrupt_Map_MAS.md", "Interrupt_Map")
    counts = _counts(model)
    # Section 6 says "None": no register table, and that is not an error.
    assert counts["registers"] == 0
    assert counts["fields"] == 0
    assert _severity(diags).get("error", 0) == 0
    assert counts["ports"] == 14
    assert counts["inferred"] == 10
