"""M2-01: the guard's read rules (`plugin/hooks/guard.py`), as a real subprocess.

A `tb-author` task is dispatched with `denied_reads` (its role's read policy over the
project: the rtl artifacts and their directory). Two kinds of subagent are bound to it:

- `chipgraph:tb-author` itself: it has no read tools, and the guard refuses every
  read-type tool to it whatever the path, and every chipgraph tool but `get_context`;
- a subagent type that does have read tools (`chipgraph:critic` here, standing in for
  any role with a deny policy and read tools): the path rules apply: a read inside a
  denied path, a Grep/Glob rooted at an ancestor of one, a non-chipgraph tool naming
  one, are refused; a read of a spec file still passes.

The guard's matchers are also checked against `chipgraph.core.runtime.roles.policy` on
the same cases, so the engine and the hook agree.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from chipgraph.adapters.runtime.claude_code.agents import CAPABILITY_TOOLS, MCP_TOOL_PREFIX
from chipgraph.adapters.runtime.claude_code.agents import agent_type as agent_type_for
from chipgraph.core.contracts import ArtifactRef, Budget, RuleInstance
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.runtime.roles import covers_denied, list_roles, path_denied, static_prefix
from chipgraph.core.state.layout import StateLayout

PLUGIN = Path(__file__).resolve().parents[2] / "plugin"
GUARD = PLUGIN / "hooks" / "guard.py"
GUARD_PYTHON = os.environ.get("CHIPGRAPH_TEST_GUARD_PYTHON", sys.executable)

GET_CONTEXT = f"{MCP_TOOL_PREFIX}get_context"
TB = "dv/tb[]"
AUTHOR = "dv/notes[]"
TB_OUTPUT = "dv/test_gpio.py"
AUTHOR_OUTPUT = "doc/notes.md"
DENIED = ("rtl/**", "rtl/tiny_gpio.sv", "rtl/tiny_timer.sv")


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("chipgraph_plugin_guard", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _task(rule_id: str, role: str, output: str) -> AgentTask:
    instance = RuleInstance(
        rule_id=rule_id,
        outputs=(ArtifactRef(kind="other", path=output),),
        instance_id=RuleInstance.make_id(rule_id, {}),
    )
    return AgentTask(instance=instance, role=role, allowed_writes=(output,), budget=Budget())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A project: rtl/ and doc/ files, a dispatched tb-author task and an author task."""
    project = (tmp_path / "proj").resolve()
    (project / ".git").mkdir(parents=True)
    (project / "rtl").mkdir()
    (project / "rtl" / "tiny_gpio.sv").write_text("module tiny_gpio; endmodule\n")
    (project / "doc" / "specs").mkdir(parents=True)
    (project / "doc" / "specs" / "A.md").write_text("# spec\n")
    (project / "hwlink").symlink_to(project / "rtl", target_is_directory=True)
    queue = TaskQueue(StateLayout(project))
    tb = _task("dv/tb", "tb-author", TB_OUTPUT)
    queue.enqueue(tb, inputs_hash="0", output_hashes={})
    queue.dispatch(TB, run_id="r1", baseline={}, denied_reads=DENIED)
    author = _task("dv/notes", "author", AUTHOR_OUTPUT)
    queue.enqueue(author, inputs_hash="0", output_hashes={})
    queue.dispatch(AUTHOR, run_id="r1", baseline={})
    return project


def run_guard(root: Path, event: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str]:
    import subprocess

    proc = subprocess.run(
        [GUARD_PYTHON, str(GUARD)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)},
        check=False,
        timeout=30,
    )
    out = proc.stdout.strip()
    return proc.returncode, (json.loads(out) if out else None), proc.stderr


def call(root: Path, tool: str, agent: str, agent_type: str, **tool_input: Any) -> str | None:
    """The deny reason, or None for no decision; asserts the hook exited cleanly."""
    code, out, err = run_guard(
        root,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "agent_id": agent,
            "agent_type": agent_type,
            "cwd": str(root),
        },
    )
    assert code == 0, (code, out, err)
    if out is None:
        return None
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    return str(spec["permissionDecisionReason"])


def bind(root: Path, agent: str, agent_type: str, task_id: str) -> None:
    assert call(root, GET_CONTEXT, agent, agent_type, task_id=task_id) is None


READER = "chipgraph:critic"  # a subagent type with read tools, bound to the tb task


@pytest.fixture
def reader(root: Path) -> Path:
    bind(root, "r1", READER, TB)
    return root


def reads(root: Path, tool: str, **tool_input: Any) -> str | None:
    return call(root, tool, "r1", READER, **tool_input)


# --- path rules -------------------------------------------------------------------------


