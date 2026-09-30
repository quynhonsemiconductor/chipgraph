"""S7 spike: PreToolUse hook that refuses writes outside the dispatched tasks' outputs.

Claude Code sends the hook JSON on stdin (`tool_name`, `tool_input.file_path` absolute,
`agent_id`/`agent_type` inside a subagent). For a file-writing tool, the path must be
one of `.s7/allowed.json` (written by `next_task`) under the project root; otherwise the
hook answers `permissionDecision: deny`. Every decision is logged to `.s7/hook.log`
(one JSON object per line) as evidence for the spike report.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def decide(event: dict[str, object], root: Path) -> tuple[str, str]:
    tool = str(event.get("tool_name", ""))
    if tool not in WRITE_TOOLS:
        return "none", ""
    tool_input = event.get("tool_input")
    raw = ""
    if isinstance(tool_input, dict):
        raw = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    path = Path(raw.replace("\\", "/")).resolve()
    allowed_file = root / ".s7" / "allowed.json"
    allowed = set(json.loads(allowed_file.read_text())) if allowed_file.is_file() else set()
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return "deny", f"{raw} is outside the project"
    if rel in allowed:
        return "allow", rel
    return "deny", f"{rel} is not an output of a dispatched task (allowed: {sorted(allowed)})"


def main() -> int:
    event = json.loads(sys.stdin.read() or "{}")
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or str(event.get("cwd", "."))).resolve()
    decision, detail = decide(event, root)
    log = root / ".s7" / "hook.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "tool": event.get("tool_name"),
                    "agent_id": event.get("agent_id"),
                    "agent_type": event.get("agent_type"),
                    "decision": decision,
                    "detail": detail,
                }
            )
            + "\n"
        )
    if decision == "deny":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"chipgraph: {detail}",
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
