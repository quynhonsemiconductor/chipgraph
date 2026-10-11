"""M2-11a: `chipgraph plan <block>` and `chipgraph rtl <block>`, and `build rtl:<block>`.

`plan` builds up to and including the plan gate; `rtl` builds the whole target; both print
what is next (the gate to approve, the agent tasks waiting for `/chipgraph:run`, the PR's
URL) and exit 0 (done), 1 (something waits or failed) or 2 (usage or config error), like
`build`. `status` and `HANDOFF.md` show the chain and where it waits.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

_HELPERS = Path(__file__).resolve().parents[1] / "packs" / "digital_rtl"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))

from rtl_target_helpers import (  # noqa: E402
    EXPAND,
    OPEN_PR,
    PLAN,
    PR_GATE,
    REVIEW,
    TB,
    ChainRuntime,
    module_id,
    planned_timer,
    register,
    target_project,
)

import chipgraph.cli as cli  # noqa: E402

runner = CliRunner()


def _run(root: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(cli.app, ["-C", str(root), *args])
    return result.exit_code, result.output


def _scripted(monkeypatch: pytest.MonkeyPatch, runtime: ChainRuntime) -> None:
    real = cli._load_ctx
    monkeypatch.setattr(cli, "_load_ctx", lambda state: register(real(state), runtime))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return target_project(tmp_path / "tinysoc", real_lint=False)


def test_plan_stops_at_the_gate_and_says_how_to_approve_it(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ChainRuntime(project, [planned_timer()])
    _scripted(monkeypatch, runtime)
    code, out = _run(project, "plan", "timer")
    assert code == 1, out
    assert f"done: {PLAN}" in out and f"waiting on a gate: {EXPAND}" in out
    assert "next:" in out
    assert "  read the plan: chipgraph plan show timer" in out
    assert f"  gate plan:timer: chipgraph approve plan:timer --instance '{EXPAND}'" in out
    assert runtime.calls == ["digital-rtl/plan"]  # only the planner ran

    code, out = _run(project, "approve", "plan:timer")
    assert code == 0, out
    code, out = _run(project, "plan", "timer")
    assert code == 0, out  # the plan gate is approved: `plan` is done
    assert "done: nothing waits" in out
    assert runtime.calls == ["digital-rtl/plan"]  # nothing past the plan gate in `plan`


def test_rtl_goes_on_after_the_gate_and_prints_the_pr_gate(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ChainRuntime(project, [planned_timer()])
    _scripted(monkeypatch, runtime)
    _run(project, "rtl", "timer")
    _run(project, "approve", "plan:timer")
    code, out = _run(project, "rtl", "timer")
    assert code == 1, out
    assert f"gate pr:timer: chipgraph approve pr:timer --instance '{PR_GATE}'" in out
    assert "HANDOFF.md" in out

    run_id = out.splitlines()[0].removeprefix("run ").strip()
    text = (project / ".chipgraph" / "state" / "runs" / run_id / "HANDOFF.md").read_text(
        encoding="utf-8"
    )
    assert "## Chain of rtl:timer" in text
    assert f"`{PR_GATE}`: waiting pr:timer" in text
    assert f"`{OPEN_PR}`: done" in text and f"`{REVIEW}`: done" in text
    assert f"`{TB}`: done" in text and f"`{module_id('tiny_timer')}`: done" in text

    code, out = _run(project, "approve", "pr:timer")
    assert code == 0, out
    code, out = _run(project, "rtl", "timer")
    assert code == 0, out
    assert "done: branch chipgraph/" in out


def test_status_shows_the_chain_and_where_it_waits(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ChainRuntime(project, [planned_timer()])
    _scripted(monkeypatch, runtime)
    _, built = _run(project, "rtl", "timer")
    run_id = built.splitlines()[0].removeprefix("run ").strip()
    code, out = _run(project, "status", run_id)
    assert code == 0 and "chain of rtl:timer:" in out
    assert f"1. done          {PLAN}" in out  # the plan was written
    assert f"2. waiting_gate  {EXPAND}" in out  # and the gate waits
    code, out = _run(project, "--json", "status", run_id)
    rows = json.loads(out)["chain"]
    assert [r["instance"] for r in rows] == [PLAN, EXPAND]
    assert rows[1]["state"] == "waiting_gate"


def test_json_output_has_the_next_steps(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = ChainRuntime(project, [planned_timer()])
    _scripted(monkeypatch, runtime)
    code, out = _run(project, "--json", "rtl", "timer")
    assert code == 1
    data = json.loads(out)
    assert data["target"] == "rtl:timer" and data["waiting_gate"] == [EXPAND]
    assert data["next"]["gates"] == [{"gate": "plan:timer", "instance": EXPAND}]
    assert data["next"]["tasks"] == [] and data["next"]["failed"] == []
    assert [c["instance"] for c in data["next"]["chain"]] == [PLAN, EXPAND]
    assert data["handoff"].endswith("HANDOFF.md")


def test_build_takes_the_target_too(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = ChainRuntime(project, [planned_timer()])
    _scripted(monkeypatch, runtime)
    code, out = _run(project, "build", "rtl:timer")
    assert code == 1 and EXPAND in out
    assert runtime.calls == ["digital-rtl/plan"]


def test_the_claude_code_runtime_queues_the_task_and_the_command_says_so(
    project: Path,
) -> None:
    path = project / ".chipgraph.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["runtime"] = "claude-code"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    code, out = _run(project, "plan", "timer")
    assert code == 1, out
    assert f"agent tasks waiting for /chipgraph:run in Claude Code: {PLAN}" in out
    assert "gate plan:timer" not in out  # nothing to approve before the plan exists


def _next_task_over_mcp(project: Path, target: str) -> Any:
    from mcp import Client

    from chipgraph.mcp.server import build_server

    async def _call() -> Any:
        async with Client(build_server(project)) as client:
            return await client.call_tool("next_task", {"target": target})

    return asyncio.run(_call())


def test_the_claude_code_runtime_and_mcp_take_the_target_too(project: Path) -> None:
    """`next_task(target="rtl:timer")` hands out the planner's task, as `build` would queue it."""
    path = project / ".chipgraph.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["runtime"] = "claude-code"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    result = _next_task_over_mcp(project, "rtl:timer")
    assert not result.is_error, result.content
    assert [t["task_id"] for t in result.structured_content["tasks"]] == [PLAN]
    # An unknown block is an error answer, not a crash.
    assert _next_task_over_mcp(project, "rtl:nope").is_error


# --- errors: exit 2 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (("rtl", "nope"), "matched no instances; check its params (block='nope')"),
        (("plan", "nope"), "matched no instances; check its params (block='nope')"),
        (("plan",), "Missing argument"),
        (("plan", "a", "b"), "usage: chipgraph plan <block>"),
        (("plan", "show"), "check its params (block='show')"),  # a block named "show"
        (("approve", "pr:nothing"), "no instance has gate 'pr:nothing'"),
        (("build", "rtl"), "needs block: write 'rtl:<block>'"),
    ],
)
def test_a_wrong_block_or_usage_exits_2_with_a_message(
    project: Path, args: tuple[str, ...], needle: str
) -> None:
    code, out = _run(project, *args)
    assert code == 2, out
    assert needle in out


def test_plan_show_still_works(project: Path) -> None:
    code, out = _run(project, "plan", "show", "timer")
    assert code == 1  # no plan yet
    assert "timer" in out
