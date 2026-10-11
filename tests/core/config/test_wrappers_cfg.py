"""M2-12: the profile's `blocks.<b>.wrappers` section, and `config check` on it."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chipgraph.core.config import BlockOverride, Profile, WrapperCfg, load

WRAPPER = {
    "name": "fifo_wrap",
    "vendor_module": "vendor_fifo",
    "vendor_files": ["vendor/fifo/fifo.sv"],
}


def _resolved(tmp_path: Path, body: str):  # type: ignore[no-untyped-def]
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / ".chipgraph.yml").write_text(body, encoding="utf-8")
    resolved = load(tmp_path, user_config=tmp_path / "no-user.yml")
    assert resolved is not None
    return resolved


def _profile(*blocks: str) -> str:
    out = "project: p\nblocks:\n"
    for block in blocks:
        out += f"  {block}:\n    wrappers:\n      - name: {block}_wrap\n"
        out += "        vendor_module: vendor_fifo\n        vendor_files: [vendor/f.sv]\n"
    return out


def test_a_wrapper_has_a_name_an_ip_module_and_its_files() -> None:
    cfg = WrapperCfg.model_validate(WRAPPER)
    assert (cfg.name, cfg.vendor_module, cfg.vendor_files, cfg.exemplar) == (
        "fifo_wrap",
        "vendor_fifo",
        ("vendor/fifo/fifo.sv",),
        None,
    )
    assert BlockOverride().wrappers == ()


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"name": "1bad"}, "not a module name"),
        ({"vendor_module": "has space"}, "not a module name"),
        ({"vendor_files": []}, "at least 1"),
        ({"vendor_files": ["/abs/x.sv"]}, "not a repo-relative path"),
        ({"vendor_files": ["../x.sv"]}, "not a repo-relative path"),
        ({"vendor_files": ["a.sv", "a.sv"]}, "listed more than once"),
        ({"exemplar": "../x.src.sv"}, "not a repo-relative path"),
        ({"name": "vendor_fifo"}, "has the name of the module it wraps"),
        ({"bogus": 1}, "Extra inputs"),
    ],
)
def test_invalid_wrappers_are_rejected(change: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        WrapperCfg.model_validate({**WRAPPER, **change})


def test_a_wrapper_is_frozen_and_a_block_rejects_a_repeated_name() -> None:
    cfg = WrapperCfg.model_validate(WRAPPER)
    with pytest.raises(ValidationError):
        cfg.name = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="listed more than once"):
        BlockOverride.model_validate({"wrappers": [WRAPPER, WRAPPER]})


def test_the_profile_loads_its_wrappers(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path, _profile("top"))
    [wrapper] = resolved.profile.blocks["top"].wrappers
    assert wrapper.name == "top_wrap" and wrapper.vendor_files == ("vendor/f.sv",)
    assert not [i for i in resolved.check() if "wrappers" in i.key]


def test_wrappers_are_not_merged_into_the_block_profile(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path, _profile("top"))
    merged = resolved.for_block("top")
    assert isinstance(merged, Profile) and not hasattr(merged, "wrappers")


def test_config_check_reports_a_wrapper_name_used_in_two_blocks(tmp_path: Path) -> None:
    body = _profile("a") + _profile("b").replace("b_wrap", "a_wrap").split("blocks:\n")[1]
    resolved = _resolved(tmp_path, body)
    [issue] = [i for i in resolved.check() if "wrappers" in i.key]
    assert issue.severity == "error" and issue.key == "blocks.b.wrappers[0]"
    assert "already a wrapper of block 'a'" in issue.message


def test_config_check_reports_a_wrapper_named_like_an_ip_module(tmp_path: Path) -> None:
    body = (
        "project: p\nblocks:\n"
        "  a:\n    wrappers:\n"
        "      - { name: a_wrap, vendor_module: vendor_fifo, vendor_files: [vendor/f.sv] }\n"
        "  b:\n    wrappers:\n"
        "      - { name: vendor_fifo, vendor_module: other_ip, vendor_files: [vendor/o.sv] }\n"
    )
    [issue] = [i for i in _resolved(tmp_path, body).check() if "wrappers" in i.key]
    assert issue.severity == "error" and issue.key == "blocks.b.wrappers[0]"
    assert "has the name of an IP module" in issue.message and "block 'a'" in issue.message


def test_config_check_warns_about_an_ip_file_as_exemplar(tmp_path: Path) -> None:
    body = _profile("top").replace(
        "vendor_files: [vendor/f.sv]", "vendor_files: [vendor/f.sv]\n        exemplar: vendor/f.sv"
    )
    resolved = _resolved(tmp_path, body)
    [issue] = [i for i in resolved.check() if "wrappers" in i.key]
    assert issue.severity == "warning" and issue.key.endswith(".exemplar")
    assert "an exemplar is another wrapper's source" in issue.message


def test_the_sample_project_profile_validates() -> None:
    root = Path(__file__).resolve().parents[3] / "examples" / "tinysoc"
    resolved = load(root, user_config=root / "no-user.yml")
    assert resolved is not None
    [wrapper] = resolved.profile.blocks["top"].wrappers
    assert (wrapper.name, wrapper.vendor_module) == ("tiny_fifo_wrap", "vendor_fifo")
    assert not [i for i in resolved.check() if i.severity == "error"]
