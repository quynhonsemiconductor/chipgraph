"""M2-02b: the acceptance script's report (`docs/agent-loop-claude-code/report.py`) on
synthetic `claude -p --output-format stream-json` streams over a real fixture project,
whose state comes from the real MCP tools (the test plays the subagents). So a real run
is graded as intended: PASS needs `fixable` accepted on attempt 2 with the label in its
second context, `hopeless` exhausted after 2 dispatches with a HANDOFF, no Agent call
for it after that, few main-session turns, and a cost.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from agent_loop_helpers_plugin import load_report
from mcp import Client

from chipgraph.mcp.server import build_server

report = load_report()
fx = report.fx
MCP = "mcp__plugin_chipgraph_chipgraph__"


def _call(root: Path, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async def _run() -> Any:
        async with Client(build_server(root)) as client:
            return await client.call_tool(tool, args)

    result = asyncio.run(_run())
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


class Stream:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self.n = 0

    def call(self, name: str, args: dict[str, Any], result: Any, parent: str | None = None) -> str:
        self.n += 1
        use_id = f"toolu_{self.n}"
        block = {"type": "tool_use", "id": use_id, "name": name, "input": args}
        self.lines.append(
            {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [block]}}
        )
        text = result if isinstance(result, str) else json.dumps(result)
        content = [{"type": "text", "text": text}]
        res = {"type": "tool_result", "tool_use_id": use_id, "content": content}
        self.lines.append(
            {"type": "user", "parent_tool_use_id": parent, "message": {"content": [res]}}
        )
        return use_id

    def write(self, out: Path, *, turns: int = 18, cost: float | None = 0.4321) -> None:
        result: dict[str, Any] = {"type": "result", "subtype": "success", "num_turns": turns}
        if cost is not None:
            result["total_cost_usd"] = cost
        lines = [{"type": "system", "subtype": "init", "plugins": [{"name": "chipgraph"}]}]
        lines += [*self.lines, result]
        (out / "stream.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def _agent(s: Stream, root: Path, task: dict[str, Any], text: str | None, out: str) -> None:
    """One Agent call for `task`: the subagent calls get_context and writes `out`."""
    agent = s.call(
        "Agent",
        {
            "subagent_type": task["agent"],
            "model": task["model"],
            "description": f"chipgraph {task['task_id']}",
            "prompt": task["prompt"],
        },
        "done",
    )
    context = _call(root, "get_context", {"task_id": task["task_id"]})
    s.call(f"{MCP}get_context", {"task_id": task["task_id"]}, context, parent=agent)
    if text is not None:
        (root / out).parent.mkdir(parents=True, exist_ok=True)
        (root / out).write_text(text)
        s.call("Write", {"file_path": str(root / out), "content": text}, "ok", parent=agent)


def _submit(s: Stream, root: Path, task_id: str) -> dict[str, Any]:
    answer = _call(root, "submit", {"task_id": task_id})
    s.call(f"{MCP}submit", {"task_id": task_id}, answer)
    return answer


def _next(s: Stream, root: Path) -> dict[str, Any]:
    answer = _call(root, "next_task", {"target": fx.TARGET})
    s.call(f"{MCP}next_task", {"target": fx.TARGET}, answer)
    return answer


def _good_run(out: Path, *, extra_hopeless_agent: bool = False) -> Stream:
    root = fx.make_loop_project(out / "tinysoc", tiers={"medium": "haiku", "large": "sonnet"})
    s = Stream()
    first = {t["task_id"]: t for t in _next(s, root)["tasks"]}
    _agent(s, root, first[fx.FIXABLE_TASK], "tiny_timer counts.\n", fx.FIXABLE_OUTPUT)
    _agent(s, root, first[fx.HOPELESS_TASK], "tiny_gpio\n", fx.HOPELESS_OUTPUT)
    assert _submit(s, root, fx.FIXABLE_TASK)["status"] == "rejected"
    assert _submit(s, root, fx.HOPELESS_TASK)["status"] == "rejected"
    second = {t["task_id"]: t for t in _next(s, root)["tasks"]}
    _agent(s, root, second[fx.FIXABLE_TASK], f"{fx.MARKER}\ntimer\n", fx.FIXABLE_OUTPUT)
    _agent(s, root, second[fx.HOPELESS_TASK], "the tiny_gpio block\n", fx.HOPELESS_OUTPUT)
    assert _submit(s, root, fx.FIXABLE_TASK)["status"] == "accepted"
    assert _submit(s, root, fx.HOPELESS_TASK)["status"] == "budget_exhausted"
    final = _next(s, root)
    assert final["stopped"] is True and final["tasks"] == []
    if extra_hopeless_agent:  # a main session that "tries once more" by hand
        s.call(
            "Agent",
            {"subagent_type": "chipgraph:author", "prompt": f"redo {fx.HOPELESS_TASK}"},
            "done",
        )
    return s


@pytest.fixture
def out(tmp_path: Path) -> Path:
    path = tmp_path / "cg-loop-test"
    path.mkdir()
    return path


def test_a_good_run_passes(out: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _good_run(out).write(out)
    assert report.main(out) == 0
    text = capsys.readouterr().out
    assert "PASS" in text
    assert "labels per submit: ['verification', None]" in text
    assert "tiers per submit:  ['medium', 'large']" in text
    assert "('chipgraph:author', 'haiku'), ('chipgraph:author', 'sonnet')" in text
    assert "final status: budget_exhausted; dispatches: 2" in text
    assert f"`{fx.HOPELESS_TASK}` [verification]" in text  # the HANDOFF text is printed
    assert "(e) total cost: $0.4321" in text


def test_an_agent_call_after_exhaustion_fails(
    out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _good_run(out, extra_hopeless_agent=True).write(out)
    assert report.main(out) == 1
    assert "after exhaustion=1" in capsys.readouterr().out


def test_too_many_turns_or_no_cost_fails(out: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stream = _good_run(out)
    stream.write(out, turns=report.MAX_MAIN_TURNS)
    assert report.main(out) == 1
    stream.write(out, cost=None)
    assert report.main(out) == 1
    assert "not reported" in capsys.readouterr().out


def test_fixed_without_the_label_in_context_fails() -> None:
    run = report.TaskRun(fx.FIXABLE_TASK)
    ok, notes = report.verdict_fixable(None, run, [])
    assert ok is False and "accepted=False" in notes[0]
