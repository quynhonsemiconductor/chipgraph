"""Tests for the `chipgraph ingest` CLI command."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chipgraph.app.context import AppContext
from chipgraph.cli import app

_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


def _copy_tinysoc(dest: Path) -> Path:
    shutil.copytree(_EXAMPLE_ROOT, dest)
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=dest, check=True)
    return dest


@pytest.fixture
def tinysoc(tmp_path: Path) -> Path:
    return _copy_tinysoc(tmp_path / "tinysoc")


def test_ingest_prints_stats_and_exits_zero(tinysoc: Path) -> None:
    result = CliRunner().invoke(app, ["-C", str(tinysoc), "ingest"])
    assert result.exit_code == 0, result.output
    assert "entities:" in result.output
    assert "block:timer" in result.output
    assert "db:" in result.output


def test_ingest_json_has_stats_issues_and_hash(tinysoc: Path) -> None:
    result = CliRunner().invoke(app, ["-C", str(tinysoc), "--json", "ingest"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"db", "build_inputs_hash", "stats", "issues"}
    stats = payload["stats"]
    assert stats["entities"] > 0
    assert stats["entities_by_kind"]["module"] == 3
    assert isinstance(payload["issues"], list)
    assert len(payload["build_inputs_hash"]) == 64


def test_ingest_no_profile_refuses(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    result = CliRunner().invoke(app, ["-C", str(tmp_path), "ingest"])
    assert result.exit_code == 2
    assert "no .chipgraph.yml" in result.output


def test_ingest_strict_exit_code_matches_errors(tinysoc: Path) -> None:
    # tinysoc ingests cleanly (no error issues), so --strict still exits 0.
    result = CliRunner().invoke(app, ["-C", str(tinysoc), "ingest", "--strict"])
    assert result.exit_code == 0, result.output


def test_mcp_model_block_works_against_the_written_store(tinysoc: Path) -> None:
    import asyncio

    from chipgraph.mcp.model_tools import model_block

    result = CliRunner().invoke(app, ["-C", str(tinysoc), "ingest"])
    assert result.exit_code == 0, result.output

    ctx = AppContext.load(tinysoc)
    payload = asyncio.run(model_block(ctx, "timer"))
    assert payload["key"] == "block:timer"
    assert "module:tiny_timer" in payload["modules"]
