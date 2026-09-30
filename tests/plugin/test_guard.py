"""The plugin's write guard (`plugin/hooks/guard.py`) as a real subprocess (M1-11).

Each test writes the engine's task queue with `chipgraph.core.runtime.TaskQueue` (the
same files the MCP server writes), then feeds the guard the hook JSON Claude Code sends
(`tool_name`, `tool_input`, `agent_id`, `agent_type`) on stdin, and reads its decision:
no output means "no decision", a deny object means refused; exit 2 is the fail-closed
path for anything unexpected.

The guard runs with this interpreter; set `CHIPGRAPH_TEST_GUARD_PYTHON` (for example to
a Python 3.9) to check it on the oldest `python3` it supports.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from chipgraph.core.contracts import ArtifactRef, Budget, RuleInstance
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import AgentBinding, TaskQueue
from chipgraph.core.state.layout import StateLayout

PLUGIN = Path(__file__).resolve().parents[2] / "plugin"
GUARD = PLUGIN / "hooks" / "guard.py"
GET_CONTEXT = "mcp__plugin_chipgraph_chipgraph__get_context"
AUTHOR = "chipgraph:author"

GUARD_PYTHON = os.environ.get("CHIPGRAPH_TEST_GUARD_PYTHON", sys.executable)

A = "pulse/a[]"
B = "pulse/b[]"


def _task(rule_id: str, output: str) -> AgentTask:
    instance = RuleInstance(
        rule_id=rule_id,
        outputs=(ArtifactRef(kind="rtl", path=output),),
        instance_id=RuleInstance.make_id(rule_id, {}),
    )
    return AgentTask(
        instance=instance, role="author", allowed_writes=(output,), budget=Budget(tries=2)
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A project with two dispatched tasks: A writes rtl/a.sv, B writes rtl/b.sv."""
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    queue = TaskQueue(StateLayout(project))
    for rule_id, output in (("pulse/a", "rtl/a.sv"), ("pulse/b", "rtl/b.sv")):
        task = _task(rule_id, output)
        queue.enqueue(task, inputs_hash="0", output_hashes={})
        queue.dispatch(task.instance.instance_id, run_id="r1", baseline={})
    return project


def run_guard(
    root: Path, event: dict[str, Any] | str, *, env: dict[str, str] | None = None
) -> tuple[int, dict[str, Any] | None, str]:
    stdin = event if isinstance(event, str) else json.dumps(event)
    full_env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root), **(env or {})}
    proc = subprocess.run(
        [GUARD_PYTHON, str(GUARD)],
        input=stdin,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
        timeout=30,
    )
    out = proc.stdout.strip()
    return proc.returncode, (json.loads(out) if out else None), proc.stderr


def event(
    tool: str,
    *,
    agent: str | None = None,
    agent_type: str = AUTHOR,
    **tool_input: Any,
) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "cwd": "/",
    }
    if agent is not None:
        ev["agent_id"] = agent
        ev["agent_type"] = agent_type
    return ev


def write(root: Path, rel: str, agent: str | None) -> dict[str, Any]:
    return event("Write", agent=agent, file_path=str(root / rel), content="x")


def denied(result: tuple[int, dict[str, Any] | None, str]) -> str:
    code, out, _ = result
    assert code == 0, result
    assert out is not None, "expected a deny"
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    return str(spec["permissionDecisionReason"])


def no_decision(result: tuple[int, dict[str, Any] | None, str]) -> None:
    code, out, _ = result
    assert (code, out) == (0, None), result


def bind(root: Path, agent: str, task_id: str) -> None:
    no_decision(run_guard(root, event(GET_CONTEXT, agent=agent, task_id=task_id)))


# --- binding and per-task writes ------------------------------------------------------


def test_get_context_binds_the_subagent_to_its_task(root: Path) -> None:
    bind(root, "a1", A)
    [binding] = TaskQueue(StateLayout(root)).bindings(A)
    assert isinstance(binding, AgentBinding)
    assert (binding.agent_id, binding.task_id, binding.dispatch) == ("a1", A, 1)
    assert binding.agent_type == AUTHOR
    assert binding.tool_calls == 1


def test_bound_subagent_may_write_its_own_output(root: Path) -> None:
    bind(root, "a1", A)
    no_decision(run_guard(root, write(root, "rtl/a.sv", "a1")))
    no_decision(run_guard(root, event("Edit", agent="a1", file_path=str(root / "rtl/a.sv"))))


