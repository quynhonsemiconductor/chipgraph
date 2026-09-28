"""Tests for the plugin Registry."""

from importlib.metadata import EntryPoint

import pytest
from fakes import FakeCheck, FakeRunner, NotAProtocol

from chipgraph.core.plugin_api.registry import KINDS, PluginError, Registry


def test_register_get_names_round_trip() -> None:
    registry = Registry()
    runner = FakeRunner()
    registry.register("runner", "fake-runner", runner)
    assert registry.get("runner", "fake-runner") is runner
    assert registry.names("runner") == ("fake-runner",)


def test_names_sorted() -> None:
    registry = Registry()
    registry.register("check", "z-check", FakeCheck())
    registry.register("check", "a-check", FakeCheck())
    assert registry.names("check") == ("a-check", "z-check")


def test_register_duplicate_name_raises() -> None:
    registry = Registry()
    registry.register("runner", "fake-runner", FakeRunner())
    with pytest.raises(PluginError, match="already registered"):
        registry.register("runner", "fake-runner", FakeRunner())


def test_register_unknown_kind_raises() -> None:
    registry = Registry()
    with pytest.raises(PluginError, match="unknown plugin kind"):
        registry.register("nonsense", "x", FakeRunner())


def test_get_unknown_kind_raises() -> None:
    registry = Registry()
    with pytest.raises(PluginError, match="unknown plugin kind"):
        registry.get("nonsense", "x")


def test_names_unknown_kind_raises() -> None:
    registry = Registry()
    with pytest.raises(PluginError, match="unknown plugin kind"):
        registry.names("nonsense")


def test_register_protocol_mismatch_raises() -> None:
    registry = Registry()
    with pytest.raises(PluginError, match="does not satisfy"):
        registry.register("runner", "bad", NotAProtocol())


def test_get_missing_name_lists_available() -> None:
    registry = Registry()
    registry.register("runner", "a", FakeRunner())
    registry.register("runner", "b", FakeRunner())
    with pytest.raises(PluginError) as exc_info:
        registry.get("runner", "c")
    message = str(exc_info.value)
    assert "'a'" in message
    assert "'b'" in message


def test_all_kinds_present() -> None:
    expected = {
        "runner",
        "parser",
        "tool",
        "check",
        "vcs",
        "review",
        "llm",
        "runtime",
        "format",
        "extractor",
        "generator",
    }
    assert set(KINDS) == expected


class _FakeDistribution:
    version = "0.0.0"


def _make_entry_point(name: str, group: str, loader: object) -> EntryPoint:
    ep = EntryPoint(name=name, value=f"{__name__}:_unused", group=group)
    # Bypass normal value-based loading; patch `load` to return our fixed object/factory.
    object.__setattr__(ep, "load", lambda: loader)
    return ep


def test_discover_registers_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_entry_points(*, group: str) -> tuple[EntryPoint, ...]:
        if group == "chipgraph.adapters.runner":
            return (_make_entry_point("fake-runner", group, FakeRunner),)
        return ()

    monkeypatch.setattr("chipgraph.core.plugin_api.registry.entry_points", fake_entry_points)

    registry = Registry()
    count = registry.discover()
    assert count == 1
    assert isinstance(registry.get("runner", "fake-runner"), FakeRunner)


def test_discover_wraps_load_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> object:
        raise RuntimeError("boom")

    def fake_entry_points(*, group: str) -> tuple[EntryPoint, ...]:
        if group == "chipgraph.adapters.check":
            return (_make_entry_point("broken-check", group, _raise),)
        return ()

    monkeypatch.setattr("chipgraph.core.plugin_api.registry.entry_points", fake_entry_points)

    registry = Registry()
    with pytest.raises(PluginError, match="broken-check"):
        registry.discover()
