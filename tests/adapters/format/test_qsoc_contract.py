"""Tests for the QSoC contract format adapter."""

from pathlib import Path

from chipgraph.adapters.format.qsoc_contract import QSocContractAdapter


def test_qsoc_contract_load_model():
    """Load real QSoC contract and verify counts."""
    adapter = QSocContractAdapter()
    fixture_path = Path(__file__).parent / "fixtures" / "qsoc_contract.yml"

    model, warnings = adapter.load_model(fixture_path)

    # Should have no warnings on real file
    assert warnings == []

    # Count entities by kind
    by_kind = {}
    for entity in model.entities.values():
        kind = entity.kind
        by_kind[kind] = by_kind.get(kind, 0) + 1

    # Verify counts
    assert by_kind["project"] == 1
    assert by_kind["block"] == 18
    assert by_kind["memory_region"] == 18
    assert by_kind["interrupt"] == 11
    assert by_kind["clock"] == 4
    assert by_kind["reset"] == 3


def test_qsoc_contract_memory_regions():
    """Verify specific memory region addresses and sizes."""
    adapter = QSocContractAdapter()
    fixture_path = Path(__file__).parent / "fixtures" / "qsoc_contract.yml"

    model, _ = adapter.load_model(fixture_path)

    regions = model.by_kind("memory_region")
    by_name = {e.name: e for e in regions}

    # Check ROM
    rom = by_name["rom"]
    assert rom.base == 0x00000000
    assert rom.size == 0x00000800

    # Check ISRAM
    isram = by_name["isram"]
    assert isram.base == 0x20001000
    assert isram.size == 0x0000F000

    # Check DMA_CFG
    dma_cfg = by_name["dma_cfg"]
    assert dma_cfg.base == 0x80034000
    assert dma_cfg.size == 0x00004000


def test_qsoc_contract_interrupt_lines():
    """Verify interrupt line numbers."""
    adapter = QSocContractAdapter()
    fixture_path = Path(__file__).parent / "fixtures" / "qsoc_contract.yml"

    model, _ = adapter.load_model(fixture_path)

    interrupts = model.by_kind("interrupt")
    lines = sorted([i.line for i in interrupts if i.line is not None])
    assert lines == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_qsoc_contract_provenance_line_numbers():
    """Verify line numbers in provenance."""
    adapter = QSocContractAdapter()
    fixture_path = Path(__file__).parent / "fixtures" / "qsoc_contract.yml"

    model, _ = adapter.load_model(fixture_path)

    # Project should have a reasonable line number
    project = model.by_kind("project")[0]
    assert project.source.line == 30  # project: line in meta section

    # First block (rom) should have a line number
    rom = next((e for e in model.by_kind("block") if e.name == "rom"), None)
    assert rom is not None
    assert rom.source.line >= 50  # memory_map starts around line 50

    # All entities should have line numbers
    for entity in model.entities.values():
        assert entity.source.line >= 1, f"{entity.name} missing line"
        assert entity.source.file is not None


def test_qsoc_contract_relations():
    """Verify contains relations exist."""
    adapter = QSocContractAdapter()
    fixture_path = Path(__file__).parent / "fixtures" / "qsoc_contract.yml"

    model, _ = adapter.load_model(fixture_path)

    # Relations validation should pass
    validation_errors = model.validate_relations()
    assert validation_errors == []

    # Should have project -> block relations
    project_key = next((e.key for e in model.by_kind("project")), None)
    assert project_key is not None

    project_contains = model.get_relations(src=project_key, kind="contains")
    assert len(project_contains) == 18  # 18 blocks

    # Should have block -> memory_region relations
    block_key = next((e.key for e in model.by_kind("block") if e.name == "rom"), None)
    assert block_key is not None

    block_contains = model.get_relations(src=block_key, kind="contains")
    assert len(block_contains) >= 1  # rom contains region and/or interrupts


def test_qsoc_contract_overlapping_regions_warning(tmp_path: Path) -> None:
    """Test detection of overlapping memory regions."""
    import yaml

    # Create a mutated contract with overlapping regions
    contract_path = Path(__file__).parent / "fixtures" / "qsoc_contract.yml"
    with open(contract_path) as f:
        contract = yaml.safe_load(f)

    # Make two regions overlap: change rom size to 0x30000000 (huge)
    contract["memory_map"][0]["size"] = 0x30000000

    # Write to temp file
    test_file = tmp_path / "overlapping.yml"
    with open(test_file, "w") as f:
        yaml.dump(contract, f)

    adapter = QSocContractAdapter()
    _, warnings = adapter.load_model(test_file)

    # Should have overlap warnings
    assert any("overlapping" in w for w in warnings)


def test_qsoc_contract_duplicate_interrupt_warning(tmp_path: Path) -> None:
    """Test detection of duplicate interrupt lines."""
    import yaml

    # Create a mutated contract with duplicate interrupt lines
    contract_path = Path(__file__).parent / "fixtures" / "qsoc_contract.yml"
    with open(contract_path) as f:
        contract = yaml.safe_load(f)

    # Make two interrupts use the same line
    contract["interrupts"]["lines"][1]["line"] = 0  # Change line 1 to 0 (duplicate)

    # Write to temp file
    test_file = tmp_path / "duplicate_int.yml"
    with open(test_file, "w") as f:
        yaml.dump(contract, f)

    adapter = QSocContractAdapter()
    _, warnings = adapter.load_model(test_file)

    # Should have duplicate warnings
    assert any("duplicate" in w for w in warnings)


def test_qsoc_contract_links_interrupts_to_rows_and_keeps_lines() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "qsoc_contract.yml"
    model, _ = QSocContractAdapter().load_model(fixture_path)
    uart = model.get("interrupt:uart_0")
    assert uart is not None and uart.block == "block:uart_0"
    assert model.get("interrupt:dma").block is None  # no memory-map row named dma
    resets = [r.source.line for r in model.by_kind("reset")]
    assert len(set(resets)) == 1 and resets[0] > 300  # the inline list's line
    facts = list(QSocContractAdapter().load(fixture_path))
    assert any(f.get("kind") == "contains" for f in facts)
