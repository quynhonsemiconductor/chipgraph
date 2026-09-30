"""Tests for the draft-profile writer and the `chipgraph try` runner (M1-21 accept)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from learn_helpers import (
    copy_ip,
    file_tree,
    git_init,
    git_status,
    make_prefixed_repo,
    make_single_ip_repo,
    opentitan_ip_or_skip,
    pulp_ip_or_skip,
    rtl_file_count,
)

from chipgraph.core.config.models import Profile
from chipgraph.learn import build_naming_rules, build_profile_dict, learn, write_draft
from chipgraph.learn.try_run import try_run

_EXAMPLE_TINYSOC = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


def _copy_tinysoc(dest: Path) -> Path:
    shutil.copytree(_EXAMPLE_TINYSOC, dest)
    git_init(dest)
    return dest


def _files_with_issues_ratio(report: object, root: Path) -> float:
    total = rtl_file_count(root)
    return report.check_files_with_issues / max(1, total)  # type: ignore[attr-defined]


# --- draft writer -------------------------------------------------------------------


def test_draft_profile_is_a_valid_profile(tmp_path: Path) -> None:
    result = learn(make_prefixed_repo(tmp_path))
    data = build_profile_dict(result, naming_rules_ref="chipgraph.draft.naming.yml")
    # The draft must validate as a real Profile (extra="forbid"): learn emits only real keys.
    profile = Profile.model_validate(data)
    assert profile.project == tmp_path.name
    assert "layout" in profile.adapters
    assert "naming" in profile.adapters


def test_draft_omits_naming_when_nothing_meets_threshold(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/mix.sv").write_text(
        "module mix(input logic clk, input logic BadName); endmodule\n", encoding="utf-8"
    )
    result = learn(tmp_path, threshold=1.01)
    assert build_naming_rules(result) is None
    data = build_profile_dict(result, naming_rules_ref=None)
    assert "naming" not in data["adapters"]


def test_write_draft_writes_profile_and_naming_files(tmp_path: Path) -> None:
    repo = make_prefixed_repo(tmp_path / "repo")
    out = tmp_path / "out"
    result = learn(repo)
    profile_path, naming_path = write_draft(result, out)
    assert profile_path.is_file() and profile_path.name == "chipgraph.draft.yml"
    assert naming_path is not None and naming_path.is_file()
    # The draft never lands in the learned repo.
    assert not (repo / ".chipgraph.yml").exists()


def test_root_relative_filelist_does_not_emit_filelist_check(tmp_path: Path) -> None:
    # tinysoc's filelists are root-relative; the `filelist` check resolves filelist-relative,
    # so learn must not emit a `filelist` adapter that would fail every entry.
    result = learn(_EXAMPLE_TINYSOC)
    assert result.filelist_style == "root"
    data = build_profile_dict(result, naming_rules_ref="chipgraph.draft.naming.yml")
    assert "filelist" not in data["adapters"]


# --- try: read-only, leaves the repo untouched --------------------------------------


def test_try_leaves_the_repo_byte_identical(tmp_path: Path) -> None:
    repo = _copy_tinysoc(tmp_path / "tinysoc")
    before_status = git_status(repo)
    before_tree = file_tree(repo)
    try_run(repo)
    assert git_status(repo) == before_status
    assert file_tree(repo) == before_tree
    # The repo's own `.chipgraph/state` must not have been created by try.
    assert not (repo / ".chipgraph" / "state").exists()


def test_try_disables_local_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_prefixed_repo(tmp_path / "repo")
    git_init(repo)
    monkeypatch.delenv("CHIPGRAPH_LOCAL_PLUGINS", raising=False)
    seen: dict[str, str | None] = {}

    from chipgraph.app.context import AppContext

    real_load = AppContext.load

    def _spy(start: Path, **kwargs: object) -> object:
        seen["env"] = os.environ.get("CHIPGRAPH_LOCAL_PLUGINS")
        return real_load(start, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(AppContext, "load", staticmethod(_spy))
    try_run(repo)
    assert seen["env"] == "0"
    # And it is restored afterwards (was unset -> unset).
    assert os.environ.get("CHIPGRAPH_LOCAL_PLUGINS") is None


def test_try_works_without_the_audit_api(tmp_path: Path) -> None:
    # The assist pack's audit API is not on main yet; try must still succeed and say so.
    repo = make_prefixed_repo(tmp_path / "repo")
    git_init(repo)
    report = try_run(repo)
    assert report.audit_available is False
    assert report.audit_summary == "audit not available yet"


def test_try_from_a_written_draft_profile(tmp_path: Path) -> None:
    repo = make_prefixed_repo(tmp_path / "repo")
    git_init(repo)
    out = tmp_path / "out"
    profile_path, _ = write_draft(learn(repo), out)
    before_tree = file_tree(repo)
    report = try_run(repo, profile_path=profile_path)
    assert report.ingest_ok
    assert file_tree(repo) == before_tree


# --- accept: < 5% of files warn, on tinysoc + two open-source IPs --------------------


def test_accept_tinysoc_under_five_percent(tmp_path: Path) -> None:
    repo = _copy_tinysoc(tmp_path / "tinysoc")
    report = try_run(repo)
    assert report.ingest_ok
    ratio = _files_with_issues_ratio(report, repo)
    assert ratio < 0.05, f"tinysoc: {ratio:.1%} of files warned"


def test_accept_single_ip_repo_under_five_percent(tmp_path: Path) -> None:
    # A fully synthetic single-IP repo so CI does not depend on the QSoC clone.
    repo = make_single_ip_repo(tmp_path / "ip")
    git_init(repo)
    report = try_run(repo)
    ratio = _files_with_issues_ratio(report, repo)
    assert ratio < 0.05, f"single-ip: {ratio:.1%} of files warned"


@pytest.mark.parametrize("which", ["opentitan", "pulp"])
def test_accept_open_source_ip_under_five_percent(tmp_path: Path, which: str) -> None:
    src = opentitan_ip_or_skip() if which == "opentitan" else pulp_ip_or_skip()
    if src is None:
        pytest.skip("QSoC clone not present at /tmp/qsoc-clone")
    repo = copy_ip(src, tmp_path / which)
    before_tree = file_tree(repo)
    report = try_run(repo)
    # Read-only even on a real vendored IP.
    assert file_tree(repo) == before_tree
    ratio = _files_with_issues_ratio(report, repo)
    assert ratio < 0.05, f"{which}: {ratio:.1%} of files warned"
