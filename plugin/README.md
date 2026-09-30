# chipgraph plugin

A Claude Code plugin that runs the `chipgraph` MCP server inside your own Claude Code
session and runs chipgraph's agent tasks there, with your own Claude plan: no separate
API key, per D35 (`docs/DECISIONS.md`).

## Install

```
/plugin marketplace add quynhonsemiconductor/chipgraph
/plugin install chipgraph@chipgraph
```

(the install id is `<plugin name>@<marketplace name>`; both are `chipgraph` here.)

## What it gives you

**MCP server `chipgraph`** (`.mcp.json`): `status`, `build`, `check`, `approve`,
`config_show`, `audit`, the `model_*` queries, and the three tools of runtime
`claude-code` (M1-11):

- `next_task(target)`: runs the build; every agent rule it reaches becomes a task; it
  hands out the ready ones (each with `task_id`, `agent`, `model`, `outputs`), or says
  `done`, or `waiting` (a gate to approve, a question for a person, a failure).
- `get_context(task_id)`: the task's inputs as text, its outputs (the only files it may
  write), role, skills and checks. Refused when an input is labelled `nda`: every model
  here is a cloud model.
- `submit(task_id, result)`: the engine checks that only the task's outputs changed
  since it was handed out, that they exist, and runs the rule's checks; then accepts,
  or rejects with reasons and counts a try (`budget.tries`).

**Command `/chipgraph:run [target]`** (`commands/run.md`): the loop. `next_task` → one
role subagent per task, all in parallel → `submit` each → repeat until `done` or
`waiting`. The main session never writes files itself.

**Role subagents** (`agents/`): one per role, with its tools and model. Today:
`chipgraph:author` (DESIGN 5.1) with `Read, Write, Edit, Glob, Grep` and `get_context`,
no shell. `next_task` picks the model from the rule's tier (`small` → haiku, `medium` →
sonnet, `large` → opus, or the profile's `models.tiers`), and the loop passes it on.

**Write guard** (`hooks/hooks.json` → `hooks/guard.py`, a `PreToolUse` hook on every
tool). It lives in the plugin's hooks, not in agent frontmatter, because frontmatter
hooks do not run under `claude -p` (spike S7). While chipgraph tasks are dispatched:

- a subagent is bound to a task when it calls `get_context` (one subagent, one task);
- a bound subagent may write only its own task's `outputs`; any other path, another
  task's outputs, or anything outside the project is refused;
- the main session may not write at all; subagents get no shell;
- each task has a tool-call budget (80 per dispatch): `--max-turns` counts only the main
  session, so the engine counts subagent calls itself;
- **fail closed**: any error (bad input, broken state) denies, with exit code 2.

Outside a chipgraph run the guard makes no decision, except that `chipgraph:*` role
subagents never write or use a shell. Its state and log are under
`.chipgraph/state/runtime/` (gitignored, never in the repo tree).

**Tokens and cost** come from the final `result` event of `claude -p` (`usage`,
`total_cost_usd`, `modelUsage` per model, so per role); OpenTelemetry is not needed.

## Requirements

- `uv` (and therefore `uvx`) on `PATH`. The plugin launches the server with
  `uvx chipgraph@0.0.1 -C ${CLAUDE_PROJECT_DIR} mcp`, which downloads and caches
  `chipgraph` from PyPI on first use. (The runtime tools arrive with the next release;
  until then use a local checkout, below.)
- `python3` (3.9 or newer) on `PATH` for the write guard. It uses only the standard
  library and does not need chipgraph installed. If `python3` is missing, Claude Code
  cannot start the hook and lets the call through: `submit` still rejects any change
  outside a task's outputs, but the write is not stopped when it happens.
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

and start Claude Code with `claude --plugin-dir /path/to/that/plugin`. See
`docs/spikes/S5.md` for how the MCP server is started and tested.

## Try it on tinysoc (the M1-11 acceptance run)

`docs/runtime-claude-code/run.sh` builds `/tmp/cg-cc-<name>/tinysoc` (tinysoc plus the
test fixture pack `pulse`: one agent rule that writes `rtl/tiny_pulse.sv`, checked by
Verilator lint, then a `gen` rule that depends on it), copies this plugin next to it
with a `.mcp.json` pointing at the checkout, and runs one headless session with your
logged-in Claude Code (it unsets `ANTHROPIC_API_KEY`):

```bash
MAIN_MODEL=opus docs/runtime-claude-code/run.sh /tmp/cg-cc-opus
```

That is `claude -p "/chipgraph:run pulse/pulse_manifest" --plugin-dir <copy> --model opus
--permission-mode acceptEdits --output-format stream-json --verbose` in the project
copy; the subagent runs on haiku (the rule's tier `small`). Afterwards the script runs
`chipgraph build pulse/pulse_manifest` again and prints `report.py`'s summary: the
tool calls, the `Agent` calls with their model, the task queue, the guard's decisions
per subagent, `git status` of the copy, cost and `modelUsage`, and a verdict (task
accepted, build finished, only the output changed). If the plugin is also installed
from the marketplace, disable that copy for the run.

## Version

The plugin, its bundled `.mcp.json` and `pyproject.toml` are kept at the same version
(currently `0.0.1`). `tests/test_release_metadata.py` checks they agree. See
`docs/RELEASING.md` for how a release is cut and how the version is bumped everywhere.