def test_read_of_a_file_inside_rtl_is_refused(reader: Path) -> None:
    reason = reads(reader, "Read", file_path=str(reader / "rtl" / "tiny_gpio.sv"))
    assert reason and "must not read" in reason
    assert reads(reader, "Read", file_path="rtl/tiny_gpio.sv")
    assert reads(reader, "Read", file_path=str(reader / "rtl" / "other.sv"))  # rtl/**
    assert reads(reader, "Read", file_path=str(reader / "doc" / ".." / "rtl" / "tiny_gpio.sv"))
    assert reads(reader, "Read", file_path=str(reader / "hwlink" / "tiny_gpio.sv"))  # symlink
    assert reads(reader, "NotebookRead", notebook_path="rtl/n.ipynb")


def test_read_of_a_spec_file_still_passes(reader: Path) -> None:
    assert reads(reader, "Read", file_path=str(reader / "doc" / "specs" / "A.md")) is None
    assert reads(reader, "Read", file_path="/etc/hosts") is None  # outside: not this rule
    assert reads(reader, "Grep", pattern="REQ-", path=str(reader / "doc")) is None
    assert reads(reader, "Glob", pattern="doc/**/*.md") is None
    assert reads(reader, "Glob", pattern="*.md", path=str(reader / "doc")) is None


def test_grep_rooted_at_an_ancestor_is_refused(reader: Path) -> None:
    assert reads(reader, "Grep", pattern="module", path=str(reader))  # the project root
    assert reads(reader, "Grep", pattern="module")  # no path: the root
    assert reads(reader, "Grep", pattern="module", path=".")
    assert reads(reader, "Grep", pattern="module", path=str(reader / "rtl"))
    assert reads(reader, "Grep", pattern="module", path=str(reader.parent))  # above the root
    assert reads(reader, "Grep", pattern="module", path=str(reader / "rtl" / "tiny_gpio.sv"))


def test_glob_whose_prefix_covers_rtl_is_refused(reader: Path) -> None:
    reason = reads(reader, "Glob", pattern="**/*.sv")
    assert reason and "Glob of **/*.sv is refused" in reason
    assert reads(reader, "Glob", pattern="*.sv", path=str(reader))
    assert reads(reader, "Glob", pattern="rtl/*.sv")
    assert reads(reader, "Glob", pattern="{rtl,doc}/*")
    assert reads(reader, "Glob", pattern="rtl/tiny_gpio.sv")
    assert reads(reader, "Glob", pattern=str(reader / "**" / "*.sv"))
    assert reads(reader, "Glob", pattern="*/tiny_gpio.sv", path=str(reader.parent / "proj"))
    assert reads(reader, "Glob", pattern="hwlink/*.sv")


def test_another_tool_naming_an_rtl_path_is_refused(reader: Path) -> None:
    reason = reads(reader, "mcp__filesystem__read_file", path="rtl/tiny_gpio.sv")
    assert reason and "rtl/tiny_gpio.sv" in reason
    assert reads(reader, "mcp__filesystem__list_directory", path=str(reader / "rtl"))
    assert reads(reader, "mcp__filesystem__search", args={"paths": [str(reader)]})
    assert reads(reader, "WebFetch", url="file://" + str(reader / "rtl" / "tiny_gpio.sv"))
    assert reads(reader, "mcp__filesystem__read_file", path="doc/specs/A.md") is None
    assert reads(reader, "TodoWrite", todos=[{"content": "write the test", "status": "x"}]) is None


def test_writing_an_output_that_mentions_rtl_is_a_write_not_a_read(reader: Path) -> None:
    content = "# ports copied from the spec, not from rtl/tiny_gpio.sv\n"
    assert reads(reader, "Write", file_path=str(reader / TB_OUTPUT), content=content) is None
    assert reads(reader, "Write", file_path=str(reader / "rtl" / "x.sv"), content="x")


def test_a_task_without_denied_reads_reads_freely(root: Path) -> None:
    bind(root, "a1", "chipgraph:author", AUTHOR)
    assert call(root, "Read", "a1", "chipgraph:author", file_path="rtl/tiny_gpio.sv") is None
    assert call(root, "Grep", "a1", "chipgraph:author", pattern="module") is None


def test_the_main_session_is_not_bound_by_the_read_rules(root: Path) -> None:
    code, out, _ = run_guard(
        root,
        {"tool_name": "Read", "tool_input": {"file_path": "rtl/tiny_gpio.sv"}, "cwd": str(root)},
    )
    assert (code, out) == (0, None)


# --- the tb-author agent type -----------------------------------------------------------

