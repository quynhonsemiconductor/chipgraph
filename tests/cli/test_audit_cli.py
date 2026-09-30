"""CLI tests for `chipgraph audit` (task M1-20).

Runs the real CLI (`typer.testing.CliRunner` over `chipgraph.cli:app`) against a fresh
git copy of `examples/tinysoc` in `tmp_path`. The lint case needs verilator, so those
tests are skipped (not failed) when it is missing, as the e2e suite does. `--strict` exit
codes and a clean `git status` after an audit are covered here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chipgraph.cli import app

runner = CliRunner()

_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"

_verilator_missing = shutil.which("verilator") is None or shutil.which("make") is None
requires_eda = pytest.mark.skipif(
    _verilator_missing, reason="verilator and/or make not found on PATH"
)


def _copy_tinysoc(dest: Path) -> Path:
    shutil.copytree(_EXAMPLE_ROOT, dest)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=dest, check=True)
    return dest


def _git_status(root: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _seed_layer1(root: Path) -> None:
    path = root / "chip.yml"
    path.write_text(path.read_text().replace("    base: 0x4", "    base: 0x0"), encoding="utf-8")


def _seed_layer5(root: Path) -> None:
    path = root / "dv" / "test_tiny_timer.py"
    path.write_text(path.read_text().replace("REQ-TIM-001", "REQ-TIM-00X", 1), encoding="utf-8")


def _invoke(root: Path, *args: str, json_output: bool = False) -> object:
    cli_args = (["--json"] if json_output else []) + ["-C", str(root), "audit", *args]
    return runner.invoke(app, cli_args)


# --- clean project: exit 0 even with --strict ---------------------------------------


@requires_eda
def test_clean_audit_exits_zero(tmp_path: Path) -> None:
    root = _copy_tinysoc(tmp_path / "tinysoc")
    result = _invoke(root, "--strict")
    assert result.exit_code == 0, result.output
    assert "open errors: none" in result.output
    assert "blocking: 0" in result.output
    # State-only: the audit wrote nothing to the working tree.
    assert _git_status(root) == ""


# --- seeded errors: text report groups by layer -------------------------------------


def test_seeded_layer1_and_5_grouped(tmp_path: Path) -> None:
    """No verilator needed: layers 1 and 5 come from model-based checks."""
    root = _copy_tinysoc(tmp_path / "tinysoc")
    _seed_layer1(root)
    _seed_layer5(root)

    result = _invoke(root)
    assert result.exit_code == 0, result.output  # no --strict: findings do not fail the run
    assert "layer 1:" in result.output
    assert "layer 5:" in result.output
    assert "cross_chip" in result.output
    assert "trace" in result.output
    assert "chip.yml" in result.output


# --- --strict exit codes ------------------------------------------------------------


def test_strict_exits_one_on_blocking(tmp_path: Path) -> None:
    root = _copy_tinysoc(tmp_path / "tinysoc")
    _seed_layer1(root)  # a layer-1 error blocks

    assert _invoke(root).exit_code == 0  # without --strict, exit 0
    result = _invoke(root, "--strict")
    assert result.exit_code == 1, result.output
    assert _git_status(root) == "" or ".chipgraph" not in _git_status(root)


# --- --json ------------------------------------------------------------------------


def test_json_output(tmp_path: Path) -> None:
    root = _copy_tinysoc(tmp_path / "tinysoc")
    _seed_layer1(root)

    result = _invoke(root, json_output=True)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["blocking"] == 1
    layers = {entry["layer"] for entry in payload["layers"]}
    assert 1 in layers
    assert payload["open_errors_by_layer"]["1"] == 1


# --- --no-ingest reuses the cached model --------------------------------------------


def test_no_ingest_reports_no_ingest_section(tmp_path: Path) -> None:
    root = _copy_tinysoc(tmp_path / "tinysoc")
    # First a full audit builds the model cache; then --no-ingest reuses it.
    assert _invoke(root).exit_code == 0
    result = _invoke(root, "--no-ingest", json_output=True)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ingest"]["ran"] is False
