"""M2-11b: the MCP tools `/chipgraph:plan` and `/chipgraph:rtl` drive, end to end, offline.

A tinysoc copy in runtime `claude-code` (target `rtl:timer`), driven through the MCP server
exactly as the two commands do, with each "subagent" scripted here (it writes its task's
outputs, or replies with a review): no model is called.

- `/chipgraph:plan timer`: `next_task(target="plan:timer")` -> the planner's first plan
  fails `plan_check`, the second passes -> the gate waits; `plan_show` has what the person
  reads; a rejection without a note changes nothing; a rejection **with a note** hands the
  planner task out again, with the note first in `previous_rejection` (`revised` in the
  answer, a `revise` event in the journal), and the note is gone once it is accepted;
  `approve` (no `instance`, no `by`) records the local user.
- `/chipgraph:rtl timer`: the modules in dependency order, the testbench, the Critic (its
  JSON review passed as `result.review`), `open_pr` inside `next_task` (no task for it),
  the gate `pr:timer` waits; `status` has the chain and, for the block, every task's files
  and checks, the Critic's verdict with its major comment and the branch.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

_RTL_TESTS = Path(__file__).resolve().parents[1] / "packs" / "digital_rtl"
if str(_RTL_TESTS) not in sys.path:
    sys.path.insert(0, str(_RTL_TESTS))

import yaml  # noqa: E402
from plan_helpers import FIXTURES, fixture_plan, overlapping  # noqa: E402
from rtl_target_helpers import (  # noqa: E402
    EXPAND,
    MODULE_IDS,
    OPEN_PR,
    PLAN,
    PR_GATE,
    PR_RECORD,
    REVIEW,
    RTL_FILES,
    TB,
    TB_FILE,
    TB_TEXT,
    module_text,
    target_project,
)

import chipgraph.mcp.server as server_mod  # noqa: E402
from chipgraph.app.context import AppContext  # noqa: E402
from chipgraph.core.state import journal as journal_mod  # noqa: E402
from chipgraph.mcp.server import build_server  # noqa: E402
from chipgraph.packs.digital_rtl.plan.show import plan_summary  # noqa: E402

GOOD = (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8")
BAD = yaml.safe_dump(overlapping(fixture_plan("timer")), sort_keys=False)
REVISED = GOOD.replace("assumptions:\n", "assumptions:\n  - Revised after the review note.\n", 1)
NOTE = "Say in the assumptions why COUNT is 32 bits."
PERSON = "person-at-the-keyboard"


class Session:
    """An MCP client session on the project, like the plugin's main session."""

    def __init__(self, client: Client) -> None:
        self.client = client

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        result = await self.client.call_tool(tool, args)
        assert not result.is_error, (tool, args, result.content)
        assert isinstance(result.structured_content, dict)
        return result.structured_content

    async def error(self, tool: str, **args: Any) -> str:
        result = await self.client.call_tool(tool, args)
        assert result.is_error, (tool, args)
        return "".join(getattr(block, "text", "") for block in result.content)


def _write(root: Path, rel: str, text: str) -> None:
    out = root / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


async def _do(s: Session, root: Path, task: dict[str, Any], plan: str) -> dict[str, Any]:
    """One scripted subagent: get_context, write the outputs (or the review), submit."""
    context = await s.call("get_context", task_id=task["task_id"])
    rule = task["rule"]
    if task.get("reply") == "review":
        review = context["review"]
        first = RTL_FILES[0]  # a new file: every line is in the diff
        line = (root / first).read_text(encoding="utf-8").splitlines()[0]
        reply = {
            "schema_version": 1,
            "target": review["target"],
            "base": review["base"],
            "head": review["head"],
            "reviewed": [*RTL_FILES, TB_FILE],
            "comments": [
                {
                    "id": "R1",
                    "severity": "major",
                    "category": "requirement",
                    "file": first,
                    "line": 1,
                    "claim": "The header does not cite the requirement it implements.",
                    "evidence": f"{first}:1 {line}",
                }
            ],
            "summary": "Three modules and a test; one header lacks its requirement id.",
            "verdict": "changes_requested",
        }
        return await s.call(
            "submit", task_id=task["task_id"], result={"status": "done", "review": reply}
        )
    if rule == "digital-rtl/plan":
        text = plan
    elif rule == "digital-rtl/rtl_module":
        text = module_text(context["params"]["module"])
    elif rule == "dv/tb_module":
        text = TB_TEXT
    else:
        raise AssertionError(f"no script for {rule}")
    for rel in task["outputs"]:
        _write(root, rel, text)
    return await s.call(
        "submit",
        task_id=task["task_id"],
        result={"status": "done", "files_written": task["outputs"]},
    )


