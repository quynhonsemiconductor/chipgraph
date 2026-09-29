"""The ``spec-core`` pack and its ``mas-markdown`` extractor are discoverable."""

from __future__ import annotations

from chipgraph.app.build import builtin_packs_dir
from chipgraph.core.plugin_api import Registry
from chipgraph.core.plugin_api.pack import discover_packs


def test_extractor_registered_by_entry_point() -> None:
    registry = Registry()
    registry.discover()
    assert "mas-markdown" in registry.names("extractor")
    extractor = registry.get("extractor", "mas-markdown")
    assert extractor.name == "mas-markdown"


def test_pack_discovered_among_builtins() -> None:
    packs_dir = builtin_packs_dir()
    assert packs_dir is not None
    packs = discover_packs([packs_dir])
    assert "spec-core" in packs
    pack = packs["spec-core"]
    assert pack.manifest.version == "0.1.0"
    assert pack.root.name == "spec_core"
