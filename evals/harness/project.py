"""A fresh tinysoc copy to run a suite in, and the dev plugin for headless Claude Code."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .suites import PLUGIN, REPO, TINYSOC, Suite

LOCAL_OUTPUT = (".chipgraph/state", "build", "obj_dir")
"""Run output a local tinysoc may hold; never copied."""


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=eval@example.invalid", "-c", "user.name=eval", *args],
        cwd=project,
        check=True,
        capture_output=True,
    )


def copy_tinysoc(dest: Path, suite: Suite, *, tiers: dict[str, str] | None = None) -> Path:
    """examples/tinysoc at `dest`, as its own git repo, ingested, with the suite's logs.

    `tiers` (triage with a real model) are written into the copy's profile as
    `models.tiers`, the decider's models. A triage suite's sample logs are copied to
    `dest/logs/` (only the logs: the labels stay in the suite's YAML file).
    """
    from chipgraph.app.context import AppContext
    from chipgraph.app.ingest import run_ingest

    shutil.copytree(TINYSOC, dest)
    for rel in LOCAL_OUTPUT:
        shutil.rmtree(dest / rel, ignore_errors=True)
    if tiers:
        lines = ["", "# Added by chipgraph eval: the models of the decider tiers.", "models:"]
        lines += ["  tiers:", *(f"    {tier}: {model}" for tier, model in tiers.items())]
        with (dest / ".chipgraph.yml").open("a", encoding="utf-8") as profile:
            profile.write("\n".join(lines) + "\n")
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "tinysoc")
    run_ingest(AppContext.load(dest))
    if suite.kind == "triage" and suite.logs is not None:
        (dest / "logs").mkdir()
        for log in sorted(suite.logs.glob("*.log")):
            shutil.copy2(log, dest / "logs" / log.name)
    return dest


def dev_plugin(dest: Path, repo: Path = REPO) -> Path:
    """plugin/ at `dest`, with a `.mcp.json` that runs this checkout instead of PyPI."""
    shutil.copytree(PLUGIN, dest)
    server = {
        "command": "uv",
        "args": ["run", "--project", str(repo), "chipgraph", "-C", "${CLAUDE_PROJECT_DIR}", "mcp"],
    }
    (dest / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"chipgraph": server}}, indent=2) + "\n", encoding="utf-8"
    )
    return dest


__all__ = ["copy_tinysoc", "dev_plugin"]
