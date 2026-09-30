"""S7 spike: PreToolUse hook that refuses writes outside the dispatched tasks' outputs.

Claude Code sends the hook JSON on stdin (`tool_name`, `tool_input.file_path` absolute,
`agent_id`/`agent_type` inside a subagent). For a file-writing tool, the path must be
one of `.s7/allowed.json` (written by `next_task`) under the project root; otherwise the
hook answers `permissionDecision: deny`. Every decision is logged to `.s7/hook.log`
(one JSON object per line) as evidence for the spike report.

Fail closed: Claude Code lets a tool call through when a hook exits with a code other
than 0 or 2, so any error here (bad stdin, a broken `allowed.json`, a bug) is caught
and answered with `deny` (exit 0 with the deny JSON, and exit 2 as a second guard).
"""

from __future__ import annotations

import contextlib
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
    loaded = json.loads(allowed_file.read_text()) if allowed_file.is_file() else []
    if not isinstance(loaded, list) or not all(isinstance(x, str) for x in loaded):
        raise ValueError(f"{allowed_file} is not a list of paths")
    allowed = set(loaded)
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return "deny", f"{raw} is outside the project"
    if rel in allowed:
        return "allow", rel
    return "deny", f"{rel} is not an output of a dispatched task (allowed: {sorted(allowed)})"


def _deny(detail: str) -> None:
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


def _log(root: Path, event: dict[str, object], decision: str, detail: str) -> None:
    log = root / ".s7" / "hook.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        record = {
            "tool": event.get("tool_name"),
            "agent_id": event.get("agent_id"),
            "agent_type": event.get("agent_type"),
            "decision": decision,
            "detail": detail,
        }
        fh.write(json.dumps(record) + "\n")


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
        root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or str(event.get("cwd", "."))).resolve()
        decision, detail = decide(event, root)
    except Exception as exc:  # fail closed: any error refuses the write
        _deny(f"write guard error, refusing: {exc!r}")
        print(f"chipgraph write guard error: {exc!r}", file=sys.stderr)
        return 2
    with contextlib.suppress(OSError):  # the log is evidence only; never let it decide
        _log(root, event, decision, detail)
    if decision == "deny":
        _deny(detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
