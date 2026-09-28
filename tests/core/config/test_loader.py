"""Tests for the layered configuration loader: merge order, provenance, extends, errors."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
from config_fakes import FakeFetcher

from chipgraph.core.config.errors import ConfigError
from chipgraph.core.config.loader import find_profile, load


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _init_git(root: Path) -> None:
    (root / ".git").mkdir(parents=True)


# --------------------------------------------------------------------------------------
# basic layering
# --------------------------------------------------------------------------------------


def test_each_layer_overrides_the_previous(tmp_path: Path) -> None:
    _init_git(tmp_path)
    _write(
        tmp_path / ".chipgraph.yml",
        """\
        project: qsoc
        autonomy: { rtl: L4 }
        target: { kind: fpga }
        """,
    )
    resolved = load(tmp_path)
    assert resolved is not None
    # tool default sets rtl: L3, project overrides to L4
    assert resolved.profile.autonomy["rtl"] == "L4"
    # tool default sets verify: L3, untouched by project
    assert resolved.profile.autonomy["verify"] == "L3"
    # project overrides target.kind, other target fields keep pydantic default
    assert resolved.profile.target.kind == "fpga"
    assert resolved.profile.target.pdk is None
    assert resolved.profile.project == "qsoc"


def test_provenance_points_to_the_right_file(tmp_path: Path) -> None:
    _init_git(tmp_path)
    project_file = _write(
        tmp_path / ".chipgraph.yml",
        """\
        project: qsoc
        autonomy: { rtl: L4 }
        """,
    )
    resolved = load(tmp_path)
    assert resolved is not None
    assert resolved.provenance["autonomy.rtl"].location == str(project_file)
    assert resolved.provenance["autonomy.rtl"].kind == "project"
    # verify wasn't touched by the project file: comes from the tool defaults layer
    assert resolved.provenance["autonomy.verify"].kind == "tool"


def test_no_profile_returns_none(tmp_path: Path) -> None:
    _init_git(tmp_path)
    assert load(tmp_path) is None


# --------------------------------------------------------------------------------------
# find_profile
# --------------------------------------------------------------------------------------


def test_find_profile_walks_up(tmp_path: Path) -> None:
    _init_git(tmp_path)
    profile = _write(tmp_path / ".chipgraph.yml", "project: qsoc\n")
    sub = tmp_path / "design" / "uart" / "rtl"
    sub.mkdir(parents=True)
    assert find_profile(sub) == profile


def test_find_profile_stops_at_git_boundary(tmp_path: Path) -> None:
    outer_profile = _write(tmp_path / ".chipgraph.yml", "project: outer\n")
    repo = tmp_path / "repo"
    _init_git(repo)
    sub = repo / "design"
    sub.mkdir(parents=True)
    # no .chipgraph.yml inside `repo`, and `repo` has .git: must not find the outer one
    assert find_profile(sub) is None
    assert outer_profile.is_file()  # sanity: the outer file does exist, just out of reach


def test_find_profile_found_in_git_root_itself(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    profile = _write(repo / ".chipgraph.yml", "project: qsoc\n")
    assert find_profile(repo / "design") == profile


# --------------------------------------------------------------------------------------
# extends: org / preset / path
# --------------------------------------------------------------------------------------


def test_extends_org_and_preset(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write(
        data_dir / "orgs" / "qnsc" / "profile.yml",
        """\
        reviewers: [nghia]
        naming: { rules: "org:qnsc/naming-v1.yml" }
        """,
    )
    _write(
        data_dir / "presets" / "mcu-soc" / "profile.yml",
        """\
        packs: [spec-core, digital-rtl]
        """,
    )
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(
        repo / ".chipgraph.yml",
        """\
        project: qsoc
        extends: [org:qnsc, preset:mcu-soc]
        """,
    )
    resolved = load(repo, data_dir=data_dir)
    assert resolved is not None
    assert resolved.profile.reviewers == ("nghia",)
    assert resolved.profile.naming.rules == "org:qnsc/naming-v1.yml"
    assert resolved.profile.packs == ("spec-core", "digital-rtl")
    kinds = {source.kind for source in resolved.sources}
    assert {"tool", "org", "preset", "project", "user"} <= kinds


def test_extends_path_relative_to_referencing_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(
        repo / "shared" / "base.yml",
        """\
        reviewers: [alice]
        """,
    )
    _write(
        repo / ".chipgraph.yml",
        """\
        project: qsoc
        extends: [path:shared/base.yml]
        """,
    )
    resolved = load(repo)
    assert resolved is not None
    assert resolved.profile.reviewers == ("alice",)
    assert resolved.provenance["reviewers"].kind == "path"


def test_extends_org_without_data_dir_is_clear_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / ".chipgraph.yml", "project: qsoc\nextends: [org:qnsc]\n")
    with pytest.raises(ConfigError, match="data_dir"):
        load(repo, data_dir=None)


# --------------------------------------------------------------------------------------
# extends: git+
# --------------------------------------------------------------------------------------


def test_extends_git_with_fake_fetcher_records_commit(tmp_path: Path) -> None:
    shared_repo = tmp_path / "shared-repo"
    _write(shared_repo / "profile.yml", "reviewers: [bob]\n")
    fetcher = FakeFetcher({"https://example.com/company-rules": shared_repo})

    repo = tmp_path / "repo"
    _init_git(repo)
    _write(
        repo / ".chipgraph.yml",
        "project: qsoc\nextends: [git+https://example.com/company-rules@v3]\n",
    )
    resolved = load(repo, fetcher=fetcher)
    assert resolved is not None
    assert resolved.profile.reviewers == ("bob",)
    git_sources = [s for s in resolved.sources if s.kind == "git"]
    assert len(git_sources) == 1
    assert git_sources[0].version == "commit-for-v3"
    assert git_sources[0].location == "https://example.com/company-rules"
    assert fetcher.calls == [("https://example.com/company-rules", "v3")]


def test_extends_git_without_ref_is_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / ".chipgraph.yml", "project: qsoc\nextends: [git+https://example.com/rules]\n")
    with pytest.raises(ConfigError, match="pin a tag or commit"):
        load(repo, fetcher=FakeFetcher({}))


def test_extends_git_without_fetcher_is_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / ".chipgraph.yml", "project: qsoc\nextends: [git+https://example.com/rules@v1]\n")
    with pytest.raises(ConfigError, match=re.escape("https://example.com/rules")):
        load(repo, fetcher=None)


# --------------------------------------------------------------------------------------
# cycles
# --------------------------------------------------------------------------------------


def test_cycle_in_extends_is_detected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / "a.yml", "extends: [path:b.yml]\n")
    _write(repo / "b.yml", "extends: [path:a.yml]\n")
    _write(repo / ".chipgraph.yml", "project: qsoc\nextends: [path:a.yml]\n")
    with pytest.raises(ConfigError, match="cycle"):
        load(repo)


# --------------------------------------------------------------------------------------
# schema errors
# --------------------------------------------------------------------------------------


def test_unknown_top_level_key_names_file_and_key(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    project_file = _write(repo / ".chipgraph.yml", "project: qsoc\nprojct: typo\n")
    with pytest.raises(ConfigError) as exc_info:
        load(repo)
    message = str(exc_info.value)
    assert str(project_file) in message
    assert "projct" in message


def test_invalid_yaml_names_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    project_file = _write(repo / ".chipgraph.yml", "project: [unterminated\n")
    with pytest.raises(ConfigError) as exc_info:
        load(repo)
    assert str(project_file) in str(exc_info.value)


def test_l5_rejected_through_loader(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / ".chipgraph.yml", "project: qsoc\nautonomy: { rtl: L5 }\n")
    with pytest.raises(ConfigError):
        load(repo)


# --------------------------------------------------------------------------------------
# user layer
# --------------------------------------------------------------------------------------


def test_user_layer_with_layout_is_an_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / ".chipgraph.yml", "project: qsoc\n")
    user_file = _write(tmp_path / "user.yml", "layout: { rtl: 'hw/{block}.v' }\n")
    with pytest.raises(ConfigError, match="conventions"):
        load(repo, user_config=user_file)


def test_user_config_merges_only_its_own_keys(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / ".chipgraph.yml", "project: qsoc\nautonomy: { rtl: L3 }\n")
    user_file = _write(tmp_path / "user.yml", "autonomy: { rtl: L2 }\nnotify: slack\n")
    resolved = load(repo, user_config=user_file)
    assert resolved is not None
    # the project's own autonomy is untouched by the user layer
    assert resolved.profile.autonomy["rtl"] == "L3"
    assert resolved.user.autonomy["rtl"] == "L2"
    assert resolved.user.notify == "slack"
    assert resolved.effective_autonomy("rtl") == "L2"


def test_user_config_missing_file_uses_defaults(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / ".chipgraph.yml", "project: qsoc\n")
    resolved = load(repo, user_config=tmp_path / "does-not-exist.yml")
    assert resolved is not None
    assert resolved.user.notify is None


# --------------------------------------------------------------------------------------
# profile_hash
# --------------------------------------------------------------------------------------


def test_profile_hash_stable_across_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / ".chipgraph.yml", "project: qsoc\nautonomy: { rtl: L4 }\n")
    first = load(repo)
    second = load(repo)
    assert first is not None
    assert second is not None
    assert first.profile_hash == second.profile_hash


def test_profile_hash_changes_when_a_value_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    _write(repo / ".chipgraph.yml", "project: qsoc\nautonomy: { rtl: L4 }\n")
    before = load(repo)
    assert before is not None
    _write(repo / ".chipgraph.yml", "project: qsoc\nautonomy: { rtl: L3 }\n")
    after = load(repo)
    assert after is not None
    assert before.profile_hash != after.profile_hash