def _events(root: Path, run_id: str) -> list[Any]:
    return journal_mod.read(AppContext.load(root).layout.journal(run_id)).events


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(server_mod, "default_identity", lambda: PERSON)
    return target_project(tmp_path / "tinysoc", profile={"runtime": "claude-code"})


async def _plan_phase(s: Session, root: Path) -> None:
    """`/chipgraph:plan timer`, part A then part B, with a revise and an approve."""
    # A: the planner's first plan fails plan_check, the second passes; the gate waits.
    for plan, want in ((BAD, "rejected"), (GOOD, "accepted")):
        answer = await s.call("next_task", target="plan:timer")
        assert [t["rule"] for t in answer["tasks"]] == ["digital-rtl/plan"]
        assert answer["tasks"][0]["agent"] == "chipgraph:planner"
        done = await _do(s, root, answer["tasks"][0], plan)
        assert done["status"] == want, done
    answer = await s.call("next_task", target="plan:timer")
    assert answer["stopped"] and not answer["tasks"] and "revised" not in answer
    assert any(b["instance"] == EXPAND and "plan:timer" in b["reason"] for b in answer["blocked"])

    # B: what the person reads is `chipgraph plan show`'s data.
    shown = await s.call("plan_show", block="timer")
    assert shown == plan_summary(AppContext.load(root), "timer")
    assert shown["exists"] and shown["ok"] and shown["gate"]["status"] == "waiting"
    assert shown["gate"]["id"] == "plan:timer" and shown["gate"]["instance"] == EXPAND
    assert [m["name"] for m in shown["modules"]] == [
        "tiny_timer_counter",
        "tiny_timer_irq",
        "tiny_timer",
    ]
    assert all(row["modules"] for row in shown["coverage"])
    assert "unknown block" in await s.error("plan_show", block="nope")

    # A rejection with no note: the gate is rejected, nothing is handed out.
    await s.call("approve", gate_id="plan:timer", reject=True)
    answer = await s.call("next_task", target="plan:timer")
    assert answer["stopped"] and not answer["tasks"] and "revised" not in answer

    # "Revise": the person's note goes to the planner, which is handed out again.
    rejected = await s.call("approve", gate_id="plan:timer", reject=True, note=NOTE)
    assert rejected["decision"] == "reject" and rejected["by"] == PERSON
    answer = await s.call("next_task", target="plan:timer")
    [item] = answer["revised"]
    assert item["task_id"] == PLAN and item["gate"] == "plan:timer"
    assert item["note"] == NOTE and item["by"] == PERSON
    [task] = answer["tasks"]
    assert task["task_id"] == PLAN
    context = await s.call("get_context", task_id=PLAN)
    assert NOTE in context["previous_rejection"][0]
    assert "plan:timer" in context["previous_rejection"][0]
    revise = [e for e in _events(root, answer["run_id"]) if e.payload.get("phase") == "revise"]
    assert revise and revise[0].rule_instance == PLAN
    _write(root, "plan/timer.plan.yml", REVISED)
    done = await s.call("submit", task_id=PLAN, result={"status": "done"})
    assert done["status"] == "accepted"
    answer = await s.call("next_task", target="plan:timer")
    assert answer["stopped"] and not answer["tasks"] and "revised" not in answer
    assert (await s.call("plan_show", block="timer"))["gate"]["status"] == "waiting"
    # The note was answered: a new dispatch would not see it again.
    assert not list((root / ".chipgraph" / "state" / "runtime" / "revisions").glob("*.json"))

    # "Approve": no instance (one instance has the gate), no `by` (the local user).
    approval = await s.call("approve", gate_id="plan:timer", note="approved in /chipgraph:plan")
    assert approval["decision"] == "approve" and approval["by"] == PERSON
    assert approval["gate_id"] == "plan:timer"
    assert (await s.call("plan_show", block="timer"))["gate"]["status"] == "approved"
    assert "gates:" in await s.error("approve", gate_id="plan:nope")


