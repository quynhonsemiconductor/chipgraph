"""Tests for pack manifest loading and pack discovery/resolution."""

from pathlib import Path

import pytest
import yaml

from chipgraph.core.plugin_api.pack import (
    Pack,
    PackManifest,
    discover_packs,
    load_pack,
    resolve_requires,
)
from chipgraph.core.plugin_api.registry import PluginError


def _write_pack(
    directory: Path,
    *,
    name: str = "digital-rtl",
    version: str = "0.1.0",
    with_rule_file: bool = True,
    extra_manifest: dict[str, object] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "name": name,
        "version": version,
        "description": "Digital RTL rules.",
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    if with_rule_file:
        (directory / "rules").mkdir(exist_ok=True)
        (directory / "rules" / "rtl_module.yml").write_text("id: rtl_module\n")
        manifest["provides"] = {"rules": ["rules/rtl_module.yml"], **manifest.get("provides", {})}
    (directory / "pack.yml").write_text(yaml.safe_dump(manifest))
    return directory


def test_load_valid_pack(tmp_path: Path) -> None:
    pack_dir = _write_pack(tmp_path / "digital-rtl")
    pack = load_pack(pack_dir)
    assert isinstance(pack, Pack)
    assert pack.manifest.name == "digital-rtl"
    assert pack.manifest.version == "0.1.0"
    assert pack.path("rules/rtl_module.yml") == pack_dir / "rules/rtl_module.yml"


def test_load_pack_missing_manifest(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PluginError, match=r"no pack\.yml"):
        load_pack(empty)


def test_bad_name_rejected(tmp_path: Path) -> None:
    pack_dir = _write_pack(tmp_path / "bad", name="Bad_Name!")
    with pytest.raises(PluginError, match="invalid pack manifest"):
        load_pack(pack_dir)


def test_bad_version_rejected(tmp_path: Path) -> None:
    pack_dir = _write_pack(tmp_path / "bad", version="not-semver")
    with pytest.raises(PluginError, match="invalid pack manifest"):
        load_pack(pack_dir)


def test_missing_provided_path_rejected(tmp_path: Path) -> None:
    pack_dir = tmp_path / "digital-rtl"
    pack_dir.mkdir()
    manifest = {
        "name": "digital-rtl",
        "version": "0.1.0",
        "provides": {"rules": ["rules/does_not_exist.yml"]},
    }
    (pack_dir / "pack.yml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(PluginError, match="missing path"):
        load_pack(pack_dir)


def test_commands_do_not_need_to_exist(tmp_path: Path) -> None:
    pack_dir = tmp_path / "assist"
    pack_dir.mkdir()
    manifest = {
        "name": "assist",
        "version": "0.1.0",
        "provides": {"commands": ["/ask", "/triage"]},
    }
    (pack_dir / "pack.yml").write_text(yaml.safe_dump(manifest))
    pack = load_pack(pack_dir)
    assert pack.manifest.provides.commands == ("/ask", "/triage")


def test_discover_packs_finds_immediate_subdirectories(tmp_path: Path) -> None:
    search_root = tmp_path / "packs"
    _write_pack(search_root / "digital-rtl", name="digital-rtl")
    _write_pack(search_root / "dv", name="dv", with_rule_file=False)
    packs = discover_packs([search_root])
    assert set(packs) == {"digital-rtl", "dv"}


def test_discover_packs_duplicate_name_raises(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write_pack(root_a / "digital-rtl", name="digital-rtl")
    _write_pack(root_b / "digital-rtl-copy", name="digital-rtl")
    with pytest.raises(PluginError, match="duplicate pack name"):
        discover_packs([root_a, root_b])


def test_resolve_requires_ok(tmp_path: Path) -> None:
    search_root = tmp_path / "packs"
    _write_pack(search_root / "lang-sv", name="lang-sv", with_rule_file=False)
    _write_pack(
        search_root / "digital-rtl",
        name="digital-rtl",
        extra_manifest={"requires": {"packs": ["lang-sv"]}},
    )
    packs = discover_packs([search_root])
    resolve_requires(packs)  # must not raise


def test_resolve_requires_missing_raises(tmp_path: Path) -> None:
    search_root = tmp_path / "packs"
    _write_pack(
        search_root / "digital-rtl",
        name="digital-rtl",
        extra_manifest={"requires": {"packs": ["lang-sv"]}},
    )
    packs = discover_packs([search_root])
    with pytest.raises(PluginError, match="requires pack 'lang-sv'"):
        resolve_requires(packs)


def test_manifest_defaults() -> None:
    manifest = PackManifest(name="dv", version="1.0.0")
    assert manifest.requires.core == ">=0.0"
    assert manifest.provides.rules == ()
    assert manifest.schema_version == 1


def test_schema_key_in_pack_yml_maps_to_schemas() -> None:
    from chipgraph.core.plugin_api.pack import PackProvides

    provides = PackProvides.model_validate({"schema": ["schema/block.json"]})
    assert provides.schemas == ("schema/block.json",)
