"""`chipgraph check` runs chip-wide checks once, and per-block checks once per block."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.cli import app

_TINYSOC = Path(__file__).parents[2] / "examples" / "tinysoc"
runner = CliRunner()


def _tinysoc(dest: Path) -> Path:
    shutil.copytree(_TINYSOC, dest)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    result = runner.invoke(app, ["-C", str(dest), "ingest"])
    assert result.exit_code == 0, result.output
    return dest


def test_runs_per_block_follows_the_plugin_and_the_profile(tmp_path: Path) -> None:
    root = _tinysoc(tmp_path / "tinysoc")
    profile = root / ".chipgraph.yml"
    profile.write_text(
        profile.read_text().replace(
            "  duplicate: { use: duplicate }", "  duplicate: { use: duplicate, per_block: true }"
        )
    )
    check_runner = ProfileCheckRunner(AppContext.load(root))
    assert check_runner.runs_per_block("lint") is True  # a cmd tool: default
    assert check_runner.runs_per_block("spec_schema") is True
    assert check_runner.runs_per_block("cross_chip") is False  # chip-wide plugin
    assert check_runner.runs_per_block("duplicate") is True  # profile override wins


def test_chip_wide_check_runs_once_unless_a_block_is_given(tmp_path: Path) -> None:
    root = _tinysoc(tmp_path / "tinysoc")

    once = runner.invoke(app, ["--json", "-C", str(root), "check", "--only", "cross_chip"])
    assert once.exit_code == 0, once.output
    assert len(json.loads(once.output)) == 1

    per_block = runner.invoke(app, ["--json", "-C", str(root), "check", "--only", "spec_schema"])
    assert per_block.exit_code == 0, per_block.output
    assert len(json.loads(per_block.output)) == 3  # timer, gpio, top

    scoped = runner.invoke(
        app, ["-C", str(root), "check", "--only", "cross_chip", "--block", "timer"]
    )
    assert scoped.exit_code == 0, scoped.output
    assert "cross_chip timer" in scoped.output
