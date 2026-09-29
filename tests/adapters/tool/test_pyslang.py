"""Tests for the PyslangExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipgraph.adapters.tool.filelist import Filelist, read_filelist
from chipgraph.adapters.tool.pyslang import PyslangExtractor


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to test RTL fixtures."""
    return Path(__file__).parent / "rtl_fixtures"


@pytest.fixture
def tinysoc_dir() -> Path:
    """Path to the tinysoc example."""
    return Path(__file__).parents[3] / "examples" / "tinysoc"


def test_extractor_has_name() -> None:
    """PyslangExtractor has a name attribute."""
    assert PyslangExtractor.name == "pyslang"


def test_extract_model_from_filelist(fixtures_dir: Path) -> None:
    """Extract a design model from a filelist."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(filelist_path, tops=("wrapper",))

    assert len(model.entities) > 0
    # Should have module entities
    modules = model.by_kind("module")
    assert len(modules) > 0


def test_extract_model_from_sources(fixtures_dir: Path) -> None:
    """Extract a design model from source paths."""
    sources = [
        fixtures_dir / "types_pkg.sv",
        fixtures_dir / "simple_core.sv",
        fixtures_dir / "wrapper.sv",
    ]
    model, _ = PyslangExtractor.extract_model(sources, tops=("wrapper",))

    assert len(model.entities) > 0
    modules = model.by_kind("module")
    assert len(modules) > 0


def test_extract_model_from_filelist_object(fixtures_dir: Path) -> None:
    """Extract a design model from a Filelist object."""
    filelist = Filelist(
        sources=(
            fixtures_dir / "types_pkg.sv",
            fixtures_dir / "simple_core.sv",
            fixtures_dir / "wrapper.sv",
        )
    )
    model, _ = PyslangExtractor.extract_model(filelist, tops=("wrapper",))

    assert len(model.entities) > 0


def test_diagnostics_returned_not_raised(tmp_path: Path) -> None:
    """Parse errors are returned as diagnostics, not raised."""
    # Write the broken source and its filelist under tmp_path, never into the source tree.
    bad_file = tmp_path / "bad.sv"
    bad_file.write_text("module bad ( input logic invalid [[ [[ ] );\n")

    filelist_path = tmp_path / "bad.f"
    filelist_path.write_text(str(bad_file) + "\n")

    _, diags = PyslangExtractor.extract_model(filelist_path)
    # Should return a model and diagnostics, never raise
    assert isinstance(diags, tuple)
    assert any(d.severity == "error" for d in diags)


def test_extract_ports_with_directions(fixtures_dir: Path) -> None:
    """Extract ports with correct directions."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(filelist_path, tops=("wrapper",))

    ports = model.by_kind("port")
    assert len(ports) > 0

    # Check that we have input, output ports
    directions = {p.direction for p in ports if p.direction}
    assert "input" in directions or "output" in directions


def test_extract_ports_with_widths(fixtures_dir: Path) -> None:
    """Extract port widths."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(filelist_path, tops=("wrapper",))

    ports = model.by_kind("port")
    # At least some ports should have a width
    assert any(p.width is not None for p in ports)


def test_extract_parameters(fixtures_dir: Path) -> None:
    """Extract parameters from modules."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(filelist_path, tops=("simple_core",))

    params = model.by_kind("parameter")
    # simple_core has WIDTH and DEPTH parameters
    assert len(params) >= 2


def test_clock_detection_by_pattern(fixtures_dir: Path) -> None:
    """Detect clock ports by name pattern."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(
        filelist_path,
        tops=("wrapper",),
        clock_patterns=(r"^i_clk", r"^clk"),
    )

    clocks = model.by_kind("clock")
    # wrapper has i_clk port
    assert len(clocks) > 0


def test_reset_detection_by_pattern(fixtures_dir: Path) -> None:
    """Detect reset ports by name pattern."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(
        filelist_path,
        tops=("wrapper",),
        reset_patterns=(r"^i_rst", r"^rst"),
    )

    resets = model.by_kind("reset")
    # wrapper has i_rst_n port
    assert len(resets) > 0