def test_write_to_another_tasks_output_by_the_wrong_agent_is_denied(root: Path) -> None:
    bind(root, "a1", A)
    bind(root, "b1", B)
    assert "not an output of task 'pulse/a[]'" in denied(
        run_guard(root, write(root, "rtl/b.sv", "a1"))
    )
    assert "not an output of task 'pulse/b[]'" in denied(
        run_guard(root, write(root, "rtl/a.sv", "b1"))
    )
    no_decision(run_guard(root, write(root, "rtl/b.sv", "b1")))


def test_write_outside_outputs_and_outside_the_project_is_denied(root: Path) -> None:
    bind(root, "a1", A)
    assert "README.md" in denied(run_guard(root, write(root, "README.md", "a1")))
    outside = event("Write", agent="a1", file_path=str(root.parent / "evil.sv"))
    assert "outside the project" in denied(run_guard(root, outside))
    sneaky = event("Write", agent="a1", file_path=str(root / "rtl" / ".." / ".." / "a.sv"))
    assert "outside the project" in denied(run_guard(root, sneaky))
    (root / "link").symlink_to(root.parent)
    via_link = event("Write", agent="a1", file_path=str(root / "link" / "a.sv"))
    assert "outside the project" in denied(run_guard(root, via_link))


def test_main_session_may_not_write_while_tasks_are_dispatched(root: Path) -> None:
    reason = denied(run_guard(root, write(root, "rtl/a.sv", None)))
    assert "main session" in reason
    no_decision(run_guard(root, event("Read", file_path=str(root / "rtl/a.sv"))))
    no_decision(run_guard(root, event(GET_CONTEXT, task_id=A)))  # reading is fine


def test_unbound_subagent_may_not_write(root: Path) -> None:
    assert "get_context" in denied(run_guard(root, write(root, "rtl/a.sv", "x1")))
    other = event("Write", agent="x1", agent_type="general-purpose", file_path=str(root / "a"))
    assert "not bound" in denied(run_guard(root, other))


def test_one_subagent_does_one_task(root: Path) -> None:
    bind(root, "a1", A)
    reason = denied(run_guard(root, event(GET_CONTEXT, agent="a1", task_id=B)))
    assert "bound to task 'pulse/a[]'" in reason


def test_get_context_for_a_task_that_is_not_dispatched_is_denied(root: Path) -> None:
    reason = denied(run_guard(root, event(GET_CONTEXT, agent="a1", task_id="pulse/zzz[]")))
    assert "not dispatched" in reason
    TaskQueue(StateLayout(root)).mark_submitted(B)
    assert "not dispatched" in denied(run_guard(root, event(GET_CONTEXT, agent="b1", task_id=B)))


def test_writes_stop_once_the_task_is_submitted_or_redispatched(root: Path) -> None:
    queue = TaskQueue(StateLayout(root))
    bind(root, "a1", A)
    queue.mark_submitted(A)
    assert "no longer dispatched" in denied(run_guard(root, write(root, "rtl/a.sv", "a1")))

    # Rejected and handed out again: the old subagent's binding is stale.
    from chipgraph.core.contracts import AgentResult

    queue.reject(A, result=AgentResult(status="failed"), reasons=("lint",))
    queue.dispatch(A, run_id="r2", baseline={})
    assert "no longer dispatched" in denied(run_guard(root, write(root, "rtl/a.sv", "a1")))
    bind(root, "a2", A)
    no_decision(run_guard(root, write(root, "rtl/a.sv", "a2")))


def test_the_unscoped_server_name_binds_too(root: Path) -> None:
    no_decision(run_guard(root, event("mcp__chipgraph__get_context", agent="a1", task_id=A)))
    no_decision(run_guard(root, write(root, "rtl/a.sv", "a1")))


# --- shell and budget -----------------------------------------------------------------


def test_bash_is_denied_to_subagents(root: Path) -> None:
    bind(root, "a1", A)
    assert "shell" in denied(
        run_guard(root, event("Bash", agent="a1", command="echo x > rtl/b.sv"))
    )
    assert "shell" in denied(
        run_guard(root, event("Bash", agent="u1", command="ls"))
    )  # unbound role
    no_decision(run_guard(root, event("Bash", command="ls")))  # main session: not ours to decide


