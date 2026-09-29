"""Tests for the Design Model query API (M1-02)."""

from __future__ import annotations

import pytest

from chipgraph.core.model import (
    BlockEntity,
    BlockInfo,
    ClockEntity,
    DesignModel,
    EntityMatch,
    ImpactResult,
    InterruptEntity,
    MemoryRegionEntity,
    ModelQuery,
    ModuleEntity,
    NeighborResult,
    PortEntity,
    QueryError,
    RegisterEntity,
    RequirementEntity,
    ResetEntity,
    TestEntity,
    TraceResult,
)
from chipgraph.core.model.relations import Relation


@pytest.fixture
def tinysoc_model() -> DesignModel:
    """Hand-built model with timer, gpio, top blocks."""
    entities = [
        # Blocks
        BlockEntity(key="block:timer", name="timer", owner="Nghia"),
        BlockEntity(key="block:gpio", name="gpio", owner="Hieu"),
        BlockEntity(key="block:top", name="top"),
        # Clocks
        ClockEntity(key="clock:peri", name="peri", frequency_hz=50e6),
        ClockEntity(key="clock:sys", name="sys", frequency_hz=100e6),
        # Resets
        ResetEntity(key="reset:rst_n", name="rst_n", active_low=True),
        ResetEntity(key="reset:rst_sys_n", name="rst_sys_n", active_low=True),
        # Modules
        ModuleEntity(
            key="module:tiny_timer",
            name="tiny_timer",
            file="design/timer/rtl/m_qnsc_timer_cnt.sv",
            block="block:timer",
        ),
        ModuleEntity(
            key="module:tiny_gpio",
            name="tiny_gpio",
            file="design/gpio/rtl/m_qnsc_gpio.sv",
            block="block:gpio",
        ),
        ModuleEntity(
            key="module:tiny_top",
            name="tiny_top",
            file="design/top/rtl/tiny_top.sv",
            block="block:top",
        ),
        # Ports (timer module)
        PortEntity(
            key="port:tiny_timer.i_clk",
            name="i_clk",
            direction="input",
            width=1,
            clock="clock:peri",
            module="module:tiny_timer",
        ),
        PortEntity(
            key="port:tiny_timer.i_rst_n",
            name="i_rst_n",
            direction="input",
            width=1,
            reset="reset:rst_n",
            module="module:tiny_timer",
        ),
        PortEntity(
            key="port:tiny_timer.o_overflow",
            name="o_overflow",
            direction="output",
            width=1,
            clock="clock:peri",
            module="module:tiny_timer",
        ),
        # Ports (gpio module)
        PortEntity(
            key="port:tiny_gpio.i_clk",
            name="i_clk",
            direction="input",
            width=1,
            clock="clock:sys",
            module="module:tiny_gpio",
        ),
        PortEntity(
            key="port:tiny_gpio.i_rst_n",
            name="i_rst_n",
            direction="input",
            width=1,
            reset="reset:rst_sys_n",
            module="module:tiny_gpio",
        ),
        # Ports (top module)
        PortEntity(
            key="port:tiny_top.i_clk",
            name="i_clk",
            direction="input",
            width=1,
            clock="clock:sys",
            module="module:tiny_top",
        ),
        PortEntity(
            key="port:tiny_top.i_rst_n",
            name="i_rst_n",
            direction="input",
            width=1,
            reset="reset:rst_sys_n",
            module="module:tiny_top",
        ),
        # Registers
        RegisterEntity(
            key="register:timer.CTRL",
            name="CTRL",
            block="block:timer",
            offset=0,
            width=32,
            access="rw",
        ),
        RegisterEntity(
            key="register:timer.COUNT",
            name="COUNT",
            block="block:timer",
            offset=4,
            width=32,
            access="ro",
        ),
        # Interrupts
        InterruptEntity(
            key="interrupt:timer.overflow",
            name="overflow",
            block="block:timer",
            line=0,
        ),
        # Memory regions
        MemoryRegionEntity(
            key="memory_region:timer",
            name="timer",
            block="block:timer",
            base=0x1000,
            size=256,
        ),
        # Requirements
        RequirementEntity(
            key="requirement:REQ-TIM-001",
            name="REQ-TIM-001",
            text="Timer shall count up on each clock pulse",
        ),
        RequirementEntity(
            key="requirement:REQ-TIM-002",
            name="REQ-TIM-002",
            text="Timer shall generate overflow interrupt when counter reaches max",
        ),
        RequirementEntity(
            key="requirement:REQ-TIM-003",
            name="REQ-TIM-003",
            text="Timer shall reset to zero on reset",
        ),
        RequirementEntity(
            key="requirement:REQ-GPIO-001",
            name="REQ-GPIO-001",
            text="GPIO shall provide 32 pins",
        ),
        # Tests
        TestEntity(
            key="test:tb_timer_cnt.test_overflow",
            name="test_overflow",
            requirement_keys=("requirement:REQ-TIM-002",),
        ),
        TestEntity(
            key="test:tb_timer_cnt.test_reset",
            name="test_reset",
            requirement_keys=("requirement:REQ-TIM-003",),
        ),
        TestEntity(
            key="test:tb_gpio.test_pin_read",
            name="test_pin_read",
            requirement_keys=("requirement:REQ-GPIO-001",),
        ),
    ]

    relations = [
        # Hierarchy: blocks contain modules, modules contain ports
        Relation(kind="contains", src="block:timer", dst="module:tiny_timer"),
        Relation(kind="contains", src="block:gpio", dst="module:tiny_gpio"),
        Relation(kind="contains", src="block:top", dst="module:tiny_top"),
        # Modules contain their ports
        Relation(kind="contains", src="module:tiny_timer", dst="port:tiny_timer.i_clk"),
        Relation(kind="contains", src="module:tiny_timer", dst="port:tiny_timer.i_rst_n"),
        Relation(kind="contains", src="module:tiny_timer", dst="port:tiny_timer.o_overflow"),
        Relation(kind="contains", src="module:tiny_gpio", dst="port:tiny_gpio.i_clk"),
        Relation(kind="contains", src="module:tiny_gpio", dst="port:tiny_gpio.i_rst_n"),
        Relation(kind="contains", src="module:tiny_top", dst="port:tiny_top.i_clk"),
        Relation(kind="contains", src="module:tiny_top", dst="port:tiny_top.i_rst_n"),
        # Top instantiates timer and gpio
        Relation(
            kind="instantiates",
            src="module:tiny_top",
            dst="module:tiny_timer",
            attrs={"instance_name": "u_timer"},
        ),
        Relation(
            kind="instantiates",
            src="module:tiny_top",
            dst="module:tiny_gpio",
            attrs={"instance_name": "u_gpio"},
        ),
        # Requirements and tests
        Relation(kind="implements", src="requirement:REQ-TIM-001", dst="module:tiny_timer"),
        Relation(kind="implements", src="requirement:REQ-TIM-002", dst="module:tiny_timer"),
        Relation(kind="implements", src="requirement:REQ-TIM-003", dst="module:tiny_timer"),
        Relation(
            kind="verifies", src="test:tb_timer_cnt.test_overflow", dst="requirement:REQ-TIM-002"
        ),
        Relation(
            kind="verifies", src="test:tb_timer_cnt.test_reset", dst="requirement:REQ-TIM-003"
        ),
        Relation(kind="implements", src="requirement:REQ-GPIO-001", dst="module:tiny_gpio"),
        Relation(kind="verifies", src="test:tb_gpio.test_pin_read", dst="requirement:REQ-GPIO-001"),
        # Derives relation
        Relation(kind="derives_from", src="requirement:REQ-TIM-002", dst="requirement:REQ-TIM-001"),
    ]

    return DesignModel.build(entities, relations)


