"""Runtime `claude-code` (DESIGN.md 5.5, D35, spike S7).

- `runtime.ClaudeCodeRuntime`: the `AgentRuntime` a build uses; it queues agent tasks.
- `service`: what the MCP tools `next_task`, `get_context` and `submit` do.
- The Claude Code plugin (`plugin/` in the repo) holds the loop command, one subagent
  per role, and the write guard hook.

`service` imports `chipgraph.app` (the build, the check runner), so it is not imported
here: `chipgraph.app.build` imports this package for the runtime.
"""

from chipgraph.adapters.runtime.claude_code.runtime import (
    PLUGIN_NAME,
    RUN_COMMAND,
    ClaudeCodeRuntime,
)

__all__ = ["PLUGIN_NAME", "RUN_COMMAND", "ClaudeCodeRuntime"]
