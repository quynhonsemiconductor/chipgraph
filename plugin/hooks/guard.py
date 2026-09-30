#!/usr/bin/env python3
"""chipgraph write guard: the plugin's `PreToolUse` hook for runtime claude-code (M1-11).

Claude Code runs this for every tool call (matcher `*`) of the main session and of every
subagent, with the hook JSON on stdin (`tool_name`, `tool_input`, and `agent_id` /
`agent_type` inside a subagent). It reads the engine's task queue under the project's
state directory and decides:

- `get_context` called by a subagent binds that `agent_id` to that task (the task must
  be dispatched; one subagent does one task). The binding is this hook's own file.
- A file-writing tool (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`) is allowed only for
  a subagent bound to a dispatched task, and only on a path in *that* task's
  `allowed_writes`. While any task is dispatched, the main session may not write at all.
- Shell tools are refused to bound subagents and to chipgraph role subagents: roles do
  not get a shell (they do not have it in their tool list either).
- Every tool call of a bound subagent counts against its task's tool-call cap; past the
  cap every call is refused. Claude Code's `--max-turns` does not count subagent turns
  (spike S7), so this is the engine's per-task budget.

Anything else gets no decision (the normal permission flow applies). A project without
chipgraph runtime state is left alone, except that chipgraph role subagents may never
write or use a shell there.

Fail closed: Claude Code lets a call through when a hook exits with a code other than 0
or 2, or when it cannot parse the output, so any error here (bad stdin, a broken state
file, a bug) prints the deny object and exits 2.

Standard library only, and no chipgraph import: it must start fast and work with any
`python3` on PATH (3.9 or newer), with or without the chipgraph package installed. The
file formats are `chipgraph.core.runtime.AgentTaskRecord` and `AgentBinding`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path

try:  # POSIX only; on Windows concurrent hooks are not serialised (documented limit)
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
ROLE_AGENT_PREFIX = "chipgraph:"
SERVER_KEY = "chipgraph"
RUNTIME_DIR = (".chipgraph", "state", "runtime")


class GuardError(Exception):
    """Something the guard cannot decide on: it denies."""


# --- state ------------------------------------------------------------------------------


def project_root(event: dict) -> Path:
    """The project root: `CLAUDE_PROJECT_DIR` (or the hook's cwd), up to the git root."""
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd")
    if not isinstance(raw, str) or not raw:
        raise GuardError("no project directory: CLAUDE_PROJECT_DIR and cwd are unset")
    start = Path(raw).resolve()
    walker = start
    while True:
        if (walker / ".git").exists():
            return walker
        if walker.parent == walker:
            return start
        walker = walker.parent


def _key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_object(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GuardError(f"{path} is not a JSON object")
    return data


def _check_task(path: Path, task: dict) -> dict:
    ok = (
        isinstance(task.get("task_id"), str)
        and isinstance(task.get("status"), str)
        and isinstance(task.get("allowed_writes"), list)
        and all(isinstance(p, str) for p in task["allowed_writes"])
        and isinstance(task.get("dispatches"), int)
        and isinstance(task.get("tool_call_cap"), int)
    )
    if not ok:
        raise GuardError(f"{path} is not a chipgraph task record")
    return task


def load_tasks(tasks_dir: Path) -> dict:
    """Every task record, by task id. A broken file raises (and so denies)."""
    tasks = {}
    for path in sorted(tasks_dir.glob("*.json")):
        task = _check_task(path, _read_object(path))
        tasks[task["task_id"]] = task
    return tasks


def _check_binding(path: Path, binding: dict) -> dict:
    ok = (
        isinstance(binding.get("agent_id"), str)
        and isinstance(binding.get("task_id"), str)
        and isinstance(binding.get("dispatch"), int)
        and isinstance(binding.get("tool_calls"), int)
    )
    if not ok:
        raise GuardError(f"{path} is not a chipgraph agent binding")
    return binding


def load_bindings(agents_dir: Path) -> list:
    if not agents_dir.is_dir():
        return []
    return [_check_binding(p, _read_object(p)) for p in sorted(agents_dir.glob("*.json"))]


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(agents_dir: Path):
    """Serialise binding and counting across concurrent hook processes."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    with (agents_dir / ".lock").open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)


# --- decision ---------------------------------------------------------------------------


def is_get_context(tool: str) -> bool:
    """`mcp__chipgraph__get_context` or the plugin-scoped `mcp__plugin_<p>_chipgraph__...`."""
    parts = tool.split("__")
    if len(parts) != 3 or parts[0] != "mcp" or parts[2] != "get_context":
        return False
    server = parts[1]
    return server == SERVER_KEY or (
        server.startswith("plugin_") and server.endswith("_" + SERVER_KEY)
    )


def _target_path(tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        raise GuardError("tool_input is not an object")
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        raise GuardError("a file-writing tool call has no file_path")
    return raw


def _relative(raw: str, root: Path) -> str | None:
    path = Path(raw.replace("\\", "/"))
    if not path.is_absolute():
        path = root / path
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def decide(event: dict, root: Path) -> tuple:
    """Return `("deny", reason)` or `("none", note)` for one hook event."""
    tool = event.get("tool_name")
    if not isinstance(tool, str) or not tool:
        raise GuardError("hook input has no tool_name")
    agent_id = event.get("agent_id")
    agent_type = event.get("agent_type") or ""
    if agent_id is not None and not isinstance(agent_id, str):
        raise GuardError("agent_id is not a string")
    if not isinstance(agent_type, str):
        raise GuardError("agent_type is not a string")
    agent_id = agent_id or None
    role_agent = agent_type.startswith(ROLE_AGENT_PREFIX)

    runtime = root.joinpath(*RUNTIME_DIR)
    tasks_dir = runtime / "tasks"
    if not tasks_dir.is_dir():
        if role_agent and (tool in WRITE_TOOLS or tool in SHELL_TOOLS):
            return "deny", "no chipgraph task is dispatched in this project"
        return "none", "no chipgraph runtime state"
    tasks = load_tasks(tasks_dir)
    active = sorted(t for t, rec in tasks.items() if rec["status"] == "dispatched")

    if agent_id is None:
        if tool in WRITE_TOOLS and active:
            return "deny", (
                "the main session may not write while chipgraph tasks are dispatched "
                f"({', '.join(active)}); each task's role subagent writes its outputs"
            )
        return "none", "main session"

    agents_dir = runtime / "agents"
    with locked(agents_dir):
        agent_file = agents_dir / f"{_key(agent_id)}.json"
        binding = (
            _check_binding(agent_file, _read_object(agent_file)) if agent_file.is_file() else None
        )

        if is_get_context(tool):
            tool_input = event.get("tool_input")
            task_id = tool_input.get("task_id") if isinstance(tool_input, dict) else None
            if not isinstance(task_id, str) or not task_id:
                return "deny", "get_context needs a task_id"
            task = tasks.get(task_id)
            if task is None or task["status"] != "dispatched":
                return (
                    "deny",
                    f"task {task_id!r} is not dispatched; the main session calls next_task",
                )
            if binding is not None and binding["task_id"] != task_id:
                return "deny", (
                    f"this subagent is bound to task {binding['task_id']!r}; "
                    "one subagent does one task"
                )
            same = binding is not None and binding["dispatch"] == task["dispatches"]
            binding = {
                "schema_version": 1,
                "agent_id": agent_id,
                "agent_type": agent_type,
                "task_id": task_id,
                "dispatch": task["dispatches"],
                "tool_calls": binding["tool_calls"] if same else 0,
            }

        if binding is None:
            if role_agent and tool in SHELL_TOOLS:
                return "deny", "chipgraph role subagents do not get a shell"
            if tool in WRITE_TOOLS and (active or role_agent):
                return "deny", (
                    "this subagent is not bound to a chipgraph task: it must call "
                    "get_context with its task_id first"
                )
            return "none", "unbound subagent"

        binding["tool_calls"] += 1
        write_json(agent_file, binding)
        task_id = binding["task_id"]
        task = tasks.get(task_id)
        current = task is not None and task["status"] == "dispatched"
        if not current or task["dispatches"] != binding["dispatch"]:
            if tool in WRITE_TOOLS or tool in SHELL_TOOLS:
                return "deny", f"task {task_id!r} is no longer dispatched to this subagent"
            return "none", "stale binding, read-only call"
        used = sum(
            b["tool_calls"]
            for b in load_bindings(agents_dir)
            if b["task_id"] == task_id and b["dispatch"] == task["dispatches"]
        )

    cap = task["tool_call_cap"]
    if used > cap:
        return "deny", (
            f"task {task_id!r} used its tool-call budget ({cap} calls); "
            "finish now and report what is left"
        )
    if tool in SHELL_TOOLS:
        return "deny", "chipgraph role subagents do not get a shell"
    if tool in WRITE_TOOLS:
        raw = _target_path(event.get("tool_input"))
        rel = _relative(raw, root)
        if rel is None:
            return "deny", f"{raw} is outside the project"
        if rel not in task["allowed_writes"]:
            return "deny", (
                f"{rel} is not an output of task {task_id!r}; "
                f"this task may write only: {', '.join(task['allowed_writes'])}"
            )
        return "none", f"write to output {rel}"
    return "none", "bound subagent"


# --- entry point ------------------------------------------------------------------------


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"chipgraph: {reason}",
                }
            }
        )
    )


def _log(root: Path, event: dict, decision: str, detail: str) -> None:
    runtime = root.joinpath(*RUNTIME_DIR)
    if not (runtime / "tasks").is_dir():
        return
    record = {
        "tool": event.get("tool_name"),
        "agent_id": event.get("agent_id"),
        "agent_type": event.get("agent_type"),
        "decision": decision,
        "detail": detail,
    }
    with (runtime / "guard.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def main() -> int:
    try:
        event = json.loads(sys.stdin.read())
        if not isinstance(event, dict):
            raise GuardError("hook input is not a JSON object")
        root = project_root(event)
        decision, detail = decide(event, root)
    except Exception as exc:  # fail closed: any error refuses the call
        _deny(f"write guard error, refusing: {exc!r}")
        print(f"chipgraph write guard error: {exc!r}", file=sys.stderr)
        return 2
    with contextlib.suppress(OSError):  # the log is evidence only; it never decides
        _log(root, event, decision, detail)
    if decision == "deny":
        _deny(detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