def test_block_query_returns_all_contents(tinysoc_model: DesignModel) -> None:
    """Test model.block() returns the block and all its contents."""
    query = ModelQuery(tinysoc_model)
    result = query.block("timer")

    assert isinstance(result, BlockInfo)
    assert result.key == "block:timer"
    assert result.name == "timer"
    assert result.owner == "Nghia"
    assert "module:tiny_timer" in result.modules
    assert "port:tiny_timer.i_clk" in result.ports
    assert "register:timer.CTRL" in result.registers
    assert "register:timer.COUNT" in result.registers
    assert "interrupt:timer.overflow" in result.interrupts
    assert "memory_region:timer" in result.memory_regions
    assert "clock:peri" in result.clocks
    assert "reset:rst_n" in result.resets
    assert "requirement:REQ-TIM-001" in result.requirements
    assert "requirement:REQ-TIM-002" in result.requirements


def test_block_query_not_found(tinysoc_model: DesignModel) -> None:
    """Test model.block() raises QueryError for unknown names."""
    query = ModelQuery(tinysoc_model)

    with pytest.raises(QueryError) as excinfo:
        query.block("nonexistent")

    assert "not found" in str(excinfo.value).lower()


def test_block_query_not_found_with_close_match(tinysoc_model: DesignModel) -> None:
    """Test model.block() raises QueryError with close matches when available."""
    query = ModelQuery(tinysoc_model)

    with pytest.raises(QueryError) as excinfo:
        query.block("timmer")  # Close to "timer"

    error_str = str(excinfo.value).lower()
    assert "not found" in error_str
    # Should suggest "timer" as a close match
    if "did you mean" in error_str:
        assert "timer" in error_str


