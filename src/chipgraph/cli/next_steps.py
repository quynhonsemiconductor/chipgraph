"""What is next after a build of a named target (`chipgraph plan`, `chipgraph rtl`).

From the run summary and the graph: each gate the run waits on, with how to approve it
(and, for a pull request gate, the PR's URL from `pr/<block>.json`); the agent tasks
queued for the Claude Code session (`/chipgraph:run`); what failed; and, when nothing
waits, the pull request of the block if there is one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chipgraph.app.context import AppContext
from chipgraph.core.engine import gate as gate_mod
from chipgraph.core.engine.graph import BuildGraph
from chipgraph.core.engine.scheduler import RunSummary
from chipgraph.core.runtime import ACTIVE_STATUSES, OPEN_STATUSES, TaskQueue

RUN_COMMAND = "/chipgraph:run"
PR_RECORD = "pr/{block}.json"


def _pr(app_ctx: AppContext, block: str | None) -> dict[str, Any] | None:
    if block is None:
        return None
    path = app_ctx.root / PR_RECORD.format(block=block)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def next_steps(app_ctx: AppContext, graph: BuildGraph, summary: RunSummary) -> dict[str, Any]:
    """The next steps after `summary`: `lines` for a person, and the same as data."""
    lines: list[str] = []
    gates: list[dict[str, Any]] = []
    for iid in summary.waiting_gate:
        instance = graph.instances[iid]
        rule = graph.rules[instance.rule_id]
        gate_id = gate_mod.gate_id_for(rule.gate or "", instance.params)
        block = instance.params.get("block")
        item: dict[str, Any] = {"gate": gate_id, "instance": iid}
        pr = _pr(app_ctx, block) if gate_id.startswith("pr:") else None
        if pr is not None and pr.get("url"):
            item["url"] = pr["url"]
            lines.append(
                f"gate {gate_id}: review and approve the pull request {pr['url']} "
                f"(head {str(pr.get('head_sha', ''))[:12]}); then run this again"
            )
        else:
            if gate_id.startswith("plan:") and block is not None:
                lines.append(f"read the plan: chipgraph plan show {block}")
            lines.append(f"gate {gate_id}: chipgraph approve {gate_id} --instance '{iid}'")
        gates.append(item)

    queue = TaskQueue(app_ctx.layout)
    tasks: list[str] = []
    failed: list[str] = []
    for iid in summary.failed:
        record = queue.get(iid)
        if record is not None and (
            record.status in OPEN_STATUSES or record.status in ACTIVE_STATUSES
        ):
            tasks.append(iid)
        else:
            failed.append(iid)
    if tasks:
        lines.append(f"agent tasks waiting for {RUN_COMMAND} in Claude Code: {', '.join(tasks)}")
    for iid in failed:
        lines.append(f"failed: {iid} (see HANDOFF.md)")
    if summary.blocked and not lines:
        lines.append(f"blocked: {', '.join(summary.blocked)}")

    pr_done: dict[str, Any] | None = None
    if summary.ok:
        blocks = {
            graph.instances[i].params.get("block") for i in (*summary.done, *summary.skipped_fresh)
        }
        for block in sorted(b for b in blocks if b is not None):
            pr_done = _pr(app_ctx, block) or pr_done
        if pr_done is not None and pr_done.get("url"):
            lines.append(f"done: the pull request {pr_done['url']} is approved; merging is yours")
        elif pr_done is not None:
            lines.append(
                f"done: branch {pr_done.get('branch')} is approved ({pr_done.get('gate')}); "
                "merging is yours"
            )
        else:
            lines.append("done: nothing waits")
    return {"lines": lines, "gates": gates, "tasks": tasks, "failed": failed, "pr": pr_done}


def chain_rows(graph: BuildGraph, target: str, summary: RunSummary) -> list[tuple[str, str]]:
    """The instances of `target` in build order with where each stands after `summary`.

    `done`, `fresh` (already up to date), `failed`, `waiting <gate>` or `blocked`; an
    instance this run did not reach is `not run`.
    """
    states: dict[str, str] = {}
    for iid in summary.done:
        states[iid] = "done"
    for iid in summary.skipped_fresh:
        states[iid] = "fresh"
    for iid in summary.failed:
        states[iid] = "failed"
    for iid in summary.blocked:
        states[iid] = "blocked"
    for iid in summary.waiting_gate:
        instance = graph.instances[iid]
        gate = gate_mod.gate_id_for(graph.rules[instance.rule_id].gate or "", instance.params)
        states[iid] = f"waiting {gate}"
    selected = graph.select(target)
    return [(iid, states.get(iid, "not run")) for iid in graph.topo_order() if iid in selected]


def write_chain(
    handoff: Path, graph: BuildGraph, target: str, summary: RunSummary
) -> list[tuple[str, str]]:
    """Append the target's chain to a run's `HANDOFF.md` (when the file is there).

    A failure to write it never fails the build. Returns the rows.
    """
    rows = chain_rows(graph, target, summary)
    lines = [f"## Chain of {target}", ""]
    lines += [f"{i + 1}. `{iid}`: {state}" for i, (iid, state) in enumerate(rows)]
    try:
        text = handoff.read_text(encoding="utf-8")
        handoff.write_text(text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return rows


__all__ = ["RUN_COMMAND", "chain_rows", "next_steps", "write_chain"]
