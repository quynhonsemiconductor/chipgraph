"""Runtime `claude-code` (DESIGN.md 5.5, D35, spike S7).

- `runtime.ClaudeCodeRuntime`: the `AgentRuntime` a build uses; it queues agent tasks.
  `runtime.ClaudeCodeExecutor` is the build's executor over it.
- `loop`: the M2-02a agent rule loop applied to one submitted attempt (label, budget,
  redo instruction, the dispatch bound).
- `service`: what the MCP tools `next_task`, `get_context` and `submit` do.
- The Claude Code plugin (`plugin/` in the repo) holds the loop command, one subagent
  per role, and the write guard hook.

`service` imports `chipgraph.app` (the build, the check runner), so it is not imported
here: `chipgraph.app.build` imports this package for the runtime.
"""

from chipgraph.adapters.runtime.claude_code.runtime import (
    PLUGIN_NAME,
    RUN_COMMAND,
    ClaudeCodeExecutor,
    ClaudeCodeRuntime,
)

__all__ = ["PLUGIN_NAME", "RUN_COMMAND", "ClaudeCodeExecutor", "ClaudeCodeRuntime"]
