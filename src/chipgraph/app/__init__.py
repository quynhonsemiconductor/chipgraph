"""The application layer: wires `chipgraph.core` to `chipgraph.adapters` and
`chipgraph.checks` for a concrete project, on behalf of the CLI (and, later, the MCP
server). Core must not import this package (see `pyproject.toml`'s import-linter
contract): this layer is where the engine actually meets tools, checks and a project's
`.chipgraph.yml`.
"""

from chipgraph.app.errors import AppError

__all__ = ["AppError"]