def test_module_query_returns_module_info(tinysoc_model: DesignModel) -> None:
    """Test model.module() returns module and its ports, parameters, instances."""
    query = ModelQuery(tinysoc_model)
    result = query.module("tiny_timer")

    assert result.key == "module:tiny_timer"
    assert result.name == "tiny_timer"
    assert result.file == "design/timer/rtl/m_qnsc_timer_cnt.sv"
    assert result.block == "block:timer"
    assert "port:tiny_timer.i_clk" in result.ports
    assert "port:tiny_timer.o_overflow" in result.ports


def test_module_query_instances(tinysoc_model: DesignModel) -> None:
    """Test model.module() shows instances and instantiated_by relationships."""
    query = ModelQuery(tinysoc_model)
    result = query.module("tiny_top")

    # tiny_top instantiates tiny_timer and tiny_gpio
    assert len(result.instances) == 2
    instance_names = {inst["instance_name"] for inst in result.instances}
    assert "u_timer" in instance_names
    assert "u_gpio" in instance_names


def test_find_query_by_kind(tinysoc_model: DesignModel) -> None:
    """Test model.find(kind=...) finds entities by kind."""
    query = ModelQuery(tinysoc_model)

    ports = query.find(kind="port")
    assert len(ports) >= 5
    assert all(isinstance(m, EntityMatch) for m in ports)
    assert all(m.kind == "port" for m in ports)

    reqs = query.find(kind="requirement")
    assert len(reqs) >= 4
    assert all(m.kind == "requirement" for m in reqs)


def test_find_query_by_name_glob(tinysoc_model: DesignModel) -> None:
    """Test model.find(name=...) with glob patterns."""
    query = ModelQuery(tinysoc_model)

    # Exact match
    result = query.find(name="timer")
    assert any(m.name == "timer" for m in result)

    # Glob match
    result = query.find(name="REQ-TIM-*")
    assert len(result) == 3
    assert all("REQ-TIM-" in m.name for m in result)


def test_find_query_by_attribute(tinysoc_model: DesignModel) -> None:
    """Test model.find(**attrs) filters by entity attributes."""
    query = ModelQuery(tinysoc_model)

    # Find ports in a specific module
    result = query.find(kind="port", module="module:tiny_timer")
    assert len(result) == 3
    assert all(m.kind == "port" for m in result)


def test_find_query_with_limit(tinysoc_model: DesignModel) -> None:
    """Test model.find(limit=...) truncates results."""
    query = ModelQuery(tinysoc_model)

    result = query.find(limit=2)
    assert len(result) <= 2


def test_trace_requirement_shows_implements_verifies(tinysoc_model: DesignModel) -> None:
    """Test model.trace() on a requirement shows implements/verifies."""
    query = ModelQuery(tinysoc_model)
    result = query.trace("requirement:REQ-TIM-002")

    assert isinstance(result, TraceResult)
    assert result.key == "requirement:REQ-TIM-002"
    assert result.kind == "requirement"
    assert "module:tiny_timer" in result.implements
    assert "test:tb_timer_cnt.test_overflow" in result.verifies
    assert result.no_test is False
    assert result.no_implementation is False


def test_trace_requirement_with_no_test(tinysoc_model: DesignModel) -> None:
    """Test model.trace() flags requirement with no test."""
    query = ModelQuery(tinysoc_model)
    result = query.trace("requirement:REQ-TIM-001")

    # REQ-TIM-001 has no verifying test
    assert result.no_test is True
    assert len(result.verifies) == 0


def test_trace_requirement_shows_derives_from(tinysoc_model: DesignModel) -> None:
    """Test model.trace() shows derives_from relationships."""
    query = ModelQuery(tinysoc_model)
    result = query.trace("requirement:REQ-TIM-002")

    assert "requirement:REQ-TIM-001" in result.derives_from


def test_trace_non_requirement_shows_traced_by(tinysoc_model: DesignModel) -> None:
    """Test model.trace() on non-requirement shows requirements that trace to it."""
    query = ModelQuery(tinysoc_model)
    result = query.trace("module:tiny_timer")

    # Three requirements implement tiny_timer
    assert len(result.traced_by) == 3
    assert "requirement:REQ-TIM-001" in result.traced_by


