"""The MCP server (task M0-14): exposes `status`, `build`, `check`, `approve` and
`config_show` as MCP tools over stdio, for Claude Code or any other MCP client
(DESIGN.md, section on the MCP server; docs/DECISIONS.md D35).

This package builds on `chipgraph.app` and `chipgraph.core`, never the other way
around: core must not import `chipgraph.mcp` (see `pyproject.toml`'s import-linter
contract).
"""

from chipgraph.mcp.server import build_server, run_stdio

__all__ = ["build_server", "run_stdio"]
