"""Tests for `ResolvedProfile.explain/check/for_path/for_block/effective_autonomy`."""

from __future__ import annotations

import textwrap
from pathlib import Path

from chipgraph.core.config.loader import load


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _init_git(root: Path) -> None:
    (root / ".git").mkdir(parents=True)


def _load(tmp_path: Path, project_yaml: str, user_yaml: str | None = None):
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / ".chipgraph.yml", project_yaml)
    user_file = None
    if user_yaml is not None:
        user_file = _write(tmp_path / "user.yml", user_yaml)
    resolved = load(
        repo, user_config=user_file if user_file is not None else tmp_path / "nouser.yml"
    )
    assert resolved is not None
    return resolved


# --------------------------------------------------------------------------------------
# explain
# --------------------------------------------------------------------------------------


def test_explain_is_sorted_and_covers_profile_and_user(tmp_path: Path) -> None:
    resolved = _load(
        tmp_path,
        "project: qsoc\nautonomy: { rtl: L4 }\n",
        "notify: slack\n",
    )
    leaves = resolved.explain()
    keys = [key for key, _, _ in leaves]
    assert keys == sorted(keys)
    assert "project" in keys
    assert "autonomy.rtl" in keys
    assert "user.notify" in keys
    by_key = {key: (value, source) for key, value, source in leaves}
    assert by_key["autonomy.rtl"][0] == "L4"
    assert by_key["autonomy.rtl"][1].kind == "project"
    assert by_key["user.notify"][0] == "slack"
    assert by_key["user.notify"][1].kind == "user"


# --------------------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------------------


def test_check_flags_user_raising_autonomy(tmp_path: Path) -> None:
    resolved = _load(
        tmp_path,
        "project: qsoc\nautonomy: { rtl: L2 }\n",
        "autonomy: { rtl: L4 }\n",
    )
    issues = resolved.check()
    assert any(i.key == "autonomy.rtl" and i.severity == "error" for i in issues)


def test_check_allows_user_lowering_autonomy(tmp_path: Path) -> None:
    resolved = _load(
        tmp_path,
        "project: qsoc\nautonomy: { rtl: L4 }\n",
        "autonomy: { rtl: L2 }\n",
    )
    issues = resolved.check()
    assert not any(i.key == "autonomy.rtl" for i in issues)
    assert resolved.effective_autonomy("rtl") == "L2"


def test_effective_autonomy_without_user_override(tmp_path: Path) -> None:
    resolved = _load(tmp_path, "project: qsoc\nautonomy: { rtl: L4 }\n")
    assert resolved.effective_autonomy("rtl") == "L4"


def test_check_flags_prefer_models_not_allowed(tmp_path: Path) -> None:
    resolved = _load(
        tmp_path,
        """\
        project: qsoc
        models: { tiers: { medium: claude-sonnet-5 } }
        """,
        "prefer_models: { medium: some-other-model }\n",
    )
    issues = resolved.check()
    assert any(i.key == "user.prefer_models.medium" for i in issues)


def test_check_allows_prefer_models_in_tiers_or_alt(tmp_path: Path) -> None:
    resolved = _load(
        tmp_path,
        """\
        project: qsoc
        models: { tiers: { medium: claude-sonnet-5 }, alt: { medium: glm } }
        """,
        "prefer_models: { medium: glm }\n",
    )
    issues = resolved.check()
    assert not any(i.key == "user.prefer_models.medium" for i in issues)


def test_check_flags_duplicate_layout_templates(tmp_path: Path) -> None:
    resolved = _load(
        tmp_path,
        """\
        project: qsoc
        layout:
          rtl: "design/{block}/rtl/{module}.sv"
          wrapper: "design/{block}/rtl/{module}.sv"
        """,
    )
    issues = resolved.check()
    assert any(i.key == "layout.wrapper" and i.severity == "error" for i in issues)


def test_check_reports_local_plugins_as_info(tmp_path: Path) -> None:
    resolved = _load(
        tmp_path,
        "project: qsoc\nplugins: ['.chipgraph/plugins/layout_rules.py']\n",
    )
    issues = resolved.check()
    assert any(i.severity == "info" and "layout_rules.py" in i.message for i in issues)


# --------------------------------------------------------------------------------------
# for_path
# --------------------------------------------------------------------------------------


def test_for_path_most_specific_pattern_wins(tmp_path: Path) -> None:
    resolved = _load(
        tmp_path,
        """\
        project: qsoc
        paths:
          "hw/**": { checks: { naming: "off" }, reason: "broad legacy rule" }
          "hw/vendor/**": { checks: { header: "off" }, write: deny, reason: "vendor IP" }
        """,
    )
    rule = resolved.for_path("hw/vendor/spi/spi_core.v")
    assert rule.checks["naming"] == "off"  # inherited from the broader rule
    assert rule.checks["header"] == "off"  # from the more specific rule
    assert rule.write == "deny"  # more specific rule wins entirely for scalar fields
    assert rule.reason == "vendor IP"


def test_for_path_no_match_returns_default_rule(tmp_path: Path) -> None:
    resolved = _load(
        tmp_path,
        """\
        project: qsoc
        paths:
          "hw/legacy/**": { checks: { naming: "off" }, reason: "old code" }
        """,
    )
    rule = resolved.for_path("hw/uart/uart_tx.v")
    assert rule.checks == {}
    assert rule.write == "allow"


# --------------------------------------------------------------------------------------
# for_block
# --------------------------------------------------------------------------------------


def test_for_block_overrides_only_that_block(tmp_path: Path) -> None:
    resolved = _load(
        tmp_path,
        """\
        project: qsoc
        autonomy: { rtl: L3 }
        layout: { rtl: "design/{block}/rtl/{module}.sv" }
        blocks:
          uart:
            autonomy: { rtl: L2 }
            layout: { rtl: "design/uart/legacy/{module}.sv" }
        """,
    )
    base = resolved.profile
    assert base.autonomy["rtl"] == "L3"
    uart = resolved.for_block("uart")
    assert uart.autonomy["rtl"] == "L2"
    assert uart.layout["rtl"] == "design/uart/legacy/{module}.sv"
    # other blocks are untouched
    spi = resolved.for_block("spi")
    assert spi.autonomy["rtl"] == "L3"
    assert spi is base or spi.autonomy == base.autonomy
