"""Shared fixtures for CLI tests."""

from __future__ import annotations

import subprocess
from pathlib import Path


def init_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def git_status(root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout


def write_profile(root: Path, content: str) -> Path:
    path = root / ".chipgraph.yml"
    path.write_text(content, encoding="utf-8")
    return path
