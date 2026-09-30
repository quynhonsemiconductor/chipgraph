"""Tests for the `audit` MCP tool (task M1-20).

Uses the `mcp` SDK's in-process `Client` (as `tests/mcp/test_server.py` does) to call the
`audit` tool against a git copy of `examples/tinysoc` in `tmp_path`, and checks it returns
the same JSON report the CLI prints. The tool needs no verilator for layers 1 and 5
(model-based checks); it is only exercised on those layers here.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from mcp import Client

from chipgraph.mcp.server import build_server

_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


def _copy_tinysoc(dest: Path) -> Path:
    shutil.copytree(_EXAMPLE_ROOT, dest)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=dest, check=True)
    return dest


def _seed_layer1(root: Path) -> None:
    path = root / "chip.yml"
    path.write_text(path.read_text().replace("    base: 0x4", "    base: 0x0"), encoding="utf-8")


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _call_audit(server: object, args: dict[str, object]) -> dict[str, object]:
    async def _call() -> dict[str, object]:
        async with Client(server) as client:  # type: ignore[arg-type]
            result = await client.call_tool("audit", args)
            assert not result.is_error, result.content
            assert isinstance(result.structured_content, dict)
            return result.structured_content

    return _run(_call())  # type: ignore[return-value]


def test_audit_tool_reports_layer1_finding(tmp_path: Path) -> None:
    root = _copy_tinysoc(tmp_path / "tinysoc")
    _seed_layer1(root)
    server = build_server(root)

    payload = _call_audit(server, {})
    assert payload["schema_version"] == 1
    assert payload["blocking"] == 1
    layers = {entry["layer"] for entry in payload["layers"]}  # type: ignore[attr-defined]
    assert 1 in layers
    assert payload["open_errors_by_layer"]["1"] == 1  # type: ignore[index]
    # A layer-1 cross_chip finding, with file evidence.
    layer1 = next(e for e in payload["layers"] if e["layer"] == 1)  # type: ignore[index]
    finding = next(f for f in layer1["findings"] if "cross_chip" in f["source"])
    assert finding["evidence"].startswith("chip.yml")
    assert finding["blocking"] is True


def test_audit_tool_clean_project_has_no_blocking(tmp_path: Path) -> None:
    root = _copy_tinysoc(tmp_path / "tinysoc")
    server = build_server(root)

    payload = _call_audit(server, {})
    assert payload["blocking"] == 0
    assert payload["open_errors_by_layer"] == {}


def test_audit_tool_no_ingest(tmp_path: Path) -> None:
    root = _copy_tinysoc(tmp_path / "tinysoc")
    server = build_server(root)
    # Build the model cache once, then call with ingest disabled.
    _call_audit(server, {})
    payload = _call_audit(server, {"ingest": False})
    assert payload["ingest"]["ran"] is False  # type: ignore[index]
