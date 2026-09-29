"""Tests for the ``diagram`` generator (pack ``spec-core``), task M1-09.

The snapshot tests compare the rendered figures for two model sources (the ``tiny_chip``
chip-yaml fixture and the QSoC contract fixture) against committed byte-exact snapshots in
``diagram_snapshots/``. Regenerate the snapshots after an intentional change with::

    UPDATE_SNAPSHOTS=1 uv run pytest tests/packs/spec_core/test_diagram.py

and review the diff before committing.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from chipgraph.adapters.format.chip_yaml import ChipYamlAdapter
from chipgraph.adapters.format.qsoc_contract import QSocContractAdapter
from chipgraph.core.model import (
    BlockEntity,
    ClockEntity,
    DesignModel,
    InterfaceEntity,
    InterruptEntity,
    MemoryRegionEntity,
    ModelStore,
    ProjectEntity,
    Relation,
    ResetEntity,
    default_model_db_path,
    make_key,
)
from chipgraph.core.plugin_api.registry import Registry
from chipgraph.packs.spec_core.gen.diagram import (
    DiagramGenerator,
    facts_to_model,
    render,
)
from chipgraph.packs.spec_core.gen.diagram.__main__ import main

_FIXTURES = Path(__file__).parent.parent.parent / "adapters" / "format" / "fixtures"
_SNAPSHOTS = Path(__file__).parent / "diagram_snapshots"
_FILE_NAMES = (
    "block_diagram.svg",
    "block_diagram.drawio",
    "memory_map.svg",
    "memory_map.drawio",
    "interrupt_map.svg",
    "interrupt_map.drawio",
    "clock_reset_tree.svg",
    "clock_reset_tree.drawio",
)


def _tiny_chip_model() -> DesignModel:
    model, _ = ChipYamlAdapter().load_model(_FIXTURES / "tiny_chip.yml")
    return model


def _qsoc_model() -> DesignModel:
    model, _ = QSocContractAdapter().load_model(_FIXTURES / "qsoc_contract.yml")
    return model


def _model_facts(model: DesignModel) -> list[dict[str, object]]:
    facts: list[dict[str, object]] = [e.model_dump(mode="json") for e in model.entities.values()]
    facts.extend(r.model_dump(mode="json") for r in model.relations)
    return facts


@pytest.mark.parametrize("source", ["tiny_chip", "qsoc"])
def test_snapshot_byte_identical(source: str) -> None:
    """Both fixtures render all 8 files byte-identical to the committed snapshots."""
    model = _tiny_chip_model() if source == "tiny_chip" else _qsoc_model()
    files = render(model)
    assert sorted(files) == sorted(_FILE_NAMES)
    snapshot_dir = _SNAPSHOTS / source
    if os.environ.get("UPDATE_SNAPSHOTS"):
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (snapshot_dir / name).write_bytes(data)
    for name, data in files.items():
        expected = (snapshot_dir / name).read_bytes()
        assert data == expected, f"{source}/{name} drifted from its snapshot"


@pytest.mark.parametrize("source", ["tiny_chip", "qsoc"])
def test_deterministic_across_shuffled_entities(source: str) -> None:
    """A model built twice, with entities in reversed order, renders identical bytes."""
    model = _tiny_chip_model() if source == "tiny_chip" else _qsoc_model()
    entities = list(model.entities.values())
    reshuffled = DesignModel.build(reversed(entities), model.relations)
    assert render(reshuffled) == render(model)


@pytest.mark.parametrize("source", ["tiny_chip", "qsoc"])
def test_all_outputs_well_formed_xml(source: str) -> None:
    """Every SVG and drawio output parses as XML; every SVG has a title and desc."""
    model = _tiny_chip_model() if source == "tiny_chip" else _qsoc_model()
    for name, data in render(model).items():
        root = ET.fromstring(data)
        if name.endswith(".svg"):
            ns = "{http://www.w3.org/2000/svg}"
            assert root.find(f"{ns}title") is not None, f"{name} missing <title>"
            assert root.find(f"{ns}desc") is not None, f"{name} missing <desc>"
            texts = root.findall(f".//{ns}text")
            assert texts, f"{name} has no real <text> elements"


def test_svg_text_is_readable_font_size() -> None:
    """SVG uses a readable font size on the root, not tiny or path-drawn text."""
    files = render(_tiny_chip_model())
    svg = files["block_diagram.svg"].decode("utf-8")
    assert 'font-size="12"' in svg
    # Text is real <text>, not vector paths: the only <path> is the arrow marker glyph.
    assert svg.count("<path") == 1


def test_overlap_is_marked() -> None:
    """Two overlapping regions get the overlap marker in both SVG and drawio."""
    project = ProjectEntity(key=make_key("project", "p"), name="p")
    block_a = BlockEntity(key=make_key("block", "a"), name="a")
    block_b = BlockEntity(key=make_key("block", "b"), name="b")
    region_a = MemoryRegionEntity(
        key=make_key("memory_region", "a"), name="a", base=0x0, size=0x2000, block=block_a.key
    )
    region_b = MemoryRegionEntity(
        key=make_key("memory_region", "b"), name="b", base=0x1000, size=0x2000, block=block_b.key
    )
    model = DesignModel.build([project, block_a, block_b, region_a, region_b])
    files = render(model)
    assert "#cc0000" in files["memory_map.svg"].decode("utf-8")
    assert "#cc0000" in files["memory_map.drawio"].decode("utf-8")
    assert "[OVERLAP]" in files["memory_map.svg"].decode("utf-8")


def test_unassigned_interrupts_listed() -> None:
    """Interrupts with no line or no block appear under an 'unassigned' group."""
    project = ProjectEntity(key=make_key("project", "p"), name="p")
    block = BlockEntity(key=make_key("block", "t"), name="t")
    assigned = InterruptEntity(
        key=make_key("interrupt", "t", "ovf"), name="ovf", line=3, block=block.key
    )
    no_line = InterruptEntity(key=make_key("interrupt", "t", "err"), name="err", block=block.key)
    no_block = InterruptEntity(key=make_key("interrupt", "x", "spurious"), name="spurious", line=9)
    model = DesignModel.build([project, block, assigned, no_line, no_block])
    svg = render(model)["interrupt_map.svg"].decode("utf-8")
    assert "unassigned" in svg
    assert "err" in svg
    assert "spurious" in svg
    assert "line 3: ovf" in svg


def test_shared_interrupt_line_is_marked() -> None:
    """Two sources on one line are marked in the marker colour, in both output formats."""
    block = BlockEntity(key=make_key("block", "t"), name="t")
    first = InterruptEntity(key=make_key("interrupt", "t", "a"), name="a", line=4, block=block.key)
    second = InterruptEntity(key=make_key("interrupt", "t", "b"), name="b", line=4, block=block.key)
    alone = InterruptEntity(key=make_key("interrupt", "t", "c"), name="c", line=5, block=block.key)
    files = render(DesignModel.build([block, first, second, alone]), kinds=("interrupts",))
    svg = files["interrupt_map.svg"].decode("utf-8")
    assert svg.count("[line shared]") == 2
    assert svg.count("#cc0000") == 2
    assert "line 5: c  (block t)<" in svg
    assert b"#cc0000" in files["interrupt_map.drawio"]


def test_qsoc_real_data_unassigned_interrupts() -> None:
    """The real QSoC contract has unassigned interrupts named by peripheral."""
    svg = render(_qsoc_model())["interrupt_map.svg"].decode("utf-8")
    assert "unassigned" in svg
    for name in ("dma", "gpio", "spi_device", "spi_host", "wdt_wakeup"):
        assert name in svg


def test_escaping_produces_valid_xml() -> None:
    """A block named 'a&b<c>' still renders well-formed XML with the escaped name."""
    project = ProjectEntity(key=make_key("project", "p"), name="p")
    block = BlockEntity(key=make_key("block", "weird"), name="a&b<c>")
    bus = InterfaceEntity(
        key=make_key("interface", "bus"),
        name="a&b<c>",
        protocol="APB",
        attrs={"masters": ["m<x>"], "slaves": ["weird"]},
    )
    rel = Relation(kind="connects", src=bus.key, dst=block.key)
    model = DesignModel.build([project, block, bus], [rel])
    files = render(model)
    for data in files.values():
        ET.fromstring(data)  # raises on malformed XML
    assert "a&amp;b&lt;c&gt;" in files["block_diagram.svg"].decode("utf-8")


def test_empty_model_still_valid_files() -> None:
    """An empty model yields 8 valid files that say 'no data'."""
    files = render(DesignModel.build([], []))
    assert sorted(files) == sorted(_FILE_NAMES)
    for name, data in files.items():
        ET.fromstring(data)
        if name.endswith(".svg"):
            assert "no data" in data.decode("utf-8")


def test_model_without_regions_or_interrupts() -> None:
    """A model with only clocks/resets renders memory-map and interrupt-map as 'no data'."""
    clock = ClockEntity(key=make_key("clock", "c"), name="c")
    reset = ResetEntity(key=make_key("reset", "r"), name="r")
    files = render(DesignModel.build([clock, reset]))
    assert "no data" in files["memory_map.svg"].decode("utf-8")
    assert "no data" in files["interrupt_map.svg"].decode("utf-8")
    assert "no data" not in files["clock_reset_tree.svg"].decode("utf-8")


def test_render_unknown_kind_raises() -> None:
    """An unknown diagram kind is rejected."""
    with pytest.raises(ValueError, match="unknown diagram kind"):
        render(DesignModel.build([]), kinds=("nope",))


def test_generator_registered_and_generates(tmp_path: Path) -> None:
    """The generator is discoverable and writes the files, returning sorted paths."""
    registry = Registry()
    registry.discover()
    generator = registry.get("generator", "diagram")
    assert generator.name == "diagram"
    facts = _model_facts(_tiny_chip_model())
    paths = generator.generate(tmp_path, facts)
    assert paths == tuple(sorted(paths))
    assert {p.name for p in paths} == set(_FILE_NAMES)
    for path in paths:
        assert path.read_bytes()


def test_generate_matches_render_from_facts(tmp_path: Path) -> None:
    """generate() from facts writes exactly what render() produces for the same model."""
    model = _qsoc_model()
    facts = _model_facts(model)
    assert render(facts_to_model(facts)) == render(model)
    DiagramGenerator().generate(tmp_path, facts)
    expected = render(model)
    for name, data in expected.items():
        assert (tmp_path / name).read_bytes() == data


def test_main_writes_from_store(tmp_path: Path) -> None:
    """__main__ against a store written in tmp_path writes the files and exits 0."""
    model = _tiny_chip_model()
    db_path = default_model_db_path(tmp_path)
    ModelStore(db_path).write(model)
    out = tmp_path / "figs"
    code = main(["--root", str(tmp_path), "--out", str(out)])
    assert code == 0
    for name in _FILE_NAMES:
        assert (out / name).is_file()


def test_main_missing_db_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """__main__ with no model store exits 1 with a clear 'run chipgraph ingest' message."""
    out = tmp_path / "figs"
    code = main(["--root", str(tmp_path), "--out", str(out)])
    assert code == 1
    err = capsys.readouterr().err
    assert "chipgraph ingest" in err
    assert not out.exists()
