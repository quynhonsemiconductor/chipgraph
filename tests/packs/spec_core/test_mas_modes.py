"""Tests for MAS template errors (with exact lines) and REQ-ID inferred mode (D37)."""

from __future__ import annotations

from pathlib import Path

from chipgraph.core.config.models import RequirementsCfg
from chipgraph.packs.spec_core.extract.mas import MasDiagnostic, MasExtractor


def _write(tmp_path: Path, body: str, name: str = "X_MAS.md") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def _errors(diags: tuple[MasDiagnostic, ...]) -> list[MasDiagnostic]:
    return [d for d in diags if d.severity == "error"]


# --- template errors point at the offending line ----------------------------------


def test_bad_direction_points_at_line(tmp_path: Path) -> None:
    body = (
        "# 5. Interface\n"  # 1
        "\n"  # 2
        "| Signal | Dir | Width | Description |\n"  # 3
        "|---|---|---|---|\n"  # 4
        "| `a` | in | 1 | ok |\n"  # 5
        "| `b` | sideways | 1 | bad direction |\n"  # 6
    )
    path = _write(tmp_path, body)
    model, diags = MasExtractor.extract_model(path, block="x")
    errs = _errors(diags)
    assert len(errs) == 1
    assert errs[0].code == "port.direction"
    assert errs[0].line == 6
    # Both ports are still extracted (never raise); the bad one has no direction.
    ports = {p.name: p for p in model.by_kind("port")}
    assert set(ports) == {"a", "b"}
    assert ports["a"].direction == "input"
    assert ports["b"].direction is None


def test_bad_access_points_at_line(tmp_path: Path) -> None:
    body = (
        "# 6. Register map\n"  # 1
        "\n"  # 2
        "| Offset | Register | Access | Reset | Description |\n"  # 3
        "|---|---|---|---|---|\n"  # 4
        "| `0x0` | `CTRL` | RW | 0 | ok |\n"  # 5
        "| `0x4` | `WEIRD` | XYZ | 0 | bad access |\n"  # 6
    )
    path = _write(tmp_path, body)
    _, diags = MasExtractor.extract_model(path, block="x")
    errs = _errors(diags)
    assert len(errs) == 1
    assert errs[0].code == "register.access"
    assert errs[0].line == 6


def test_short_row_points_at_line(tmp_path: Path) -> None:
    body = (
        "# 5. Interface\n"  # 1
        "\n"  # 2
        "| Signal | Dir | Width | Description |\n"  # 3
        "|---|---|---|---|\n"  # 4
        "| `a` | in | 1 |\n"  # 5  (three cells, header has four)
    )
    path = _write(tmp_path, body)
    _, diags = MasExtractor.extract_model(path, block="x")
    errs = _errors(diags)
    assert len(errs) == 1
    assert errs[0].code == "table.row_width"
    assert errs[0].line == 5


def test_duplicate_req_id_cites_both_lines(tmp_path: Path) -> None:
    body = (
        "# 7. Functional behaviour\n"  # 1
        "\n"  # 2
        "`REQ-X-001` first declaration.\n"  # 3
        "\n"  # 4
        "`REQ-X-001` second declaration.\n"  # 5
    )
    path = _write(tmp_path, body)
    cfg = RequirementsCfg(id_pattern="REQ-X-\\d{3}")
    _, diags = MasExtractor.extract_model(path, block="x", requirements=cfg)
    errs = _errors(diags)
    assert len(errs) == 1
    assert errs[0].code == "req.duplicate"
    assert errs[0].line == 5
    assert "line 3" in errs[0].message


def test_duplicate_register_cites_both_lines(tmp_path: Path) -> None:
    body = (
        "# 6. Register map\n"  # 1
        "\n"  # 2
        "| Offset | Register | Access | Reset | Description |\n"  # 3
        "|---|---|---|---|---|\n"  # 4
        "| `0x0` | `CTRL` | RW | 0 | first |\n"  # 5
        "| `0x4` | `CTRL` | RW | 0 | second, duplicate |\n"  # 6
    )
    path = _write(tmp_path, body)
    _, diags = MasExtractor.extract_model(path, block="x")
    errs = _errors(diags)
    assert len(errs) == 1
    assert errs[0].code == "register.duplicate"
    assert errs[0].line == 6
    assert "line 5" in errs[0].message


# --- inferred mode (D37) -----------------------------------------------------------

_INFER_CFG = RequirementsCfg(infer="verification")


def _verification_mas(items: list[str]) -> str:
    lines = ["# 12. Verification", ""]
    for n, text in enumerate(items, start=1):
        lines.append(f"{n}. {text}")
    return "\n".join(lines) + "\n"