TBA = "chipgraph:tb-author"


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "rtl/tiny_gpio.sv"}),
        ("Read", {"file_path": "doc/specs/A.md"}),  # whatever the path
        ("Glob", {"pattern": "doc/*.md"}),
        ("Grep", {"pattern": "x", "path": "doc"}),
        ("NotebookRead", {"notebook_path": "doc/x.ipynb"}),
    ],
)
def test_tb_author_gets_no_read_tool_bound_or_not(
    root: Path, tool: str, tool_input: dict[str, Any]
) -> None:
    reason = call(root, tool, "t0", TBA, **tool_input)  # before get_context
    assert reason and "no file-reading tools" in reason
    bind(root, "t1", TBA, TB)
    assert call(root, tool, "t1", TBA, **tool_input)


def test_tb_author_may_call_only_get_context_of_chipgraph(root: Path) -> None:
    reason = call(root, f"{MCP_TOOL_PREFIX}model_block", "t1", TBA, name="gpio")
    assert reason and "only get_context" in reason
    bind(root, "t1", TBA, TB)
    assert call(root, f"{MCP_TOOL_PREFIX}ask_context", "t1", TBA, question="ports?")


def test_tb_author_writes_its_output_and_nothing_else(root: Path) -> None:
    bind(root, "t1", TBA, TB)
    assert call(root, "Write", "t1", TBA, file_path=str(root / TB_OUTPUT), content="x") is None
    reason = call(root, "Write", "t1", TBA, file_path=str(root / "rtl" / "a.sv"), content="x")
    assert reason and "not an output" in reason
    assert call(root, "Bash", "t1", TBA, command="cat rtl/tiny_gpio.sv")


def test_the_decider_reads_no_file_but_lists_its_questions(root: Path) -> None:
    decider = "chipgraph:decider"
    assert call(root, "Read", "d1", decider, file_path="doc/specs/A.md")
    assert call(root, f"{MCP_TOOL_PREFIX}pending_decisions", "d1", decider) is None
    assert call(root, f"{MCP_TOOL_PREFIX}next_task", "d1", decider)


def test_no_read_agents_follow_the_role_data() -> None:
    engine_tools = {
        tool.removeprefix(MCP_TOOL_PREFIX)
        for cap in ("engine_context", "engine_decisions")
        for tool in CAPABILITY_TOOLS[cap]
    }
    expected = {
        agent_type_for(role.id): {
            tool.removeprefix(MCP_TOOL_PREFIX)
            for cap in role.tools
            for tool in CAPABILITY_TOOLS[cap]
            if tool.removeprefix(MCP_TOOL_PREFIX) in engine_tools
        }
        for role in list_roles()
        if not role.can_read_files
    }
    assert {k: set(v) for k, v in guard.NO_READ_AGENTS.items()} == expected


# --- state format -----------------------------------------------------------------------


def test_old_task_records_without_denied_reads_still_work(root: Path) -> None:
    for path in (root / ".chipgraph" / "state" / "runtime" / "tasks").glob("*.json"):
        data = json.loads(path.read_text())
        data.pop("denied_reads", None)
        path.write_text(json.dumps(data))
    bind(root, "a1", "chipgraph:author", AUTHOR)
    assert (
        call(root, "Write", "a1", "chipgraph:author", file_path=AUTHOR_OUTPUT, content="x") is None
    )


def test_a_broken_denied_reads_fails_closed(root: Path) -> None:
    for path in (root / ".chipgraph" / "state" / "runtime" / "tasks").glob("*.json"):
        data = json.loads(path.read_text())
        data["denied_reads"] = "rtl/**"
        path.write_text(json.dumps(data))
    code, out, _ = run_guard(
        root,
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "doc/specs/A.md"},
            "agent_id": "r9",
            "agent_type": READER,
            "cwd": str(root),
        },
    )
    assert code == 2 and out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- the guard and the engine match paths the same way ----------------------------------

DENIED_CASES = ["rtl/**", "rtl/tiny_gpio.sv", "hw/gpio/rtl/*.sv", "ip/{a,b}/x.v"]


@pytest.mark.parametrize(
    "path",
    [
        "rtl/tiny_gpio.sv",
        "rtl/sub/x.sv",
        "rtl",
        "doc/specs/A.md",
        "hw/gpio/rtl/a.sv",
        "hw/gpio/a.sv",
        "ip/a/x.v",
        "",
    ],
)
def test_guard_and_engine_agree_on_paths(path: str) -> None:
    assert guard.path_denied(path, DENIED_CASES) == path_denied(path, DENIED_CASES)
    assert guard.covers_denied(path, DENIED_CASES) == covers_denied(path, DENIED_CASES)


@pytest.mark.parametrize("pattern", ["**/*.sv", "rtl/*.sv", "a/b/c", "a/[bc]/d", "{x,y}/z", ""])
def test_guard_and_engine_agree_on_static_prefixes(pattern: str) -> None:
    assert guard.static_prefix(pattern) == static_prefix(pattern)
