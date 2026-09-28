"""Tests for loading RuleSpec from rule files and packs."""

from pathlib import Path

import pytest
import yaml

from chipgraph.core.engine.rules import RuleLoadError, load_pack_rules, load_rule_file
from chipgraph.core.plugin_api.pack import load_pack

_RULE_BODY = {
    "kind": "gen",
    "outputs": ["design/{block}/rtl/m_{block}.sv"],
    "run": {"use": "cmd"},
}


def _write_rule(
    path: Path,
    *,
    id_key: str = "rule",
    rule_id: str = "digital-rtl/rtl_module",
    extra: dict | None = None,
) -> Path:
    body = {id_key: rule_id, **_RULE_BODY, **(extra or {})}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body))
    return path


def test_load_rule_file_with_rule_key(tmp_path: Path) -> None:
    path = _write_rule(tmp_path / "rtl_module.yml", id_key="rule")
    spec = load_rule_file(path)
    assert spec.id == "digital-rtl/rtl_module"
    assert spec.kind == "gen"


def test_load_rule_file_with_id_key(tmp_path: Path) -> None:
    path = _write_rule(tmp_path / "rtl_module.yml", id_key="id")
    spec = load_rule_file(path)
    assert spec.id == "digital-rtl/rtl_module"


def test_load_rule_file_rejects_both_keys(tmp_path: Path) -> None:
    path = tmp_path / "rtl_module.yml"
    body = {"rule": "digital-rtl/rtl_module", "id": "digital-rtl/rtl_module", **_RULE_BODY}
    path.write_text(yaml.safe_dump(body))
    with pytest.raises(RuleLoadError, match="both 'rule' and 'id'"):
        load_rule_file(path)


def test_load_rule_file_bad_yaml_names_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.yml"
    path.write_text("id: [unterminated\n")
    with pytest.raises(RuleLoadError, match=str(path)):
        load_rule_file(path)


def test_load_rule_file_not_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "bad.yml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(RuleLoadError, match=str(path)):
        load_rule_file(path)


def test_load_rule_file_invalid_spec_names_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.yml"
    path.write_text(yaml.safe_dump({"id": "digital-rtl/rtl_module", "kind": "gen"}))  # no outputs
    with pytest.raises(RuleLoadError, match=str(path)):
        load_rule_file(path)


def _write_pack(directory: Path, *, name: str = "digital-rtl", rules_provides: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "0.1.0",
        "provides": {"rules": rules_provides},
    }
    (directory / "pack.yml").write_text(yaml.safe_dump(manifest))
    return directory


def test_load_pack_rules_from_single_file(tmp_path: Path) -> None:
    pack_dir = tmp_path / "digital-rtl"
    _write_rule(pack_dir / "rules" / "rtl_module.yml")
    _write_pack(pack_dir, rules_provides=["rules/rtl_module.yml"])
    pack = load_pack(pack_dir)
    specs = load_pack_rules(pack)
    assert [spec.id for spec in specs] == ["digital-rtl/rtl_module"]


def test_load_pack_rules_from_directory(tmp_path: Path) -> None:
    pack_dir = tmp_path / "digital-rtl"
    _write_rule(pack_dir / "rules" / "rtl_module.yml", rule_id="digital-rtl/rtl_module")
    _write_rule(pack_dir / "rules" / "tb_module.yml", rule_id="digital-rtl/tb_module")
    _write_pack(pack_dir, rules_provides=["rules"])
    pack = load_pack(pack_dir)
    specs = load_pack_rules(pack)
    assert sorted(spec.id for spec in specs) == [
        "digital-rtl/rtl_module",
        "digital-rtl/tb_module",
    ]


def test_load_pack_rules_namespace_mismatch(tmp_path: Path) -> None:
    pack_dir = tmp_path / "digital-rtl"
    _write_rule(pack_dir / "rules" / "rtl_module.yml", rule_id="other-pack/rtl_module")
    _write_pack(pack_dir, rules_provides=["rules/rtl_module.yml"])
    pack = load_pack(pack_dir)
    with pytest.raises(RuleLoadError, match="does not belong to namespace"):
        load_pack_rules(pack)


def test_load_pack_rules_duplicate_ids(tmp_path: Path) -> None:
    pack_dir = tmp_path / "digital-rtl"
    _write_rule(pack_dir / "rules" / "a.yml", rule_id="digital-rtl/rtl_module")
    _write_rule(pack_dir / "rules" / "b.yml", rule_id="digital-rtl/rtl_module")
    _write_pack(pack_dir, rules_provides=["rules/a.yml", "rules/b.yml"])
    pack = load_pack(pack_dir)
    with pytest.raises(RuleLoadError, match="duplicate rule id"):
        load_pack_rules(pack)


def test_short_rule_id_gets_the_pack_namespace(tmp_path: Path) -> None:
    from chipgraph.core.engine.rules import load_rule_file

    f = tmp_path / "r.yml"
    f.write_text("rule: rtl_module\nkind: gen\noutputs: ['a/{block}.sv']\nrun: {use: cmd}\n")
    assert load_rule_file(f, namespace="digital-rtl").id == "digital-rtl/rtl_module"
