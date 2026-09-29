"""Tests for the chip.yml format adapter."""

import json
from pathlib import Path

from chipgraph.adapters.format.chip_yaml import ChipYaml, ChipYamlAdapter, schema_json


def test_chip_yaml_load_model():
    """Load tiny chip fixture and verify structure."""
    adapter = ChipYamlAdapter()
    fixture_path = Path(__file__).parent / "fixtures" / "tiny_chip.yml"

    model, warnings = adapter.load_model(fixture_path)

    # Should have no warnings
    assert warnings == []

    # Count entities
    by_kind = {}
    for entity in model.entities.values():
        kind = entity.kind
        by_kind[kind] = by_kind.get(kind, 0) + 1

    assert by_kind["project"] == 1
    assert by_kind["block"] == 2  # timer and gpio
    assert by_kind["memory_region"] == 2
    assert by_kind["interrupt"] == 2
    assert by_kind["clock"] == 1
    assert by_kind["reset"] == 1


def test_chip_yaml_auto_address_resolution():
    """Test deterministic auto address resolution."""
    adapter = ChipYamlAdapter()
    fixture_path = Path(__file__).parent / "fixtures" / "tiny_chip.yml"

    model, _ = adapter.load_model(fixture_path)
    regions = model.by_kind("memory_region")
    by_name = {e.name: e for e in regions}

    timer = by_name["timer"]
    assert timer.base == 0  # First block at 0
    assert timer.size == 0x1000

    gpio = by_name["gpio"]
    assert gpio.base == 0x1000  # After timer
    assert gpio.size == 0x1000


def test_chip_yaml_auto_interrupt_resolution():
    """Test deterministic auto interrupt line resolution."""
    adapter = ChipYamlAdapter()
    fixture_path = Path(__file__).parent / "fixtures" / "tiny_chip.yml"

    model, _ = adapter.load_model(fixture_path)
    interrupts = model.by_kind("interrupt")

    lines = sorted([i.line for i in interrupts if i.line is not None])
    assert lines == [0, 1]  # Global assignment


def test_chip_yaml_schema_generation():
    """Test that schema can be generated and is valid."""
    schema = ChipYaml.model_json_schema()

    assert schema is not None
    assert schema["type"] == "object"
    assert "properties" in schema

    props = schema["properties"]
    assert "project" in props
    assert "data_width" in props
    assert "addr_width" in props
    assert "buses" in props
    assert "blocks" in props
    assert "clocks" in props
    assert "resets" in props


def test_chip_yaml_schema_serialization():
    """Test that schema serializes to JSON."""
    schema = ChipYaml.model_json_schema()
    json_str = json.dumps(schema, indent=2)
    assert json.loads(json_str) == schema


def test_chip_yaml_schema_matches_committed() -> None:
    """The committed chip.yml schema is what `python -m chipgraph.adapters.format` writes."""
    committed = Path(__file__).parents[3] / "schemas" / "formats" / "chip.schema.json"
    assert committed.read_text(encoding="utf-8") == schema_json()


def test_chip_yaml_validation_error_includes_file_and_context(
    tmp_path: Path,
) -> None:
    """Test that validation errors include file path."""
    import yaml

    bad_data = {
        "project": "test",
        "data_width": 32,
        "addr_width": 32,
        # Missing required buses, blocks, etc. -- validation should fail
        "invalid_field": "should not be here",
    }

    test_file = tmp_path / "bad.yml"
    with open(test_file, "w") as f:
        yaml.dump(bad_data, f)

    adapter = ChipYamlAdapter()

    try:
        adapter.load_model(test_file)
        raise AssertionError("Should have raised ValueError")
    except ValueError as exc:
        # Error message should include the file path
        assert str(test_file) in str(exc) or "bad.yml" in str(exc)
        assert "Invalid chip.yml" in str(exc)


def test_chip_yaml_relations_structure():
    """Test that relations have correct structure."""
    adapter = ChipYamlAdapter()
    fixture_path = Path(__file__).parent / "fixtures" / "tiny_chip.yml"

    model, _ = adapter.load_model(fixture_path)

    # Should have contains relations
    relations = model.relations
    contains = [r for r in relations if r.kind == "contains"]
    assert len(contains) > 0

    # Validate all relations
    validation_errors = model.validate_relations()
    assert validation_errors == []

    # All relations should have src/dst as entity keys
    for relation in relations:
        assert relation.src in model.entities
        assert relation.dst in model.entities


def test_chip_yaml_error_names_the_line(tmp_path: Path) -> None:
    bad = tmp_path / "chip.yml"
    bad.write_text(
        "project: p\ndata_width: 32\naddr_width: 32\nblocks:\n"
        "  - name: a\n    size: 4096\n  - name: b\n    size: -1\n"
    )
    try:
        ChipYamlAdapter().load_model(bad)
    except ValueError as exc:
        assert f"{bad.as_posix()}:8:" in str(exc)
    else:
        raise AssertionError("expected a ValueError")


def test_auto_never_takes_a_fixed_address_or_line(tmp_path: Path) -> None:
    chip = tmp_path / "chip.yml"
    chip.write_text(
        "project: p\ndata_width: 32\naddr_width: 32\nblocks:\n"
        "  - {name: a, base: auto, size: 4096, interrupts: [{name: x}]}\n"
        "  - {name: b, base: 0, size: 4096, interrupts: [{name: y, line: 0}]}\n"
        "  - {name: c, base: auto, size: 8192, interrupts: [{name: z}]}\n"
    )
    model, warnings = ChipYamlAdapter().load_model(chip)
    assert warnings == []
    bases = {r.name: r.base for r in model.by_kind("memory_region")}
    assert bases == {"a": 0x1000, "b": 0, "c": 0x2000}
    lines = {i.name: i.line for i in model.by_kind("interrupt")}
    assert lines == {"x": 1, "y": 0, "z": 2}
