"""Tests for Verification items missing their ID in a MAS that declares IDs.

In a file with at least one declared requirement ID, each top-level numbered item of the
Verification section that carries no ID is recorded as a requirement with
`attrs.id_source = "missing"` and, when one can be proposed, `attrs.suggested_id` (the
next number of the file's ID family). `spec_schema` turns these into
`requirement.missing_id` (tested in `tests/checks/test_spec_schema_missing_id.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chipgraph.core.config.models import RequirementsCfg
from chipgraph.core.model import DesignModel, RequirementEntity
from chipgraph.packs.spec_core.extract.mas import MasExtractor

_OFF = RequirementsCfg(id_pattern="{BLOCK}_\\d{3}")
_INFER = RequirementsCfg(id_pattern="{BLOCK}_\\d{3}", infer="verification")


def _extract(tmp_path: Path, body: str, cfg: RequirementsCfg, block: str = "DMA") -> DesignModel:
    path = tmp_path / f"QNSC_{block}_MAS.md"
    path.write_text(body, encoding="utf-8")
    model, _ = MasExtractor.extract_model(path, block=block, requirements=cfg, root=tmp_path)
    return model


def _by_source(model: DesignModel, id_source: str) -> list[RequirementEntity]:
    reqs = [r for r in model.by_kind("requirement") if r.attrs.get("id_source") == id_source]
    assert all(isinstance(r, RequirementEntity) for r in reqs)
    return sorted(reqs, key=lambda r: r.source.line or 0)  # type: ignore[return-value]


_ONE_MISSING = (
    "# 12. Verification\n"  # 1
    "\n"  # 2
    "1. `DMA_001` Reset values match section 6.\n"  # 3
    "2. `DMA_002` A 1D job copies `LENGTH` bytes.\n"  # 4
    "3. A 2D job with `REPS` > 1 advances `DONE_ID` once.\n"  # 5  (no ID)
    "4. `DMA_007` Two jobs complete in order.\n"  # 6
)


@pytest.mark.parametrize("cfg", [_OFF, _INFER], ids=["infer-off", "infer-verification"])
def test_one_missing_item_has_its_line_and_the_next_id(
    tmp_path: Path, cfg: RequirementsCfg
) -> None:
    model = _extract(tmp_path, _ONE_MISSING, cfg)
    (missing,) = _by_source(model, "missing")
    assert missing.source.file == "QNSC_DMA_MAS.md"
    assert missing.source.line == 5
    assert missing.attrs["suggested_id"] == "DMA_008"  # max (007) + 1, not a gap
    assert missing.attrs["block"] == "block:dma"
    assert missing.text == "A 2D job with `REPS` > 1 advances `DONE_ID` once."
    assert missing.key.startswith("requirement:dma.h")
    # Emitted once: never also as an inferred requirement.
    assert _by_source(model, "inferred") == []
    assert len(_by_source(model, "declared")) == 3


def test_missing_key_is_stable_under_renumbering(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _extract(tmp_path / "a", _ONE_MISSING, _OFF)
    b = _extract(tmp_path / "b", _ONE_MISSING.replace("3. A 2D job", "7. A 2D job"), _OFF)
    assert _by_source(a, "missing")[0].key == _by_source(b, "missing")[0].key


def test_several_missing_items_get_consecutive_ids_in_line_order(tmp_path: Path) -> None:
    body = (
        "# 12. Verification\n"  # 1
        "\n"  # 2
        "1. First item, no ID.\n"  # 3
        "2. `DMA_009` A declared item.\n"  # 4
        "3. Second item, no ID.\n"  # 5
        "4. `DMA_010` Another declared item.\n"  # 6
        "5. Third item, no ID.\n"  # 7
    )
    model = _extract(tmp_path, body, _OFF)
    got = [(r.source.line, r.attrs["suggested_id"]) for r in _by_source(model, "missing")]
    assert got == [(3, "DMA_011"), (5, "DMA_012"), (7, "DMA_013")]


def test_zero_padding_width_is_kept(tmp_path: Path) -> None:
    body = "# 12. Verification\n\n1. `REQ-DMA-0041` A declared item.\n2. An item with no ID.\n"
    cfg = RequirementsCfg(id_pattern="REQ-{BLOCK}-\\d{4}")
    model = _extract(tmp_path, body, cfg)
    (missing,) = _by_source(model, "missing")
    assert missing.attrs["suggested_id"] == "REQ-DMA-0042"


def test_most_frequent_prefix_family_is_continued(tmp_path: Path) -> None:
    body = (
        "# 12. Verification\n"
        "\n"
        "1. `SCRC_CLK_001` Clock item.\n"
        "2. `SCRC_RST_001` Reset item.\n"
        "3. `SCRC_RST_004` Reset item.\n"
        "4. `SCRC_CLK_009` Clock item, the higher number but the smaller family.\n"
        "5. `SCRC_RST_002` Reset item.\n"
        "6. An item with no ID.\n"
    )
    cfg = RequirementsCfg(id_pattern="{BLOCK}_[A-Z]+_\\d{3}")
    model = _extract(tmp_path, body, cfg, block="SCRC")
    (missing,) = _by_source(model, "missing")
    assert missing.attrs["suggested_id"] == "SCRC_RST_005"


def test_prefix_tie_goes_to_the_higher_number(tmp_path: Path) -> None:
    body = (
        "# 12. Verification\n"
        "\n"
        "1. `SCRC_CLK_003` Clock item.\n"
        "2. `SCRC_RST_008` Reset item.\n"
        "3. An item with no ID.\n"
    )
    cfg = RequirementsCfg(id_pattern="{BLOCK}_[A-Z]+_\\d{3}")
    model = _extract(tmp_path, body, cfg, block="SCRC")
    (missing,) = _by_source(model, "missing")
    assert missing.attrs["suggested_id"] == "SCRC_RST_009"


def test_a_prefix_that_cannot_be_continued_gives_no_hint(tmp_path: Path) -> None:
    body = (
        "# 12. Verification\n"
        "\n"
        "1. `DMA_999` The last three-digit ID.\n"
        "2. An item with no ID.\n"  # DMA_1000 does not match {BLOCK}_\d{3}
    )
    model = _extract(tmp_path, body, _OFF)
    (missing,) = _by_source(model, "missing")
    assert "suggested_id" not in missing.attrs


def test_ids_without_a_trailing_number_give_no_hint(tmp_path: Path) -> None:
    body = (
        "# 12. Verification\n"
        "\n"
        "1. `REQ-DMA-A` A declared item with a letter suffix.\n"
        "2. An item with no ID.\n"
    )
    cfg = RequirementsCfg(id_pattern="REQ-{BLOCK}-[A-Z]")
    model = _extract(tmp_path, body, cfg)
    (missing,) = _by_source(model, "missing")
    assert "suggested_id" not in missing.attrs


_NO_IDS = (
    "# 12. Verification\n\n1. Reset values match section 6.\n2. A 1D job copies `LENGTH` bytes.\n"
)


def test_file_without_declared_ids_and_infer_off_has_no_requirement(tmp_path: Path) -> None:
    model = _extract(tmp_path, _NO_IDS, _OFF)
    assert model.by_kind("requirement") == ()


def test_file_without_declared_ids_keeps_inferred_behaviour(tmp_path: Path) -> None:
    model = _extract(tmp_path, _NO_IDS, _INFER)
    assert _by_source(model, "missing") == []
    inferred = _by_source(model, "inferred")
    assert [r.source.line for r in inferred] == [3, 4]
    assert all("suggested_id" not in r.attrs for r in inferred)


def test_ids_declared_outside_verification_still_count(tmp_path: Path) -> None:
    body = (
        "# 7. Functional behaviour\n"  # 1
        "\n"  # 2
        "`DMA_001` The DMA copies bytes.\n"  # 3
        "\n"  # 4
        "# 12. Verification\n"  # 5
        "\n"  # 6
        "1. An item with no ID.\n"  # 7
    )
    model = _extract(tmp_path, body, _OFF)
    (missing,) = _by_source(model, "missing")
    assert (missing.source.line, missing.attrs["suggested_id"]) == (7, "DMA_002")


def test_all_items_numbered_gives_no_missing(tmp_path: Path) -> None:
    body = (
        "# 12. Verification\n"
        "\n"
        "1. `DMA_001` Reset values match section 6.\n"
        "2. **`DMA_002`** A bold, back-quoted ID still counts.\n"
        "3. DMA_003 A bare ID still counts.\n"
    )
    for cfg in (_OFF, _INFER):
        model = _extract(tmp_path, body, cfg)
        assert _by_source(model, "missing") == []
        assert _by_source(model, "inferred") == []


def test_an_item_naming_an_id_inside_its_text_is_not_missing(tmp_path: Path) -> None:
    """The tinysoc style: `1. Count load (`REQ-TIM-002`): ...` cites the ID mid-text."""
    body = (
        "# 7. Functional behaviour\n"
        "\n"
        "`REQ-TIM-001` The counter counts.\n"
        "\n"
        "`REQ-TIM-002` A write loads it.\n"
        "\n"
        "# 12. Verification\n"
        "\n"
        "1. Counting rate (`REQ-TIM-001`): `COUNT` increments once per cycle.\n"
        "2. Count load (`REQ-TIM-002`): a write to `COUNT` replaces the value.\n"
    )
    cfg = RequirementsCfg(id_pattern="REQ-TIM-\\d{3}")
    model = _extract(tmp_path, body, cfg, block="TIMER")
    assert _by_source(model, "missing") == []
    assert len(_by_source(model, "declared")) == 2


def test_nested_items_are_not_top_level_items(tmp_path: Path) -> None:
    body = (
        "# 12. Verification\n"  # 1
        "\n"  # 2
        "1. `DMA_001` A declared item with steps:\n"  # 3
        "   1. a nested step with no ID\n"  # 4
        "   2. another nested step\n"  # 5
        "   - a nested bullet\n"  # 6
        "2. A top-level item with no ID.\n"  # 7
        "   1. its nested step\n"  # 8
    )
    model = _extract(tmp_path, body, _OFF)
    got = [(r.source.line, r.attrs["suggested_id"]) for r in _by_source(model, "missing")]
    assert got == [(7, "DMA_002")]


def test_other_sections_are_not_checked(tmp_path: Path) -> None:
    body = (
        "# 7. Functional behaviour\n"
        "\n"
        "1. `DMA_001` A declared item.\n"
        "2. A numbered item outside Verification.\n"
        "\n"
        "# 12. Verification\n"
        "\n"
        "1. `DMA_002` A declared item.\n"
    )
    model = _extract(tmp_path, body, _OFF)
    assert _by_source(model, "missing") == []


def test_infer_heading_is_respected(tmp_path: Path) -> None:
    body = (
        "# 12. Verification\n"
        "\n"
        "1. `DMA_001` A declared item.\n"
        "\n"
        "# 13. Checks\n"
        "\n"
        "1. `DMA_002` A declared item.\n"
        "2. An item with no ID under the configured heading.\n"
    )
    cfg = RequirementsCfg(id_pattern="{BLOCK}_\\d{3}", infer_heading="Checks")
    model = _extract(tmp_path, body, cfg)
    (missing,) = _by_source(model, "missing")
    assert missing.attrs["suggested_id"] == "DMA_003"