async def _rtl_phase(s: Session, root: Path) -> dict[str, Any]:
    """`/chipgraph:rtl timer`: the loop until the engine stops at the gate `pr:timer`."""
    seen: list[str] = []
    for _ in range(20):
        answer = await s.call("next_task", target="rtl:timer")
        if answer["done"] or answer["stopped"]:
            break
        for task in answer["tasks"]:
            assert task["rule"] not in ("digital-rtl/open_pr", "digital-rtl/pr_gate")
            seen.append(task["task_id"])
            done = await _do(s, root, task, GOOD)
            assert done["status"] == "accepted", done["reasons"]
    else:
        raise AssertionError("the loop did not stop in 20 rounds")
    assert not answer["done"]
    assert [b["instance"] for b in answer["blocked"]] == [PR_GATE]
    assert "pr:timer" in answer["blocked"][0]["reason"]
    assert set(seen) == {*MODULE_IDS, TB, REVIEW}
    assert seen.index(REVIEW) == len(seen) - 1
    assert seen.index(MODULE_IDS[2]) > max(seen.index(MODULE_IDS[0]), seen.index(MODULE_IDS[1]))
    return answer


def test_plan_then_rtl_through_the_mcp_tools(project: Path) -> None:
    root = project

    async def _session() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        async with Client(build_server(root)) as client:
            s = Session(client)
            await _plan_phase(s, root)
            last = await _rtl_phase(s, root)
            status = await s.call("status", run_id=last["run_id"])
            # A note on a gate over a file no agent writes (the PR record) reopens nothing.
            await s.call("approve", gate_id="pr:timer", reject=True, note="rename en")
            again = await s.call("next_task", target="rtl:timer")
            assert "revised" not in again and not again["tasks"] and again["stopped"]
            await s.call("approve", gate_id="pr:timer")  # the reviewer, not the command
            final = await s.call("next_task", target="rtl:timer")
            return last, status, final

    last, status, final = asyncio.run(_session())
    assert status["run_id"] == last["run_id"]
    assert status["target"] == "rtl:timer"
    chain = {row["instance"]: row["state"] for row in status["chain"]}
    assert list(chain)[:2] == [PLAN, EXPAND] and list(chain)[-2:] == [OPEN_PR, PR_GATE]
    assert chain[PR_GATE] == "waiting_gate" and chain[OPEN_PR] == "done"

    block = status["blocks"]["timer"]
    tasks = {t["instance"]: t for t in block["tasks"]}
    assert set(tasks) == {PLAN, *MODULE_IDS, TB, REVIEW}
    assert all(t["status"] == "accepted" for t in tasks.values())
    for iid, rel in zip(MODULE_IDS, RTL_FILES, strict=True):
        assert tasks[iid]["files"] == [rel]
        checks = {c["check_id"]: c["status"] for c in tasks[iid]["checks"]}
        assert set(checks) == {"format", "lint", "naming", "style", "hardcode"}
        assert set(checks.values()) == {"pass"}
    assert tasks[TB]["files"] == [TB_FILE]
    assert [c["check_id"] for c in tasks[TB]["checks"]] == ["tb_static"]
    assert tasks[TB]["checks"][0]["status"] == "pass"

    review = block["review"]
    assert review["verdict"] == "changes_requested"
    assert [c["severity"] for c in review["comments"]] == ["major"]
    assert review["other_comments"] == 0

    record = json.loads((root / PR_RECORD).read_text(encoding="utf-8"))
    pr = block["pr"]
    assert pr["branch"] == record["branch"] == f"chipgraph/{record['branch'].split('/')[1]}/timer"
    assert pr["head_sha"] == record["head_sha"] and pr["url"] is None
    assert pr["files"] == [*RTL_FILES, TB_FILE] and pr["gate"] == "pr:timer"

    # After the reviewer's approval the target is done.
    assert final["done"] and not final["tasks"]


def test_status_of_a_run_that_is_not_a_named_target(project: Path) -> None:
    async def _session() -> dict[str, Any]:
        async with Client(build_server(project)) as client:
            s = Session(client)
            await s.call("next_task", target="digital-rtl/plan[block=timer]")
            return await s.call("status")

    status = asyncio.run(_session())
    assert status["target"] == "digital-rtl/plan[block=timer]"
    assert status["chain"] == [] and status["blocks"] == {}
