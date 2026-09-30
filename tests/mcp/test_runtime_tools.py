"""The runtime claude-code MCP tools, in process (M1-11): `next_task`, `get_context`,
`submit` on a tmp git copy of tinysoc with one agent rule (`runtime_tools_helpers`).

No model and no API key: the test plays the subagent's part by writing the output file
itself, the way a role subagent would after `get_context`.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from runtime_tools_helpers import BAD_RTL, GOOD_RTL, OUTPUT, SPEC, TASK_ID, make_pulse_project
from typer.testing import CliRunner

from chipgraph.cli import app
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state.layout import StateLayout
from chipgraph.mcp.server import build_server

TARGET = "pulse/pulse_manifest"


class Session:
    """Calls the tools of one in-process server, like the plugin's main session."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.server = build_server(root)

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        async def _call() -> Any:
            async with Client(self.server) as client:
                return await client.call_tool(tool, args or {})

        return asyncio.run(_call())

    def ok(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.call(tool, args)
        assert not result.is_error, result.content
        assert isinstance(result.structured_content, dict)
        return result.structured_content


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_pulse_project(tmp_path / "tinysoc")


def _record(root: Path) -> Any:
    return TaskQueue(StateLayout(root)).require(TASK_ID)


def test_tools_are_listed(project: Path) -> None:
    async def _names() -> set[str]:
        async with Client(build_server(project)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert {"next_task", "get_context", "submit"} <= asyncio.run(_names())


def test_one_agent_task_runs_and_the_build_goes_on(project: Path) -> None:
    s = Session(project)

    first = s.ok("next_task", {"target": TARGET})
    assert first["done"] is False
    assert [t["task_id"] for t in first["tasks"]] == [TASK_ID]
    task = first["tasks"][0]
    assert task["agent"] == "chipgraph:author"
    assert task["model"] == "haiku"  # tier small, no profile override
    assert task["outputs"] == [OUTPUT]
    assert task["attempt"] == 1
    assert _record(project).status == "dispatched"

    # The build stopped at the agent rule, like at a human rule.
    assert not (project / "build" / "pulse.manifest.json").exists()

    context = s.ok("get_context", {"task_id": TASK_ID})
    assert context["outputs"] == [OUTPUT]
    assert context["role"] == "author"
    assert context["checks"] == ["pulse_lint"]
    [spec] = context["inputs"]
    assert spec["path"] == SPEC
    assert "tiny_pulse" in spec["content"]

    (project / OUTPUT).write_text(GOOD_RTL)
    submitted = s.ok(
        "submit", {"task_id": TASK_ID, "result": {"assumptions": ["active-low reset"]}}
    )
    assert submitted["accepted"] is True, submitted
    assert submitted["status"] == "accepted"
    assert submitted["result"]["files_written"] == [OUTPUT]
    assert submitted["result"]["assumptions"] == ["active-low reset"]

    # The executor now reports ok, and the build goes on to the gen rule.
    second = s.ok("next_task", {"target": TARGET})
    assert second["done"] is True, second
    assert second["tasks"] == []
    manifest = json.loads((project / "build" / "pulse.manifest.json").read_text())
    assert set(manifest) == {"rtl"}

    # `chipgraph build` sees a finished, fresh build.
    result = CliRunner().invoke(app, ["--json", "-C", str(project), "build", TARGET])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["failed"] == [] and summary["waiting_gate"] == []

    # Every step is in the journal of the run that dispatched the task.
    layout = StateLayout(project)
    events = journal_mod.read(layout.journal(first["run_id"])).events
    phases = [e.payload.get("phase") for e in events if e.type == "agent_turn"]
    assert phases == ["dispatch", "context", "submit"]
    assert any(e.type == "check_result" and e.rule_instance == TASK_ID for e in events)


def test_a_change_outside_outputs_is_rejected(project: Path) -> None:
    s = Session(project)
    s.ok("next_task", {"target": TARGET})
    s.ok("get_context", {"task_id": TASK_ID})
    (project / OUTPUT).write_text(GOOD_RTL)
    readme = project / "README.md"
    readme.write_text(readme.read_text() + "\n- rtl/tiny_pulse.sv\n")

    rejected = s.ok("submit", {"task_id": TASK_ID})
    assert rejected["accepted"] is False
    assert rejected["status"] == "rejected"
    assert rejected["outside_outputs"] == ["README.md"]
    assert rejected["attempts"] == 1
    assert "README.md" in rejected["reasons"][0]

    # It is not handed out again while the stray change is still there.
    waiting = s.ok("next_task", {"target": TARGET})
    assert waiting["tasks"] == [] and waiting["done"] is False
    assert "README.md" in waiting["waiting"]

    subprocess.run(["git", "checkout", "--", "README.md"], cwd=project, check=True)
    again = s.ok("next_task", {"target": TARGET})
    [task] = again["tasks"]
    assert task["attempt"] == 2
    context = s.ok("get_context", {"task_id": TASK_ID})
    assert "README.md" in context["previous_rejection"][0]
    assert s.ok("submit", {"task_id": TASK_ID})["accepted"] is True


def test_missing_output_and_failing_check_use_up_the_budget(project: Path) -> None:
    s = Session(project)
    s.ok("next_task", {"target": TARGET})
    first = s.ok("submit", {"task_id": TASK_ID})
    assert first["status"] == "rejected"
    assert first["missing"] == [OUTPUT]

    s.ok("next_task", {"target": TARGET})
    (project / OUTPUT).write_text(BAD_RTL)
    second = s.ok("submit", {"task_id": TASK_ID})
    assert second["status"] == "budget_exhausted"
    assert second["checks"][0]["status"] == "fail"
    assert second["result"]["status"] == "budget_exhausted"

    after = s.ok("next_task", {"target": TARGET})
    assert after["tasks"] == [] and after["done"] is False
    assert "used all 2 tries" in after["waiting"]

    result = CliRunner().invoke(app, ["-C", str(project), "build", TARGET])
    assert result.exit_code != 0
    assert "failed: pulse/tiny_pulse[]" in result.output
    handoff = Path(result.output.strip().splitlines()[-1]).read_text()
    assert "used all 2 tries" in handoff


def test_nda_input_is_refused_before_it_reaches_context(tmp_path: Path) -> None:
    project = make_pulse_project(tmp_path / "tinysoc", nda=True)
    s = Session(project)
    s.ok("next_task", {"target": TARGET})

    refused = s.call("get_context", {"task_id": TASK_ID})
    assert refused.is_error
    text = " ".join(getattr(c, "text", "") for c in refused.content)
    assert "nda" in text and SPEC in text
    assert "rising edge" not in text  # no spec content
    assert _record(project).status == "needs_human"

    after = s.ok("next_task", {"target": TARGET})
    assert after["tasks"] == [] and "nda" in after["waiting"]


def test_submit_needs_human_does_not_use_a_try(project: Path) -> None:
    s = Session(project)
    s.ok("next_task", {"target": TARGET})
    answer = s.ok(
        "submit",
        {
            "task_id": TASK_ID,
            "result": {"status": "needs_human", "open_questions": ["which reset polarity?"]},
        },
    )
    assert answer["status"] == "needs_human"
    assert answer["attempts"] == 0
    assert "which reset polarity?" in s.ok("next_task", {"target": TARGET})["waiting"]


def test_waiting_on_a_gate(project: Path) -> None:
    # tinysoc's RTL rule is gated on its spec, and this copy has no baseline decision.
    answer = Session(project).ok("next_task", {"target": "tinysoc/rtl"})
    assert answer["tasks"] == [] and answer["done"] is False
    assert "waiting for gate 'spec:" in answer["waiting"]


def test_tools_refuse_a_task_that_is_not_dispatched(project: Path) -> None:
    s = Session(project)
    assert s.call("get_context", {"task_id": TASK_ID}).is_error  # never enqueued
    s.ok("next_task", {"target": TARGET})
    (project / OUTPUT).write_text(GOOD_RTL)
    assert s.ok("submit", {"task_id": TASK_ID})["accepted"] is True
    assert s.call("submit", {"task_id": TASK_ID}).is_error  # already accepted


def test_other_runtimes_are_not_served(tmp_path: Path) -> None:
    project = make_pulse_project(tmp_path / "tinysoc", runtime="generic")
    result = Session(project).call("next_task", {"target": TARGET})
    assert result.is_error
    assert "claude-code" in " ".join(getattr(c, "text", "") for c in result.content)
