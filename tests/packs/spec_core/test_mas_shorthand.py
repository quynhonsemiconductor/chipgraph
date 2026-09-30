"""Tests for the MAS extractor's shorthand signals, wildcards and access modes.

Follow-up to M1-05: shorthand ``_<segment>`` signal cells, ``*`` wildcards, and the
configurable ``access_modes`` list.
"""

from __future__ import annotations

from pathlib import Path

from chipgraph.packs.spec_core.extract.mas import MasExtractor


def _extract(tmp_path: Path, body: str, *, block: str = "x", **kw: object) -> tuple[object, list]:
    path = tmp_path / f"QNSC_{block.upper()}_MAS.md"
    path.write_text(body, encoding="utf-8")
    model, diags = MasExtractor.extract_model(path, block=block, **kw)  # type: ignore[arg-type]
    return model, list(diags)


_INTERFACE_HEADER = "# 5. Interface\n\n| Signal | Dir | Width | Description |\n|---|---|---:|---|\n"


def test_shorthand_axi_row_expands_to_full_names(tmp_path: Path) -> None:
    body = _INTERFACE_HEADER + (
        "| `o_bus_axi_aw_len`, `_size`, `_burst`, `_valid` | out | per-field | AW |\n"
        "| `i_bus_axi_b_id`, `_resp`, `_user` | in | per-field | B |\n"
    )
    model, diags = _extract(tmp_path, body)
    assert not [d for d in diags if d.severity == "error"]
    names = [p.name for p in model.by_kind("port")]  # type: ignore[attr-defined]
    assert names == [
        "o_bus_axi_aw_len",
        "o_bus_axi_aw_size",
        "o_bus_axi_aw_burst",
        "o_bus_axi_aw_valid",
        "i_bus_axi_b_id",
        "i_bus_axi_b_resp",
        "i_bus_axi_b_user",
    ]
    ports = {p.name: p for p in model.by_kind("port")}  # type: ignore[attr-defined]
    # Direction and (string) width are carried across the expanded ports.
    assert ports["o_bus_axi_aw_size"].direction == "output"
    assert ports["o_bus_axi_aw_size"].width == "per-field"
    assert ports["i_bus_axi_b_resp"].direction == "input"


def test_leading_shorthand_first_in_cell_is_error(tmp_path: Path) -> None:
    body = _INTERFACE_HEADER + "| `_size` | out | 1 | no prefix |\n"
    model, diags = _extract(tmp_path, body)
    errors = [d for d in diags if d.severity == "error"]
    assert len(errors) == 1
    assert errors[0].code == "port.shorthand"
    assert errors[0].line == 5  # the offending row's line
    # No port entity is created for the failed shorthand.
    assert model.by_kind("port") == ()  # type: ignore[attr-defined]


def test_same_shorthand_different_prefixes_do_not_collide(tmp_path: Path) -> None:
    body = _INTERFACE_HEADER + (
        "| `o_bus_axi_aw_len`, `_valid` | out | per-field | AW |\n"
        "| `o_bus_axi_ar_len`, `_valid` | out | per-field | AR |\n"
    )
    model, diags = _extract(tmp_path, body)
    assert not [d for d in diags if d.severity == "error"]
    names = {p.name for p in model.by_kind("port")}  # type: ignore[attr-defined]
    assert "o_bus_axi_aw_valid" in names
    assert "o_bus_axi_ar_valid" in names


def test_wildcard_signal_is_info_no_entity(tmp_path: Path) -> None:
    body = _INTERFACE_HEADER + (
        "| `i_bus_axi_ar_*`, `o_bus_axi_r_*` | -- | AXI4 | ref |\n| `i_clk` | in | 1 | clock |\n"
    )
    model, diags = _extract(tmp_path, body)
    assert not [d for d in diags if d.severity == "error"]
    wildcards = [d for d in diags if d.code == "port.wildcard"]
    assert len(wildcards) == 2
    assert all(d.severity == "info" for d in wildcards)
    names = {p.name for p in model.by_kind("port")}  # type: ignore[attr-defined]
    assert names == {"i_clk"}  # the wildcards produced no port


_REG_HEADER = (
    "# 6. Register map\n\n"
    "| Offset | Register | Access | Reset | Description |\n|---|---|---|---|---|\n"
)


def test_default_access_list_rejects_rw1c(tmp_path: Path) -> None:
    body = _REG_HEADER + "| `0x00` | `INTR_STATE` | RW1C | 0 | interrupt |\n"
    model, diags = _extract(tmp_path, body)
    errors = [d for d in diags if d.severity == "error"]
    assert len(errors) == 1
    assert errors[0].code == "register.access"
    assert errors[0].line == 5
    reg = next(iter(model.by_kind("register")))  # type: ignore[attr-defined]
    assert reg.access is None  # rejected mode is not stored


def test_configured_access_list_accepts_rw1c_lowercase(tmp_path: Path) -> None:
    body = _REG_HEADER + (
        "| `0x00` | `INTR_STATE` | RW1C | 0 | interrupt |\n"
        "| `0x04` | `WKUP_CAUSE` | RW0C | 0 | wake |\n"
    )
    modes = ("RW", "RO", "WO", "W1C", "RSVD", "RW0C", "RW1C")
    model, diags = _extract(tmp_path, body, access_modes=modes)
    assert not [d for d in diags if d.severity == "error"]
    regs = {r.name: r for r in model.by_kind("register")}  # type: ignore[attr-defined]
    assert regs["INTR_STATE"].access == "rw1c"  # stored lower-case
    assert regs["WKUP_CAUSE"].access == "rw0c"


def test_access_modes_case_insensitive(tmp_path: Path) -> None:
    body = _REG_HEADER + "| `0x00` | `CTRL` | RW1C | 0 | c |\n"
    # A lower-case configured mode still matches an upper-case cell (and vice versa).
    model, diags = _extract(tmp_path, body, access_modes=("rw1c",))
    assert not [d for d in diags if d.severity == "error"]
    reg = next(iter(model.by_kind("register")))  # type: ignore[attr-defined]
    assert reg.access == "rw1c"
