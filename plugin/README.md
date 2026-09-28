# chipgraph plugin

A Claude Code plugin that runs the `chipgraph` MCP server inside your own Claude Code
session — no separate API key, per D35 (`docs/DECISIONS.md`).

## Install

```
/plugin marketplace add quynhonsemiconductor/chipgraph
/plugin install chipgraph@chipgraph
```

(the install id is `<plugin name>@<marketplace name>`; both are `chipgraph` here.)

## What it gives you today

The MCP server `chipgraph` exposes the five tools shipped in M0-14:

- `status` — current build state
- `build` — run/resume the build graph
- `check` — run the deterministic EDA checks for one node
- `approve` — approve a human gate
- `config_show` — show the resolved project config

Agents, skills, hooks and slash commands are not part of this plugin yet; they land in
M1.

## Requirements

- `uv` (and therefore `uvx`) on `PATH`. The plugin launches the server with
  `uvx chipgraph@0.0.1 -C ${CLAUDE_PROJECT_DIR} mcp`, which downloads and caches
  `chipgraph` from PyPI on first use.
- `${CLAUDE_PROJECT_DIR}` is the project root Claude Code is running in; Claude Code
  substitutes it into the server's `args` before launch (see
  https://code.claude.com/docs/en/plugins/manifest-reference#environment-variables).
  Without it, a plugin MCP server's own working directory defaults to the plugin's
  install directory, not your project — so `-C` is required here, not optional.

## Local development (no PyPI)

Point the plugin at your checkout instead of PyPI by editing `.mcp.json` (or adding a
project-level `.mcp.json` that overrides it) to:

```json
{
  "mcpServers": {
    "chipgraph": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/chipgraph", "chipgraph", "-C", "${CLAUDE_PROJECT_DIR}", "mcp"]
    }
  }
}
```

See `docs/spikes/S5.md` for how the MCP server is started and tested.

## Version

The plugin, its bundled `.mcp.json` and `pyproject.toml` are kept at the same version
(currently `0.0.1`). `tests/test_release_metadata.py` checks they agree. See
`docs/RELEASING.md` for how a release is cut and how the version is bumped everywhere.
