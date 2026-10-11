"""M2-04: the two-way permission suite. `tb-author` cannot read `rtl`; `rtl-author`
cannot read `tb`. One parametrised suite, the same assertions for both directions:

- the rule validator refuses an input of the denied kind, by its kind and by its name;
- `denied_reads` holds the profile's layout paths of the denied kind, never the task's
  own outputs;
- `get_context` refuses an input on a denied path and hands the task to a person;
- the guard (`plugin/hooks/guard.py`, as a real subprocess) refuses every read-type tool,
  any other tool whose input names a denied path, and a write outside the outputs, and
  lets the task write its own output;
- the feedback filter drops issues in a denied file, cuts source excerpts, replaces
  denied paths, keeps behavioural text, and notes what was withheld.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from chipgraph.adapters.runtime.claude_code import service
from chipgraph.adapters.runtime.claude_code.agents import MCP_TOOL_PREFIX
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.contracts import ArtifactRef, Budget, CheckResult, Issue, RuleInstance
from chipgraph.core.contracts.rule import RuleSpec
from chipgraph.core.engine.agent_rule import FeedbackFilter, render_feedback
from chipgraph.core.engine.graph import kind_for
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.runtime.roles import RoleError, check_agent_rules, denied_reads, get_role
from chipgraph.core.state.layout import StateLayout

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "plugin" / "hooks" / "guard.py"
GUARD_PYTHON = os.environ.get("CHIPGRAPH_TEST_GUARD_PYTHON", sys.executable)
GET_CONTEXT = f"{MCP_TOOL_PREFIX}get_context"


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("chipgraph_plugin_guard_two_way", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


@dataclass(frozen=True)
class Side:
    """One direction: a role, the kind it must not see, and paths on either side."""

    role: str
    kind: str
    word: str  # how the withheld-note names the hidden side
    layout_key: str
    layout: str  # the profile's layout template of the denied kind
    denied: str  # a file of the denied kind, under the layout
    named: str  # a file the role must not see by its name (kind by suffix is other)
    loose: str  # a file under the layout whose suffix says nothing (only the path denies)
    allowed: str  # a file the role may read (the spec)
    output: str  # the task's own output

    @property
    def agent(self) -> str:
        return f"chipgraph:{self.role}"


TB_AUTHOR = Side(
    role="tb-author",
    kind="rtl",
    word="the design",
    layout_key="rtl",
    layout="rtl/**",
    denied="rtl/tiny_gpio.sv",
    named="hw/core.svh",
    loose="rtl/NOTES.md",
    allowed="doc/specs/A.md",
    output="dv/test_gpio.py",
)
RTL_AUTHOR = Side(
    role="rtl-author",
    kind="tb",
    word="the testbench",
    layout_key="tb",
    layout="dv/**",
    denied="dv/test_gpio.py",
    named="sim/tb_gpio.sv",
    loose="dv/NOTES.md",
    allowed="doc/specs/A.md",
    output="rtl/tiny_pwm.sv",
)
SIDES = pytest.mark.parametrize("side", [TB_AUTHOR, RTL_AUTHOR], ids=["tb-author", "rtl-author"])


# --- the rule validator --------------------------------------------------------------------


def _rule(side: Side, inputs: list[dict[str, str]]) -> RuleSpec:
    return RuleSpec.model_validate(
        {
            "id": "p/task",
            "kind": "agent",
            "role": side.role,
            "inputs": inputs,
            "outputs": [side.output],
        }
    )


def _instance(rule: RuleSpec, refs: list[ArtifactRef]) -> RuleInstance:
    return RuleInstance(
        rule_id=rule.id,
        inputs=tuple(refs),
        outputs=(ArtifactRef(kind="other", path=rule.outputs[0]),),
        instance_id=RuleInstance.make_id(rule.id, {}),
    )


@SIDES
def test_the_validator_refuses_an_input_of_the_denied_kind(side: Side) -> None:
    rule = _rule(side, [{"path": side.denied}])
    by_kind = _instance(
        rule, [ArtifactRef.model_validate({"kind": side.kind, "path": side.denied})]
    )
    with pytest.raises(RoleError, match=rf"'{side.role}' must not see '{side.kind}'"):
        check_agent_rules({rule.id: rule}, [by_kind])
    # The graph gives a file the kind of its suffix: `hw/core.svh` is `rtl`, and so is the
    # testbench `sim/tb_gpio.sv`, which only its name marks as `tb`. Both are refused.
    named = _instance(rule, [ArtifactRef(kind=kind_for(side.named), path=side.named)])
    with pytest.raises(RoleError, match=rf"must not see '{side.kind}'.*{side.named}"):
        check_agent_rules({rule.id: rule}, [named])
    spec = _instance(rule, [ArtifactRef(kind="spec", path=side.allowed)])
    check_agent_rules({rule.id: rule}, [spec])  # the spec is fine


@SIDES
def test_denied_reads_hold_the_layout_paths_never_the_outputs(side: Side) -> None:
    role = get_role(side.role)
    refs = [ArtifactRef.model_validate({"kind": side.kind, "path": side.denied})]
    denied = denied_reads(
        role, artifacts=refs, layout={side.layout_key: side.layout}, keep=(side.output,)
    )
    assert side.layout in denied and side.denied in denied
    assert side.output not in denied
    from chipgraph.core.runtime.roles import path_denied

    assert path_denied(side.loose, denied) and path_denied(side.denied, denied)
    assert path_denied(side.named, denied) or kind_for(side.named) == side.kind
    assert not path_denied(side.allowed, denied) and not path_denied(side.output, denied)


# --- get_context -----------------------------------------------------------------------------


def _project(tmp: Path, side: Side) -> Path:
    """A tinysoc copy whose one rule gives `side.role` an input on a denied layout path."""
    root = tmp / "proj"
    shutil.copytree(REPO / "examples" / "tinysoc", root)
    (root / side.loose).parent.mkdir(parents=True, exist_ok=True)
    (root / side.loose).write_text("ZQX notes about the hidden side\n")
    pack = root / ".chipgraph" / "packs" / "twoway"
    (pack / "rules").mkdir(parents=True)
    (pack / "pack.yml").write_text("name: twoway\nversion: 0.1.0\nprovides:\n  rules: [rules]\n")
    rule = {
        "rule": "task",
        "kind": "agent",
        "role": side.role,
        "inputs": [{"path": side.loose}],
        "outputs": [side.output],
    }
    (pack / "rules" / "task.yml").write_text(yaml.safe_dump(rule))
    profile_path = root / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["packs"] = ["twoway"]
    profile["layout"][side.layout_key] = side.layout
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
    for args in (
        ("init", "-q", "-b", "main"),
        ("-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A"),
        ("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "i"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


@SIDES
def test_get_context_refuses_an_input_on_a_denied_path(tmp_path: Path, side: Side) -> None:
    root = _project(tmp_path, side)
    answer = asyncio.run(service.next_task(AppContext.load(root), "twoway/task"))
    [task] = answer["tasks"]
    assert task["agent"] == side.agent
    with pytest.raises(AppError) as err:
        asyncio.run(service.get_context(AppContext.load(root), task["task_id"]))
    message = str(err.value)
    assert f"gives role '{side.role}' inputs it must not see" in message
    assert side.loose in message and "ZQX" not in message
    record = TaskQueue(StateLayout(root)).require(task["task_id"])
    assert record.status == "needs_human"
    assert side.layout in record.denied_reads


# --- the guard -------------------------------------------------------------------------------


@pytest.fixture
def guarded(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, Side]:
    side: Side = request.param
    root = (tmp_path / "g").resolve()
    (root / ".git").mkdir(parents=True)
    for rel in (side.denied, side.named, side.allowed):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x\n")
    queue = TaskQueue(StateLayout(root))
    instance = RuleInstance(
        rule_id="p/task",
        outputs=(ArtifactRef(kind="other", path=side.output),),
        instance_id=RuleInstance.make_id("p/task", {}),
    )
    task = AgentTask(
        instance=instance, role=side.role, allowed_writes=(side.output,), budget=Budget()
    )
    queue.enqueue(task, inputs_hash="0", output_hashes={})
    role = get_role(side.role)
    denied = denied_reads(
        role, artifacts=[], layout={side.layout_key: side.layout}, keep=(side.output,)
    )
    queue.dispatch(instance.instance_id, run_id="r1", baseline={}, denied_reads=denied)
    return root, side


def _call(root: Path, tool: str, agent_type: str, **tool_input: Any) -> str | None:
    proc = subprocess.run(
        [GUARD_PYTHON, str(GUARD)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "tool_input": tool_input,
                "agent_id": "s1",
                "agent_type": agent_type,
                "cwd": str(root),
            }
        ),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)},
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    if not out:
        return None
    return str(json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])


TWO_WAY = pytest.mark.parametrize(
    "guarded", [TB_AUTHOR, RTL_AUTHOR], ids=["tb-author", "rtl-author"], indirect=True
)


@TWO_WAY
def test_the_guard_refuses_every_read_tool(guarded: tuple[Path, Side]) -> None:
    root, side = guarded
    assert _call(root, GET_CONTEXT, side.agent, task_id="p/task[]") is None  # binds
    for tool, tool_input in (
        ("Read", {"file_path": str(root / side.denied)}),
        ("Read", {"file_path": str(root / side.allowed)}),  # whatever the path
        ("Glob", {"pattern": "**/*"}),
        ("Grep", {"pattern": "x", "path": str(root / "doc")}),
        ("NotebookRead", {"notebook_path": "doc/x.ipynb"}),
    ):
        reason = _call(root, tool, side.agent, **tool_input)
        assert reason and "no file-reading tools" in reason, (tool, reason)


@TWO_WAY
def test_the_guard_refuses_a_tool_naming_a_denied_path(guarded: tuple[Path, Side]) -> None:
    root, side = guarded
    assert _call(root, GET_CONTEXT, side.agent, task_id="p/task[]") is None
    reason = _call(root, "mcp__filesystem__read_file", side.agent, path=side.denied)
    assert reason and side.denied in reason and "must not read" in reason
    assert _call(root, "Bash", side.agent, command=f"cat {side.denied}")
    other = _call(root, f"{MCP_TOOL_PREFIX}model_block", side.agent, name="gpio")
    assert other and "only get_context" in other


@TWO_WAY
def test_the_guard_lets_the_task_write_only_its_output(guarded: tuple[Path, Side]) -> None:
    root, side = guarded
    assert _call(root, GET_CONTEXT, side.agent, task_id="p/task[]") is None
    assert _call(root, "Write", side.agent, file_path=str(root / side.output), content="x") is None
    refused = _call(root, "Write", side.agent, file_path=str(root / side.denied), content="x")
    assert refused and "not an output" in refused


def test_both_restricted_authors_are_no_read_agents() -> None:
    assert guard.NO_READ_AGENTS[TB_AUTHOR.agent] == guard.NO_READ_AGENTS[RTL_AUTHOR.agent]
    assert guard.NO_READ_AGENTS[RTL_AUTHOR.agent] == frozenset({"get_context"})


# --- the feedback filter ---------------------------------------------------------------------


@SIDES
def test_the_feedback_filter_hides_the_other_side(side: Side) -> None:
    role = get_role(side.role)
    denied = denied_reads(role, artifacts=[], layout={side.layout_key: side.layout})
    shown = FeedbackFilter.for_role(side.role, denied=denied, root="/proj")
    assert shown is not None and shown.kinds == (side.kind,)
    result = CheckResult(
        check_id="sim",
        status="fail",
        issues=(
            Issue(file=side.denied, line=12, rule="assert", msg="ZQX line of the hidden file"),
            Issue(file=side.output, line=3, rule="lint", msg="expected 0x2, got 0x0"),
            Issue(msg=f"/proj/{side.denied}:40: expected 0x2, got 0x0\n   40 |   ZQX excerpt"),
            Issue(file=side.named, msg="ZQX by name"),
        ),
        log_tail=f"{side.denied}:7: ZQX in the log\nexpected 0x2, got 0x0\n      |   ^~~\n",
        duration_s=0.0,
        idempotency_key="k",
    )
    [visible], withheld = shown.apply([result])
    text = json.dumps(visible.model_dump(mode="json"))
    assert "ZQX" not in text and side.denied not in text and side.named not in text
    assert "expected 0x2, got 0x0" in text
    assert f"<{side.kind}>:40: expected 0x2, got 0x0" in text
    assert withheld >= 4
    redo = render_feedback([visible], "verification", withheld=withheld, withheld_note=shown.note)
    assert redo.endswith(shown.note) and side.word in shown.note
    hidden = "the testbench" if side.kind == "tb" else "the design"
    assert f"A failure in {hidden} was hidden" in shown.note
