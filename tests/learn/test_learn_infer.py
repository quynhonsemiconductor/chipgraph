"""Tests for `chipgraph.learn.infer.learn`: layout, naming, header, filelist, doc, blocks."""

from __future__ import annotations

from pathlib import Path

from learn_helpers import (
    make_prefixed_repo,
    make_single_ip_repo,
    make_vendored_repo,
)

from chipgraph.learn import learn


def _layout(result: object, kind: str) -> object | None:
    for rule in result.layout:  # type: ignore[attr-defined]
        if rule.kind == kind:
            return rule
    return None


def _naming(result: object, kind: str) -> object | None:
    for rule in result.naming:  # type: ignore[attr-defined]
        if rule.kind == kind:
            return rule
    return None


def test_learn_infers_layout_templates_from_repeated_block(tmp_path: Path) -> None:
    result = learn(make_prefixed_repo(tmp_path))
    assert set(result.blocks) == {"timer", "gpio"}
    rtl = _layout(result, "rtl")
    assert rtl is not None
    assert rtl.template == "rtl/blk_{block}.sv"  # type: ignore[attr-defined]
    assert rtl.coverage.matched == rtl.coverage.total == 2  # type: ignore[attr-defined]
    filelist = _layout(result, "filelist")
    assert filelist is not None and filelist.template == "filelists/{block}.f"  # type: ignore[attr-defined]
    spec = _layout(result, "spec")
    assert spec is not None and spec.template == "doc/specs/BLK_{BLOCK}_MAS.md"  # type: ignore[attr-defined]


def test_learn_infers_naming_patterns_via_pyslang(tmp_path: Path) -> None:
    result = learn(make_prefixed_repo(tmp_path))
    port = _naming(result, "port")
    assert port is not None
    # Every port is i_/o_ prefixed, so the prefix family is chosen and emitted.
    assert port.pattern == r"^(?:i|o|io)_[A-Za-z0-9_]+$"  # type: ignore[attr-defined]
    assert port.emitted is True  # type: ignore[attr-defined]
    assert port.coverage.ratio == 1.0  # type: ignore[attr-defined]
    signal = _naming(result, "signal")
    assert signal is not None and signal.pattern == r"^(?:r|w|mem)_[A-Za-z0-9_]+$"  # type: ignore[attr-defined]
    instance = _naming(result, "instance")
    assert instance is not None and instance.pattern == r"^u_[A-Za-z0-9_]+$"  # type: ignore[attr-defined]


def test_learn_reports_coverage_statistic_in_description(tmp_path: Path) -> None:
    result = learn(make_prefixed_repo(tmp_path))
    port = _naming(result, "port")
    assert port is not None
    # "97% of ports match `...`" style statistic (DESIGN.md 8.6 V1).
    assert port.description.startswith("100% of ports match")  # type: ignore[attr-defined]


def test_learn_below_threshold_pattern_is_observation_not_rule(tmp_path: Path) -> None:
    # A repo whose ports do NOT share a prefix: the prefix rule cannot reach the default
    # 0.9 threshold, so it is reported as an observation, not emitted as a rule. Raise the
    # bar so even the permissive fallback (which always matches) is not emitted.
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/mix.sv").write_text(
        "module mix(input logic clk, input logic BadName, output logic q); endmodule\n",
        encoding="utf-8",
    )
    result = learn(tmp_path, threshold=1.01)
    port = _naming(result, "port")
    assert port is not None
    assert port.emitted is False  # type: ignore[attr-defined]
    topics = {obs.topic for obs in result.observations}  # type: ignore[attr-defined]
    assert "naming" in topics


def test_learn_observes_header_and_filelist_and_doc(tmp_path: Path) -> None:
    result = learn(make_prefixed_repo(tmp_path))
    by_topic = {obs.topic: obs for obs in result.observations}  # type: ignore[attr-defined]
    assert "header" in by_topic
    assert "SPDX-License-Identifier" in by_topic["header"].summary
    assert by_topic["filelist"].detail["style"] == "relative-to-filelist"  # type: ignore[index]
    assert "Registers" in by_topic["doc"].detail["headings"]  # type: ignore[index]


def test_learn_single_ip_repo_has_no_blocks_and_globs_rtl(tmp_path: Path) -> None:
    result = learn(make_single_ip_repo(tmp_path))
    assert result.blocks == ()
    rtl = _layout(result, "rtl")
    assert rtl is not None and rtl.template == "rtl/*.sv"  # type: ignore[attr-defined]
    assert rtl.coverage.ratio == 1.0  # type: ignore[attr-defined]


def test_learn_exempts_vendored_directories(tmp_path: Path) -> None:
    result = learn(make_vendored_repo(tmp_path))
    assert any("vendor" in glob for glob in result.vendor_paths)
    # The vendored file must not drag naming coverage down: it is excluded from inference.
    module = _naming(result, "module")
    assert module is not None
    assert all("Acme" not in ex for ex in module.examples)  # type: ignore[attr-defined]
