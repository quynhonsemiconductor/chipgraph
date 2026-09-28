"""Tests for `chipgraph.core.state.backend`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from chipgraph.core.state.backend import LocalBackend, backend_from_config
from chipgraph.core.state.lock import BlockLock

GIT_AVAILABLE = shutil.which("git") is not None


def test_prepare_creates_state_dir_without_git(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path)
    backend.prepare()
    assert backend.layout.state_dir.is_dir()
    # No .git here, so nothing to update; prepare() must not error or create .git.
    assert not (tmp_path / ".git").exists()


def test_prepare_adds_single_exclude_line_and_is_idempotent(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    backend = LocalBackend(tmp_path)
    backend.prepare()
    backend.prepare()

    exclude_path = git_dir / "info" / "exclude"
    content = exclude_path.read_text(encoding="utf-8")
    assert content.count("/.chipgraph/state/") == 1


def test_prepare_appends_to_existing_exclude_file(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "info").mkdir(parents=True)
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.write_text("*.orig\n", encoding="utf-8")

    LocalBackend(tmp_path).prepare()

    content = exclude_path.read_text(encoding="utf-8")
    assert "*.orig" in content
    assert "/.chipgraph/state/" in content


def test_prepare_worktree_git_file(tmp_path: Path) -> None:
    # Simulate a linked worktree: <root>/.git is a file pointing at a gitdir under the
    # main repo's .git/worktrees/<name>, which has a commondir file pointing back at the
    # real common git dir.
    main_git = tmp_path / "main_repo" / ".git"
    main_git.mkdir(parents=True)
    worktree_gitdir = main_git / "worktrees" / "wt1"
    worktree_gitdir.mkdir(parents=True)
    (worktree_gitdir / "commondir").write_text("../..\n", encoding="utf-8")

    project_root = tmp_path / "worktree_checkout"
    project_root.mkdir()
    (project_root / ".git").write_text(f"gitdir: {worktree_gitdir}\n", encoding="utf-8")

    LocalBackend(project_root).prepare()

    exclude_path = main_git / "info" / "exclude"
    assert exclude_path.exists()
    assert "/.chipgraph/state/" in exclude_path.read_text(encoding="utf-8")


def test_backend_from_config_local(tmp_path: Path) -> None:
    backend = backend_from_config("local", tmp_path)
    assert isinstance(backend, LocalBackend)
    assert backend.name == "local"


def test_backend_from_config_branch_not_implemented(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError):
        backend_from_config("branch:chipgraph-state", tmp_path)


def test_backend_from_config_repo_not_implemented(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError):
        backend_from_config("repo:git@example.com/x-state.git", tmp_path)


def test_backend_from_config_unknown_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown state backend"):
        backend_from_config("s3://bucket", tmp_path)


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not installed")
def test_running_under_real_git_repo_creates_no_tracked_file(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    backend = LocalBackend(tmp_path)
    backend.prepare()

    # Write some state, plus exercise the lock and journal paths under it.
    layout = backend.layout
    layout.runs_dir.mkdir(parents=True, exist_ok=True)
    (layout.runs_dir / "some-run").mkdir()
    (layout.runs_dir / "some-run" / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    with BlockLock(layout, block="timer", owner="run-1"):
        pass

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    assert status.stdout.strip() == ""
