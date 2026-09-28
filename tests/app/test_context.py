"""Tests for `chipgraph.app.context.AppContext`."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git_status, init_git, write_profile

from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError


def test_load_without_profile_is_read_only(tmp_path: Path) -> None:
    init_git(tmp_path)
    before = git_status(tmp_path)

    ctx = AppContext.load(tmp_path)

    assert ctx.resolved is None
    assert ctx.profile is None
    assert ctx.backend is None
    assert git_status(tmp_path) == before == ""


def test_load_without_profile_require_profile_raises(tmp_path: Path) -> None:
    ctx = AppContext.load(tmp_path)
    with pytest.raises(AppError, match="chipgraph init"):
        ctx.require_profile()


def test_load_with_profile_prepares_state(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")

    ctx = AppContext.load(tmp_path)

    assert ctx.resolved is not None
    assert ctx.profile is not None
    assert ctx.profile.project == "demo"
    assert ctx.backend is not None
    assert ctx.layout.state_dir.is_dir()
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/.chipgraph/state/" in exclude


def test_load_labels_nda_paths_from_profile_data(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(
        tmp_path,
        "project: demo\ndata:\n  default: internal\n  nda_paths: ['secret/**']\n",
    )

    ctx = AppContext.load(tmp_path)

    assert ctx.store.labels.label_for("secret/design.sv") == "nda"
    assert ctx.store.labels.label_for("design/design.sv") == "internal"


def test_load_explicit_profile_path_outside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git(repo)
    external = tmp_path / "shared" / "external.chipgraph.yml"
    external.parent.mkdir()
    external.write_text("project: external\n", encoding="utf-8")

    ctx = AppContext.load(repo, profile_path=external)

    assert ctx.root == repo
    assert ctx.profile is not None
    assert ctx.profile.project == "external"


def test_load_bad_profile_raises_app_error(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: [not, a, string]\n")
    with pytest.raises(AppError):
        AppContext.load(tmp_path)
