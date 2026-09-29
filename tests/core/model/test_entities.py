"""Tests that every core entity kind constructs, validates, and round-trips."""

from __future__ import annotations

import pytest

from chipgraph.core.model.entities import (
    CORE_ENTITY_CLASSES,
    BlockEntity,
    ClockEntity,
    DecisionEntity,
    EarsParts,
    ExtEntity,
    FieldEntity,
    InterfaceEntity,
    InterruptEntity,
    MemoryRegionEntity,
    ModuleEntity,
    OpenItemEntity,
    ParameterEntity,
    PortEntity,
    ProjectEntity,
    RegisterEntity,
    RequirementEntity,
    ResetEntity,
    TestEntity,
)
from chipgraph.core.model.keys import make_key
from chipgraph.core.model.provenance import Provenance


def test_core_entity_classes_cover_every_design_kind() -> None:
    expected = {
        "project",
        "block",
        "module",
        "port",
        "interface",
        "clock",
        "reset",
        "parameter",
        "register",
        "field",
        "interrupt",
        "memory_region",
        "requirement",
        "decision",
        "open_item",
        "test",
    }
    actual = {cls.model_fields["kind"].default for cls in CORE_ENTITY_CLASSES}
    assert actual == expected


@pytest.mark.parametrize(
    ("factory",),
    [
        (lambda: ProjectEntity(key=make_key("project", "qsoc"), name="qsoc"),),
        (
            lambda: BlockEntity(
                key=make_key("block", "timer"), name="timer", owner="nghia", path="design/timer"
            ),
        ),
        (
            lambda: ModuleEntity(
                key=make_key("module", "tiny_timer"),
                name="tiny_timer",
                file="design/timer/rtl/tiny_timer.sv",
                block="block:timer",
            ),
        ),
        (
            lambda: PortEntity(
                key=make_key("port", "tiny_timer", "i_clk"),
                name="i_clk",
                direction="input",
                width=1,
                clock="clock:peri",
                reset="reset:rst_n",
                module="module:tiny_timer",
            ),
        ),
        (
            lambda: InterfaceEntity(
                key=make_key("interface", "timer", "apb"), name="apb", protocol="apb"
            ),
        ),
        (
            lambda: ClockEntity(
                key=make_key("clock", "peri"), name="peri", frequency_hz=20_000_000.0
            ),
        ),
        (
            lambda: ResetEntity(
                key=make_key("reset", "rst_n"), name="rst_n", active_low=True, sync=False
            ),
        ),
        (
            lambda: ParameterEntity(
                key=make_key("parameter", "tiny_timer", "WIDTH"),
                name="WIDTH",
                value=32,
                module="module:tiny_timer",
            ),
        ),
        (
            lambda: RegisterEntity(
                key=make_key("register", "timer", "CTRL"),
                name="CTRL",
                block="block:timer",
                offset=0x00,
                width=32,
                reset_value=0,
                access="rw",
            ),
        ),
        (
            lambda: FieldEntity(
                key=make_key("field", "timer", "CTRL", "EN"),
                name="EN",
                register="register:timer.CTRL",
                lsb=0,
                msb=0,
                access="rw",
                reset_value=0,
            ),
        ),
        (
            lambda: InterruptEntity(
                key=make_key("interrupt", "timer", "overflow"),
                name="overflow",
                block="block:timer",
                line=3,
            ),
        ),
        (
            lambda: MemoryRegionEntity(
                key=make_key("memory_region", "timer"),
                name="timer",
                base=0x4000_0000,
                size=0x1000,
                block="block:timer",
            ),
        ),
        (
            lambda: RequirementEntity(
                key=make_key("requirement", "REQ-TIM-004"),
                name="REQ-TIM-004",
                text="the counter shall stop at zero when AUTO_RELOAD is disabled",
                ears=EarsParts(trigger="counter reaches zero", action="stop counting"),
            ),
        ),
        (
            lambda: DecisionEntity(
                key=make_key("decision", "D-timer-0001"),
                name="D-timer-0001",
                text="use a 32-bit counter",
                rationale="matches the widest register client",
            ),
        ),
        (
            lambda: OpenItemEntity(
                key=make_key("open_item", "OI-timer-0001"), name="OI-timer-0001", status="open"
            ),
        ),
        (
            lambda: TestEntity(
                key=make_key("test", "tb_timer_cnt.test_overflow"),
                name="test_overflow",
                requirement_keys=("requirement:REQ-TIM-004",),
            ),
        ),
    ],
)
def test_every_core_kind_constructs_and_round_trips(factory: object) -> None:
    entity = factory()  # type: ignore[operator]
    dumped = entity.model_dump(mode="json")
    restored = type(entity).model_validate(dumped)
    assert restored == entity


def test_entity_defaults_are_lean() -> None:
    block = BlockEntity(key=make_key("block", "timer"), name="timer")
    assert block.owner is None
    assert block.path is None
    assert block.source == Provenance()
    assert block.attrs == {}


def test_entity_is_frozen_and_forbids_extra() -> None:
    block = BlockEntity(key=make_key("block", "timer"), name="timer")
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError on frozen instance
        block.name = "renamed"  # type: ignore[misc]
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError, extra="forbid"
        BlockEntity(key=make_key("block", "timer"), name="timer", not_a_field=1)  # type: ignore[call-arg]


def test_ext_entity_round_trips_a_pack_kind() -> None:
    ext = ExtEntity(
        key="pin_spec:timer.pad0",
        name="pad0",
        kind="pin_spec",
        attrs={"drive_strength": "8mA", "voltage": 3.3},
    )
    dumped = ext.model_dump(mode="json")
    restored = ExtEntity.model_validate(dumped)
    assert restored == ext
    assert restored.kind == "pin_spec"
    assert restored.attrs["drive_strength"] == "8mA"


def test_importing_entities_emits_no_warning() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", "import chipgraph.core.model.entities"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