def test_tool_call_cap_per_task(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    queue = TaskQueue(StateLayout(project))
    task = _task("pulse/a", "rtl/a.sv")
    queue.enqueue(task, inputs_hash="0", output_hashes={}, tool_call_cap=4)
    queue.dispatch(A, run_id="r1", baseline={})

    bind(project, "a1", A)  # call 1
    no_decision(run_guard(project, event("Read", agent="a1", file_path=str(project / "x"))))
    bind(project, "a2", A)  # call 3: a second subagent on the same task shares the budget
    no_decision(run_guard(project, write(project, "rtl/a.sv", "a2")))  # call 4
    assert "tool-call budget" in denied(
        run_guard(project, event("Read", agent="a1", file_path="/x"))
    )
    assert "tool-call budget" in denied(run_guard(project, write(project, "rtl/a.sv", "a2")))
    assert queue.tool_calls(A) == 6


# --- fail closed ----------------------------------------------------------------------


def _fails_closed(result: tuple[int, dict[str, Any] | None, str]) -> None:
    code, out, err = result
    assert code == 2, result
    assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "write guard error" in err


def test_broken_state_file_denies_with_exit_2(root: Path) -> None:
    tasks_dir = root / ".chipgraph" / "state" / "runtime" / "tasks"
    next(tasks_dir.glob("*.json")).write_text("{not json")
    _fails_closed(run_guard(root, event("Read", agent="a1", file_path="/x")))
    _fails_closed(run_guard(root, write(root, "rtl/a.sv", None)))


def test_broken_binding_denies_with_exit_2(root: Path) -> None:
    bind(root, "a1", A)
    agents_dir = root / ".chipgraph" / "state" / "runtime" / "agents"
    next(agents_dir.glob("*.json")).write_text(json.dumps({"agent_id": "a1"}))
    _fails_closed(run_guard(root, write(root, "rtl/a.sv", "a1")))


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", json.dumps({"tool_input": {}})])
def test_bad_stdin_denies_with_exit_2(root: Path, stdin: str) -> None:
    _fails_closed(run_guard(root, stdin))


def test_write_without_a_path_denies_with_exit_2(root: Path) -> None:
    bind(root, "a1", A)
    _fails_closed(run_guard(root, event("Write", agent="a1", content="x")))


# --- outside a chipgraph run ----------------------------------------------------------


def test_project_without_runtime_state_is_left_alone(tmp_path: Path) -> None:
    project = tmp_path / "plain"
    (project / ".git").mkdir(parents=True)
    no_decision(run_guard(project, write(project, "notes.md", None)))
    no_decision(run_guard(project, write(project, "notes.md", "g1") | {"agent_type": "Explore"}))
    # ...but a chipgraph role subagent never writes without a dispatched task.
    assert "no chipgraph task" in denied(run_guard(project, write(project, "a.sv", "r1")))
    assert not (project / ".chipgraph").exists()


def test_decisions_are_logged(root: Path) -> None:
    bind(root, "a1", A)
    denied(run_guard(root, write(root, "README.md", "a1")))
    log = root / ".chipgraph" / "state" / "runtime" / "guard.log"
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [line["decision"] for line in lines] == ["none", "deny"]
    assert lines[1]["agent_type"] == AUTHOR


# --- plugin wiring --------------------------------------------------------------------


def test_hooks_json_runs_the_guard_on_every_tool() -> None:
    config = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    [group] = config["hooks"]["PreToolUse"]
    assert group["matcher"] == "*"
    [hook] = group["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == "python3"
    assert hook["args"] == ["${CLAUDE_PLUGIN_ROOT}/hooks/guard.py"]


def test_guard_imports_only_the_standard_library() -> None:
    source = GUARD.read_text()
    assert "import chipgraph" not in source and "from chipgraph" not in source
    for line in source.splitlines():
        if line.startswith(("import ", "from ")) and "__future__" not in line:
            module = line.split()[1].split(".")[0]
            assert module in sys.stdlib_module_names, line


def _frontmatter(path: Path) -> dict[str, Any]:
    _, head, _ = path.read_text().split("---", 2)
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data


def test_author_agent_has_no_shell_and_a_model() -> None:
    meta = _frontmatter(PLUGIN / "agents" / "author.md")
    assert meta["name"] == "author"
    tools = [t.strip() for t in meta["tools"].split(",")]
    assert GET_CONTEXT in tools
    assert "Bash" not in tools and "PowerShell" not in tools
    assert {"Read", "Write", "Edit"} <= set(tools)
    assert meta["model"]


def test_run_command_drives_the_loop() -> None:
    meta = _frontmatter(PLUGIN / "commands" / "run.md")
    allowed = [t.strip() for t in meta["allowed-tools"].split(",")]
    assert "mcp__plugin_chipgraph_chipgraph__next_task" in allowed
    assert "mcp__plugin_chipgraph_chipgraph__submit" in allowed
    assert "Bash" not in allowed and "Write" not in allowed