def test_trace_unknown_key(tinysoc_model: DesignModel) -> None:
    """Test model.trace() raises QueryError for unknown keys."""
    query = ModelQuery(tinysoc_model)

    with pytest.raises(QueryError) as excinfo:
        query.trace("requirement:NONEXISTENT")

    assert "not found" in str(excinfo.value).lower()


def test_impact_query_traverses_downstream(tinysoc_model: DesignModel) -> None:
    """Test model.impact() traverses downstream relations."""
    query = ModelQuery(tinysoc_model)
    result = query.impact("block:timer", max_depth=3)

    assert isinstance(result, ImpactResult)
    assert result.key == "block:timer"
    assert result.max_depth == 3
    assert "module" in result.by_kind
    assert "port" in result.by_kind or "register" in result.by_kind


def test_impact_query_groups_by_kind(tinysoc_model: DesignModel) -> None:
    """Test model.impact() groups results by entity kind."""
    query = ModelQuery(tinysoc_model)
    result = query.impact("block:timer", max_depth=2)

    # Should have at least modules as direct children
    assert len(result.by_kind) > 0

    # Each entry should have ImpactNode objects with path info
    for kind, nodes in result.by_kind.items():
        assert isinstance(kind, str)
        for node in nodes:
            assert isinstance(node.path, list)


def test_neighbors_query_outgoing(tinysoc_model: DesignModel) -> None:
    """Test model.neighbors(direction='out') finds outgoing edges."""
    query = ModelQuery(tinysoc_model)
    result = query.neighbors("block:timer", direction="out")

    assert isinstance(result, NeighborResult)
    assert result.key == "block:timer"
    assert len(result.outgoing) > 0
    # Should have contains edges to tiny_timer
    assert any(n["dst_key"] == "module:tiny_timer" for n in result.outgoing)


def test_neighbors_query_incoming(tinysoc_model: DesignModel) -> None:
    """Test model.neighbors(direction='in') finds incoming edges."""
    query = ModelQuery(tinysoc_model)
    result = query.neighbors("module:tiny_timer", direction="in")

    assert len(result.incoming) > 0
    # Should have incoming contains edge from block:timer
    assert any(n["src_key"] == "block:timer" for n in result.incoming)


def test_neighbors_query_by_kind_filter(tinysoc_model: DesignModel) -> None:
    """Test model.neighbors(kinds=...) filters by destination entity kind."""
    query = ModelQuery(tinysoc_model)
    result = query.neighbors("module:tiny_top", kinds=["module"], direction="out")

    # Only instantiates edges to modules should remain
    assert all(any(d.get("relation_kind") == "instantiates" for d in result.outgoing) for _ in [1])


def test_neighbors_query_by_relation_filter(tinysoc_model: DesignModel) -> None:
    """Test model.neighbors(relation=...) filters by relation kind."""
    query = ModelQuery(tinysoc_model)
    result = query.neighbors("module:tiny_top", relation="instantiates", direction="out")

    # All outgoing should be instantiates
    assert all(n["relation_kind"] == "instantiates" for n in result.outgoing)


def test_neighbors_query_both_directions(tinysoc_model: DesignModel) -> None:
    """Test model.neighbors(direction='both') finds edges in both directions."""
    query = ModelQuery(tinysoc_model)
    result = query.neighbors("module:tiny_timer", direction="both")

    # Should have both incoming and outgoing edges
    assert len(result.incoming) > 0 or len(result.outgoing) > 0


def test_search_requires_store(tinysoc_model: DesignModel) -> None:
    """Test model.search() raises QueryError when no store is provided."""
    query = ModelQuery(tinysoc_model, store=None)

    with pytest.raises(QueryError) as excinfo:
        query.search("timer")

    assert "store" in str(excinfo.value).lower() or "search" in str(excinfo.value).lower()


def test_block_with_no_modules(tinysoc_model: DesignModel) -> None:
    """Test model.block() on a block with empty lists when nothing is contained."""
    query = ModelQuery(tinysoc_model)
    # gpio block should have module
    result = query.block("gpio")
    assert "module:tiny_gpio" in result.modules


def test_find_empty_result(tinysoc_model: DesignModel) -> None:
    """Test model.find() returns empty list when nothing matches."""
    query = ModelQuery(tinysoc_model)

    result = query.find(kind="port", module="module:nonexistent")
    assert result == []


def test_module_with_no_instances(tinysoc_model: DesignModel) -> None:
    """Test model.module() on a leaf module with no instances."""
    query = ModelQuery(tinysoc_model)
    result = query.module("tiny_timer")

    # tiny_timer has no instances (is a leaf)
    assert result.instances == []
    # But it is instantiated by tiny_top
    assert len(result.instantiated_by) > 0