def test_inferred_keys_stable_under_renumber(tmp_path: Path) -> None:
    items = [
        "The counter increments once per enabled clock.",
        "A write to COUNT replaces the value.",
        "The interrupt is a level, high until cleared.",
    ]
    a = _write(tmp_path, _verification_mas(items), "A_MAS.md")
    # Same items, renumbered and reordered.
    reordered = _verification_mas([items[2], items[0], items[1]])
    b = _write(tmp_path, reordered, "B_MAS.md")

    model_a, _ = MasExtractor.extract_model(a, block="blk", requirements=_INFER_CFG)
    model_b, _ = MasExtractor.extract_model(b, block="blk", requirements=_INFER_CFG)
    keys_a = {r.key for r in model_a.by_kind("requirement")}
    keys_b = {r.key for r in model_b.by_kind("requirement")}
    assert keys_a == keys_b
    assert len(keys_a) == 3
    assert all(r.attrs["id_source"] == "inferred" for r in model_a.by_kind("requirement"))


def test_inferred_key_changes_when_text_changes(tmp_path: Path) -> None:
    a = _write(tmp_path, _verification_mas(["The counter counts up."]), "A_MAS.md")
    b = _write(tmp_path, _verification_mas(["The counter counts down."]), "B_MAS.md")
    model_a, _ = MasExtractor.extract_model(a, block="blk", requirements=_INFER_CFG)
    model_b, _ = MasExtractor.extract_model(b, block="blk", requirements=_INFER_CFG)
    key_a = model_a.by_kind("requirement")[0].key
    key_b = model_b.by_kind("requirement")[0].key
    assert key_a != key_b


def test_declared_wins_inside_verification(tmp_path: Path) -> None:
    body = _verification_mas(
        [
            "`BLK_001` A declared item wins over inference.",
            "An item with no tag, in a file that declares IDs, is missing its ID.",
        ]
    )
    path = _write(tmp_path, body, "BLK_MAS.md")
    cfg = RequirementsCfg(id_pattern="{BLOCK}_\\d{3}", infer="verification")
    model, _ = MasExtractor.extract_model(path, block="BLK", requirements=cfg)
    reqs = model.by_kind("requirement")
    sources = {r.name: r.attrs["id_source"] for r in reqs}
    assert sources["BLK_001"] == "declared"
    # The declared item is not also inferred; the untagged one is recorded once, as
    # missing its ID (not inferred), because the file declares IDs.
    assert sorted(sources.values()) == ["declared", "missing"]
    assert model.get("requirement:BLK_001") is not None


def test_infer_off_leaves_only_declared(tmp_path: Path) -> None:
    body = _verification_mas(["An item with no tag stays out when infer is off."])
    path = _write(tmp_path, body, "BLK_MAS.md")
    cfg = RequirementsCfg(id_pattern="{BLOCK}_\\d{3}")  # infer defaults to "off"
    model, _ = MasExtractor.extract_model(path, block="BLK", requirements=cfg)
    assert model.by_kind("requirement") == ()


def test_duplicate_port_is_an_error_at_its_line(tmp_path: Path) -> None:
    body = (
        "# 5. Interface\n"  # 1
        "\n"  # 2
        "| Signal | Dir | Width | Description |\n"  # 3
        "|---|---|---|---|\n"  # 4
        "| `a` | in | 1 | first |\n"  # 5
        "| `a` | in | 1 | again |\n"  # 6
    )
    model, diags = MasExtractor.extract_model(_write(tmp_path, body), block="x")
    errs = _errors(diags)
    assert [(e.code, e.line) for e in errs] == [("port.duplicate", 6)]
    assert len(model.by_kind("port")) == 1


def test_inferred_items_with_the_same_text_do_not_raise(tmp_path: Path) -> None:
    body = (
        "# 12. Verification\n"  # 1
        "\n"  # 2
        "1. Reset values match section 6.\n"  # 3
        "2. Reset values match section 6.\n"  # 4
    )
    cfg = RequirementsCfg(infer="verification")
    model, diags = MasExtractor.extract_model(_write(tmp_path, body), block="x", requirements=cfg)
    assert len(model.by_kind("requirement")) == 1
    assert [(d.code, d.line) for d in diags if d.severity == "warning"] == [
        ("req.duplicate_text", 4)
    ]


def test_same_port_in_two_interface_tables_is_not_an_error(tmp_path: Path) -> None:
    body = (
        "# 5. Interface\n"  # 1
        "\n"  # 2
        "| Signal | Dir | Width | Description |\n"  # 3
        "|---|---|---|---|\n"  # 4
        "| `i_clk` | in | 1 | wrapper |\n"  # 5
        "\n"  # 6
        "| Signal | Dir | Width | Description |\n"  # 7
        "|---|---|---|---|\n"  # 8
        "| `i_clk` | in | 1 | core |\n"  # 9
    )
    model, diags = MasExtractor.extract_model(_write(tmp_path, body), block="x")
    assert not _errors(diags)
    assert [(d.code, d.line) for d in diags] == [("port.repeated", 9)]
    (port,) = model.by_kind("port")
    assert port.source.line == 5
