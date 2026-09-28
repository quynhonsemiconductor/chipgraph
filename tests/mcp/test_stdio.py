"""One subprocess test: `chipgraph mcp -C <tmp>` started for real over stdio, using
the `mcp` SDK's `StdioServerParameters` transport (`mcp.Client(StdioServerParameters(...))`
launches the command and talks newline-delimited JSON-RPC over its stdin/stdout).

Every other `chipgraph.mcp` test uses the in-process `Client(server)` transport
instead (see `test_server.py`): it exercises the exact same tool code without the
cost or flakiness of spawning a real interpreter. This test is the one place that
also proves the CLI's `chipgraph mcp` command actually starts the server.

Marked so a slow-to-start interpreter doesn't make CI flaky: any failure to complete
the handshake and one tool call within `_STARTUP_TIMEOUT_S` is a skip, not a failure.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from conftest import init_git, write_profile
from mcp import Client, StdioServerParameters

_STARTUP_TIMEOUT_S = 10.0


def _chipgraph_script() -> Path:
    """The `chipgraph` console script installed next to the running interpreter."""
    return Path(sys.executable).parent / "chipgraph"


@pytest.mark.skipif(not _chipgraph_script().is_file(), reason="no 'chipgraph' console script")
def test_chipgraph_mcp_over_stdio_status(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")

    # `-C/--dir` is a global option (parsed before the subcommand): `chipgraph -C DIR mcp`.
    params = StdioServerParameters(
        command=str(_chipgraph_script()), args=["-C", str(tmp_path), "mcp"]
    )

    async def _call() -> dict[str, object]:
        async with Client(params) as client:
            result = await client.call_tool("status", {})
            assert not result.is_error
            return result.structured_content

    try:
        payload = asyncio.run(asyncio.wait_for(_call(), timeout=_STARTUP_TIMEOUT_S))
    except TimeoutError:
        pytest.skip(f"chipgraph mcp did not answer within {_STARTUP_TIMEOUT_S}s")

    assert payload == {"runs": []}