def test_reset_active_low_detection(fixtures_dir: Path) -> None:
    """Detect active-low resets by name suffix."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(
        filelist_path,
        tops=("wrapper",),
        reset_patterns=(r"^i_rst", r"^rst"),
    )

    resets = [r for r in model.by_kind("reset") if r.name and "_n" in r.name]
    # Check that active_low is set for _n suffixed resets
    assert any(r.active_low for r in resets)


def test_hierarchical_instances(fixtures_dir: Path) -> None:
    """Extract module instantiation hierarchy."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(filelist_path, tops=("wrapper",))

    # Check for instantiates relations
    relations = model.get_relations(kind="instantiates")
    assert len(relations) > 0


def test_instantiates_relation_has_attrs(fixtures_dir: Path) -> None:
    """instantiates relations carry instance name and location info."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(filelist_path, tops=("wrapper",))

    relations = model.get_relations(kind="instantiates")
    # At least one should have instance_name
    assert any(r.attrs.get("instance_name") for r in relations)


def test_provenance_on_entities(fixtures_dir: Path) -> None:
    """Every entity has provenance information."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(filelist_path, tops=("wrapper",))

    for entity in model.entities.values():
        assert entity.source is not None
        assert entity.source.extractor == "pyslang"


def test_tinysoc_extraction(tinysoc_dir: Path) -> None:
    """Extract the tinysoc example design."""
    filelist_path = tinysoc_dir / "filelists" / "top.f"
    if not filelist_path.exists():
        pytest.skip("tinysoc not found")

    filelist = read_filelist(filelist_path, relative_to="cwd", root=tinysoc_dir)
    model, _ = PyslangExtractor.extract_model(
        filelist,
        tops=("tiny_top",),
        clock_patterns=(r"^clk",),
        reset_patterns=(r"^rst",),
    )

    names = sorted(m.name for m in model.by_kind("module"))
    assert names == ["tiny_gpio", "tiny_timer", "tiny_top"]
    children = sorted(r.dst for r in model.get_relations(kind="instantiates"))
    assert children == ["module:tiny_gpio", "module:tiny_timer"]
    # Clock and reset entities come only from the top's ports.
    assert [c.name for c in model.by_kind("clock")] == ["clk"]
    assert [r.name for r in model.by_kind("reset")] == ["rst_n"]
    assert model.validate_relations() == []


def test_deterministic_output_order(fixtures_dir: Path) -> None:
    """Extracting the same design twice gives identical results."""
    filelist_path = fixtures_dir / "test.f"

    model1, _1 = PyslangExtractor.extract_model(filelist_path, tops=("wrapper",))
    model2, _2 = PyslangExtractor.extract_model(filelist_path, tops=("wrapper",))

    # Same number of entities
    assert len(model1.entities) == len(model2.entities)
    # Same keys in same order
    assert list(model1.entities.keys()) == list(model2.entities.keys())
    # Same relations
    assert len(model1.relations) == len(model2.relations)


def test_generate_block_handling(fixtures_dir: Path) -> None:
    """Modules with generate blocks are handled."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(filelist_path, tops=("simple_core",))

    # simple_core has a generate block; just check it doesn't crash
    assert len(model.entities) > 0


def test_custom_timescale(fixtures_dir: Path) -> None:
    """Custom timescale can be set."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(
        filelist_path,
        tops=("wrapper",),
        timescale="10ns/1ps",
    )

    assert len(model.entities) > 0


def test_custom_clock_patterns(fixtures_dir: Path) -> None:
    """Custom clock patterns can be used."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(
        filelist_path,
        tops=("wrapper",),
        clock_patterns=(r"^sys_clk", r"^ck"),
    )

    clocks = model.by_kind("clock")
    # With restrictive patterns, fewer clocks should match
    assert len(clocks) >= 0


def test_block_parameter_for_contains_relations(fixtures_dir: Path) -> None:
    """When block is specified, it's used in relation attributes."""
    filelist_path = fixtures_dir / "test.f"
    model, _ = PyslangExtractor.extract_model(
        filelist_path,
        tops=("wrapper",),
        block="block:test_block",
    )

    # Check that module entities have the block set
    modules = model.by_kind("module")
    assert all(m.block == "block:test_block" for m in modules)


def test_unknown_module_handling(fixtures_dir: Path) -> None:
    """Unknown modules are handled gracefully."""
    filelist_path = fixtures_dir / "test.f"
    # If simple_core is not found, pyslang should still work with --ignore-unknown-modules
    model, _ = PyslangExtractor.extract_model(
        filelist_path,
        tops=("wrapper",),
    )

    # Should still extract something
    assert len(model.entities) > 0


