"""Tests for the git VCS adapter."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from chipgraph.adapters.vcs.git import GitVcs, VcsError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "committed.txt").write_text("v1\n")
    _git(root, "add", "committed.txt")
    _git(root, "commit", "-q", "-m", "initial")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    return head


def test_head_returns_current_sha(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    vcs = GitVcs()
    assert vcs.head(tmp_path) == head


def test_changed_files_detects_all_kinds(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "committed.txt").write_text("v2\n")  # modified
    (tmp_path / "added.txt").write_text("new\n")
    _git(tmp_path, "add", "added.txt")
    (tmp_path / "untracked.txt").write_text("untracked\n")

    vcs = GitVcs()
    changed = vcs.changed_files(tmp_path)
    assert set(changed) >= {"committed.txt", "added.txt", "untracked.txt"}
    assert changed == tuple(sorted(set(changed)))


def test_changed_files_detects_deleted(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "committed.txt").unlink()

    vcs = GitVcs()
    changed = vcs.changed_files(tmp_path)
    assert "committed.txt" in changed


def test_changed_files_with_since(tmp_path: Path) -> None:
    head1 = _init_repo(tmp_path)
    (tmp_path / "second.txt").write_text("second\n")
    _git(tmp_path, "add", "second.txt")
    _git(tmp_path, "commit", "-q", "-m", "second")

    vcs = GitVcs()
    changed = vcs.changed_files(tmp_path, since=head1)
    assert "second.txt" in changed


def test_changed_files_ignores_ignored_files(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-q", "-m", "add gitignore")
    (tmp_path / "ignored.txt").write_text("secret\n")

    vcs = GitVcs()
    changed = vcs.changed_files(tmp_path)
    assert "ignored.txt" not in changed


def test_create_and_remove_workspace(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    vcs = GitVcs()

    workspace = vcs.create_workspace(tmp_path, "wt-one")
    assert workspace.is_dir()
    assert (workspace / "committed.txt").read_text() == "v1\n"

    listing = _git(tmp_path, "worktree", "list").stdout
    assert str(workspace) in listing

    vcs.remove_workspace(workspace)
    assert not workspace.exists()
    listing_after = _git(tmp_path, "worktree", "list").stdout
    assert str(workspace) not in listing_after


def test_create_workspace_custom_dir(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspaces_dir = tmp_path.parent / "custom-workspaces"
    vcs = GitVcs(workspaces_dir=workspaces_dir)

    workspace = vcs.create_workspace(tmp_path, "wt-custom")
    assert workspace == workspaces_dir / "wt-custom"
    assert workspace.is_dir()

    vcs.remove_workspace(workspace)


def test_create_workspace_rejects_bad_name(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    vcs = GitVcs()
    with pytest.raises(VcsError):
        vcs.create_workspace(tmp_path, "Bad Name!")


def test_create_workspace_rejects_existing_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    vcs = GitVcs()
    workspace = vcs.create_workspace(tmp_path, "wt-dup")
    with pytest.raises(VcsError):
        vcs.create_workspace(tmp_path, "wt-dup")
    vcs.remove_workspace(workspace)


def test_failing_command_raises_vcs_error(tmp_path: Path) -> None:
    vcs = GitVcs()
    with pytest.raises(VcsError):
        vcs.head(tmp_path)  # not a git repo
