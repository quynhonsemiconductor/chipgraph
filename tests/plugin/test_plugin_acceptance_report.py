"""M1-16: the acceptance script's report (`docs/plugin-claude-code/report.py`) on synthetic
`claude -p --output-format stream-json` streams, so a real run is graded as intended: PASS
needs the expected MCP tool, no tool outside chipgraph (other than `Agent(chipgraph:*)`),
and the expected content in the output.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
MCP = "mcp__plugin_chipgraph_chipgraph__"


def _report() -> ModuleType:
    path = REPO / "docs" / "plugin-claude-code" / "report.py"
    spec = importlib.util.spec_from_file_location("plugin_acceptance_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["plugin_acceptance_report"] = module
    spec.loader.exec_module(module)
    return module


report = _report()


class _Stream:
    """Builds one stream-json transcript: tool uses with their results, then `result`."""

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
        content = [{"type": "text", "text": json.dumps(result)}]
        res = {"type": "tool_result", "tool_use_id": use_id, "content": content}
        self.lines.append(
            {"type": "user", "parent_tool_use_id": parent, "message": {"content": [res]}}
        )
        return use_id

    def write(self, path: Path, text: str, cost: float = 0.01) -> None:
        final = {
            "type": "result",
            "subtype": "success",
            "result": text,
            "num_turns": self.n,
            "total_cost_usd": cost,
        }
        path.write_text("".join(json.dumps(x) + "\n" for x in [*self.lines, final]))


def _good_run(out: Path) -> None:
    streams = out / "streams"
    streams.mkdir(parents=True)
    (out / "build.txt").write_text("run 20260101T000000Z-abc\n  waiting on a gate: ...\n")

    s = _Stream()
    s.call(MCP + "config_show", {}, {"profile": {"project": "tinysoc", "blocks": {}}})
    s.call(MCP + "status", {}, {"run_id": "20260101T000000Z-abc", "counts": {}})
    s.write(
        streams / "status.jsonl",
        "project: tinysoc (3 blocks)\nrun: 20260101T000000Z-abc (finished)\n"
        "tinysoc/rtl[block=gpio] waits for gate spec:gpio",
    )

    s = _Stream()
    s.call("ToolSearch", {"query": "select:model_find"}, {})
    s.call(MCP + "model_find", {"kind": "requirement"}, {"results": [{"key": "r"}]})
    s.call(MCP + "model_trace", {"key": "requirement:REQ-TIM-001"}, {"key": "r"})
    s.write(
        streams / "trace.jsonl",
        "REQ-TIM-001 (requirement:REQ-TIM-001)\nspec:\n  doc/specs/TINY_TIMER_MAS.md:68  ...",
    )

    s = _Stream()
    s.call(MCP + "model_find", {"name": "REQ-NOPE-999"}, {"results": []})
    s.call(MCP + "model_find", {"name": "REQ-NOPE-*"}, {"results": []})
    s.write(
        streams / "trace_unknown.jsonl",
        "`REQ-NOPE-999` is not a requirement in the Design Model.\nclose IDs: REQ-TIM-001",
    )

    s = _Stream()
    agent = s.call("Agent", {"subagent_type": "chipgraph:asker", "model": "haiku"}, {})
    s.call(MCP + "ask_context", {"question": "q"}, {"sources": []}, parent=agent)
    cite = "doc/specs/TINY_TIMER_MAS.md:60"
    verdict = {"ok": True, "answer": {"answer": "0", "citations": [cite], "unknown": False}}
    s.call(MCP + "ask_check", {}, verdict)
    s.write(streams / "ask.jsonl", f"It resets to 0.\nSources:\n{cite}  `COMPARE` ...")

    s = _Stream()
    s.call(MCP + "triage", {"path": "logs/triage.log"}, {"status": "deferred"})
    s.call(MCP + "pending_decisions", {}, {"decisions": [{"question_id": "q"}]})
    s.call("Agent", {"subagent_type": "chipgraph:decider", "model": "haiku"}, {})
    s.call(MCP + "answer_decision", {"question_id": "q"}, {"status": "answered"})
    s.call(MCP + "triage", {"path": "logs/triage.log"}, {"status": "decided", "label": "rtl"})
    s.write(streams / "triage.jsonl", "rtl (small, confidence 0.9)\nsummary ...")

    s = _Stream()
    s.call(MCP + "init", {"confirm": False}, {"written": False})
    s.call(MCP + "init", {"confirm": True}, {"written": True})
    s.write(streams / "init.jsonl", "files written: .chipgraph.yml\nNext steps: ...")
    (out / "fresh").mkdir()
    (out / "fresh" / ".chipgraph.yml").write_text("project: fresh\n")
    (out / "git-status-fresh.txt").write_text("?? .chipgraph.yml\n")
    (out / "git-status-tinysoc.txt").write_text("")


def test_a_good_run_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _good_run(tmp_path)
    assert report.main(tmp_path) == 0
    printed = capsys.readouterr().out
    assert printed.count(": PASS") == 6
    assert "total_cost_usd: 0.0600" in printed


def test_a_tool_outside_chipgraph_fails_the_case(tmp_path: Path) -> None:
    _good_run(tmp_path)
    s = _Stream()
    s.call("Read", {"file_path": "doc/specs/TINY_TIMER_MAS.md"}, {})
    s.call(MCP + "model_find", {}, {"results": [{"key": "r"}]})
    s.call(MCP + "model_trace", {"key": "r"}, {"key": "r"})
    s.write(tmp_path / "streams" / "trace.jsonl", "REQ-TIM-001 doc/specs/TINY_TIMER_MAS.md:68")
    assert report.main(tmp_path) == 1


def test_another_plugins_agent_fails_the_case(tmp_path: Path) -> None:
    _good_run(tmp_path)
    s = _Stream()
    s.call("Agent", {"subagent_type": "other-plugin:helper"}, {})
    s.call(MCP + "triage", {}, {"status": "decided", "label": "rtl"})
    s.write(tmp_path / "streams" / "triage.jsonl", "rtl")
    assert report.main(tmp_path) == 1


def test_missing_content_fails_the_case(tmp_path: Path) -> None:
    _good_run(tmp_path)
    (tmp_path / "fresh" / ".chipgraph.yml").unlink()  # init "said" it wrote; it did not
    assert report.main(tmp_path) == 1


def test_the_cases_cover_every_v0_command() -> None:
    prompts = [case.prompt for case in report.cases()]
    for command in ("status", "trace", "ask", "triage", "init-chipgraph"):
        assert any(
            p.startswith(f"/chipgraph:{command} ") or p == f"/chipgraph:{command}" for p in prompts
        ), command
    assert "/chipgraph:trace REQ-NOPE-999" in prompts
    assert (REPO / "evals" / "triage" / "logs" / f"{report.TRIAGE_LOG}.log").is_file()
    # The pre-approved tools are chipgraph MCP tools only.
    assert all(t.startswith(MCP) for t in report._allowed_tools().split())
