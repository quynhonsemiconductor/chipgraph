"""Shared fixtures for `chipgraph.app` tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from chipgraph.core.contracts import ArtifactRef, RuleInstance


def init_git(root: Path) -> None:
    """Initialize a git repo at `root` (needed so `AppContext` finds a project root)."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def git_status(root: Path) -> str:
    """`git status --porcelain` output for `root`, for asserting nothing was written."""
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout


def write_profile(root: Path, content: str) -> Path:
    """Write `content` as `root/.chipgraph.yml`."""
    path = root / ".chipgraph.yml"
    path.write_text(content, encoding="utf-8")
    return path


def make_instance(
    rule_id: str = "demo/instance",
    params: dict[str, str] | None = None,
    output_path: str = "out/report.json",
) -> RuleInstance:
    """A throwaway `RuleInstance` for exercising executors and check runners."""
    p = params or {}
    return RuleInstance(
        rule_id=rule_id,
        params=p,
        outputs=(ArtifactRef(kind="report", path=output_path),),
        instance_id=RuleInstance.make_id(rule_id, p),
    )
