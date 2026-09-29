"""The built-in adapters are found through their entry points."""

from chipgraph.core.plugin_api import Registry


def test_builtin_adapters_are_discovered() -> None:
    registry = Registry()
    registry.discover()
    assert "git" in registry.names("vcs")
    assert "local" in registry.names("runner")
    assert "cmd" in registry.names("tool")
    assert {"verilator", "verible"} <= set(registry.names("parser"))
    assert {"layout", "filelist", "generated", "naming"} <= set(registry.names("check"))
    assert "file" in registry.names("review")