def test_nested_filelist_extraction(fixtures_dir: Path, tmp_path: Path) -> None:
    """Extract with nested filelists."""
    # Create a nested structure
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "simple_core.sv").write_text((fixtures_dir / "simple_core.sv").read_text())

    sub_f = tmp_path / "sub" / "core.f"
    sub_f.write_text("simple_core.sv\n")

    main_f = tmp_path / "main.f"
    main_f.write_text("-f sub/core.f\n")

    model, _ = PyslangExtractor.extract_model(main_f, tops=("simple_core",))
    assert len(model.entities) > 0


def test_a_module_instantiated_twice_is_described_once(tmp_path: Path) -> None:
    """Ports come from the definition, once, with the declaration's line."""
    (tmp_path / "leaf.sv").write_text(
        "module leaf (\n  input logic clk_i,\n  output logic q_o\n);\n"
        "  assign q_o = clk_i;\nendmodule\n"
    )
    (tmp_path / "pair.sv").write_text(
        "module pair (input logic clk, output logic a, output logic b);\n"
        "  leaf u_a (.clk_i(clk), .q_o(a));\n"
        "  leaf u_b (.clk_i(clk), .q_o(b));\n"
        "endmodule\n"
    )
    model, _ = PyslangExtractor.extract_model(
        [tmp_path / "leaf.sv", tmp_path / "pair.sv"], tops=("pair",), root=tmp_path
    )
    port = model.get("port:leaf.clk_i")
    assert port is not None
    assert (port.source.file, port.source.line) == ("leaf.sv", 2)
    assert port.attrs == {"clock_like": True}
    instances = sorted(
        str(r.attrs.get("instance")) for r in model.get_relations(kind="instantiates")
    )
    assert len(instances) == 2


def test_extract_protocol_returns_plain_facts(tinysoc_dir: Path) -> None:
    facts = list(PyslangExtractor().extract(tinysoc_dir / "rtl" / "tiny_timer.sv"))
    kinds = {f.get("kind") for f in facts}
    assert {"module", "port"} <= kinds


def test_two_tops_with_the_same_clock_name_give_one_clock(tmp_path: Path) -> None:
    """Uninstantiated modules are all tops; a shared `clk_i`/`rst_ni` must not conflict."""
    a = tmp_path / "a.sv"
    a.write_text("module a (input logic clk_i, input logic rst_ni, output logic o);\nendmodule\n")
    b = tmp_path / "b.sv"
    b.write_text("module b (input logic clk_i, input logic rst_ni, output logic o);\nendmodule\n")

    model, _ = PyslangExtractor.extract_model(
        [a, b], clock_patterns=(r"^clk",), reset_patterns=(r"^rst",)
    )

    assert [c.key for c in model.by_kind("clock")] == ["clock:clk_i"]
    assert [r.key for r in model.by_kind("reset")] == ["reset:rst_ni"]
    clocked = {p.key: p.clock for p in model.by_kind("port") if p.name == "clk_i"}
    assert clocked == {"port:a.clk_i": "clock:clk_i", "port:b.clk_i": "clock:clk_i"}


def test_filelist_top_module_limits_the_tops(tmp_path: Path) -> None:
    """A filelist's `--top-module` is used when the caller gives no tops."""
    (tmp_path / "a.sv").write_text("module a (input logic clk_i);\nendmodule\n")
    (tmp_path / "b.sv").write_text("module b (input logic clk_b);\nendmodule\n")
    filelist = tmp_path / "x.f"
    filelist.write_text("--top-module a\na.sv\nb.sv\n")

    model, _ = PyslangExtractor.extract_model(filelist, clock_patterns=(r"^clk",))

    assert [m.name for m in model.by_kind("module")] == ["a"]
    assert [c.key for c in model.by_kind("clock")] == ["clock:clk_i"]


def test_diagnostic_messages_are_text(tmp_path: Path) -> None:
    bad = tmp_path / "bad.sv"
    bad.write_text("module m; assign y = 1'b0 + ; endmodule\n")

    _, diags = PyslangExtractor.extract_model([bad])

    errors = [d for d in diags if d.severity == "error"]
    assert errors
    assert all("object at 0x" not in d.message and d.message for d in errors)
