"""M2-11a: what a role may not read comes from the profile's layout, in both runtimes.

The RTL author is refused the testbench paths of the layout (`layout.tb`) and the
testbench author the RTL paths (`layout.rtl`), whatever the graph names and whatever the
files are called. Both runtimes ask `chipgraph.core.runtime.roles.task_denied_reads`:

- the in-process loop (`AgentRuleExecutor`, runtime `generic`/`fake`) filters the redo
  text of a failed check by it;
- the Claude Code runtime sets it on the task (`denied_reads`) for the guard, and filters
  what `submit` tells the author by it.

The paths used here are named so that nothing but the layout denies them: no `rtl`/`tb`
suffix, no `test_`/`tb_` name, not an input or output of any rule.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from plan_helpers import approve_plan
from rtl_target_helpers import (
    EXPAND,
    TB,
    ChainRuntime,
    module_id,
    planned_timer,
    register,
    target_project,
)

from chipgraph.adapters.runtime.claude_code import service
from chipgraph.app.build import denied_reads_for, make_scheduler, project_graph
from chipgraph.app.context import AppContext
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.core.runtime import TaskQueue

SECRET = "zqx_secret_marker"
BENCH = "bench/timer/data.dat"  # layout.tb only
HDL = "gen/timer/core.dat"  # layout.rtl only
MODULE = module_id("tiny_timer_counter")
RTL_LAYOUT = ["rtl/tiny_{block}*.sv", "gen/{block}/*"]
TB_LAYOUT = ["dv/{block}/**", "bench/{block}/*"]


class LeakyCheck:
    """A check that fails once with an issue in `path` (and a message naming a marker)."""

    name = "leaky"
    id = "leaky"

    def __init__(self, path: str) -> None:
        self.path = path
        self.calls = 0

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        self.calls += 1
        if self.calls > 1:
            return CheckResult(
                check_id=spec.id, status="pass", issues=(), duration_s=0.0, idempotency_key="k"
            )
        issue = Issue(
            rule="leak",
            severity="error",
            file=self.path,
            line=3,
            msg=f"{self.path}:3 {SECRET} differs; expected 0x2, got 0x0",
        )
        return CheckResult(
            check_id=spec.id,
            status="fail",
            issues=(issue,),
            duration_s=0.0,
            idempotency_key="k",
            log_tail=f"{self.path}:3 {SECRET}",
        )


def _project(tmp_path: Path, *, layout_only: bool = True, runtime: str = "generic") -> Path:
    """tinysoc with the target's packs; with `layout_only`, the layout names the paths."""
    root = target_project(tmp_path / "tinysoc", real_lint=False)
    path = root / ".chipgraph.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if layout_only:
        data["layout"]["rtl"] = RTL_LAYOUT
        data["layout"]["tb"] = TB_LAYOUT
    data["adapters"]["naming"] = {"use": "leaky"}
    data["adapters"]["tb_static"] = {"use": "leaky"}
    data["runtime"] = runtime
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return root


def _approved(root: Path) -> None:
    """The planner's plan for timer, written, approved and expanded (in process)."""
    ctx = register(AppContext.load(root), ChainRuntime(root, [planned_timer(root)]))
    asyncio.run(make_scheduler(ctx, EXPAND).run(EXPAND))
    approve_plan(root, "timer")
    ctx = register(AppContext.load(root), ChainRuntime(root, [planned_timer(root)]))
    summary = asyncio.run(make_scheduler(ctx, EXPAND).run(EXPAND))
    assert summary.ok, summary


def _run_in_process(root: Path, target: str, path: str) -> tuple[ChainRuntime, LeakyCheck]:
    """Build `target` with the scripted runtime and a check that fails once in `path`."""
    ctx = AppContext.load(root)
    scripted = ChainRuntime(root, [planned_timer(root)], name=ctx.require_profile().profile.runtime)
    register(ctx, scripted)
    leaky = LeakyCheck(path)
    ctx.registry.register("check", "leaky", leaky)
    summary = asyncio.run(make_scheduler(ctx, target).run(target))
    assert summary.ok, summary
    return scripted, leaky


# --- in process (the AgentRuleExecutor path) --------------------------------------------------


def test_the_rtl_author_is_not_shown_a_layout_only_testbench_path(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _approved(root)
    runtime, leaky = _run_in_process(root, MODULE, BENCH)
    assert leaky.calls == 2
    [first, second] = [t for t in runtime.tasks if t.instance.instance_id == MODULE]
    assert "feedback" not in first.context
    feedback = second.context["feedback"]
    assert BENCH not in feedback and SECRET not in feedback
    assert "withheld" in feedback.lower()


def test_the_tb_author_is_not_shown_a_layout_only_rtl_path(tmp_path: Path) -> None:
    root = _project(tmp_path)
    runtime, leaky = _run_in_process(root, TB, HDL)
    assert leaky.calls == 2
    feedback = runtime.tasks[1].context["feedback"]
    assert HDL not in feedback and SECRET not in feedback


def test_without_the_layout_the_same_failure_is_shown(tmp_path: Path) -> None:
    """The control: the path is not denied by anything else, so the layout is the cause."""
    root = _project(tmp_path, layout_only=False)
    _approved(root)
    runtime, _ = _run_in_process(root, MODULE, BENCH)
    second = [t for t in runtime.tasks if t.instance.instance_id == MODULE][1]
    assert BENCH in second.context["feedback"] and SECRET in second.context["feedback"]
    root_tb = _project(tmp_path / "again", layout_only=False)
    runtime, _ = _run_in_process(root_tb, TB, HDL)
    assert HDL in runtime.tasks[1].context["feedback"]


def test_the_executor_gets_the_same_list_the_claude_code_runtime_computes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _approved(root)
    ctx = AppContext.load(root)
    graph = project_graph(ctx)
    for iid, path in ((MODULE, "bench/timer/*"), (TB, "gen/timer/*")):
        instance = graph.instances[iid]
        keep = [ref.path for ref in instance.outputs if ref.path]
        role = graph.rules[instance.rule_id].role
        denied = denied_reads_for(ctx, graph, role, keep)
        assert path in denied, (iid, denied)
        assert not set(keep) & set(denied)


# --- the Claude Code runtime ---------------------------------------------------------------


def _next(root: Path, target: str) -> dict[str, Any]:
    return asyncio.run(service.next_task(AppContext.load(root), target))


def _submit(root: Path, task_id: str) -> dict[str, Any]:
    ctx = AppContext.load(root)
    ctx.registry.register("check", "leaky", LeakyCheck(task_paths[task_id]))
    return asyncio.run(service.submit(ctx, task_id, service.SubmitReport()))


task_paths: dict[str, str] = {MODULE: BENCH, TB: HDL}


@pytest.mark.parametrize(
    ("task_id", "layout_only"), [(MODULE, "bench/timer/*"), (TB, "gen/timer/*")]
)
def test_the_claude_code_runtime_denies_the_layout_path_and_filters_submit(
    tmp_path: Path, task_id: str, layout_only: str
) -> None:
    root = _project(tmp_path)
    _approved(root)
    data = yaml.safe_load((root / ".chipgraph.yml").read_text(encoding="utf-8"))
    data["runtime"] = "claude-code"
    (root / ".chipgraph.yml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    answer = _next(root, task_id)
    assert task_id in {t["task_id"] for t in answer["tasks"]}
    ctx = AppContext.load(root)
    record = TaskQueue(ctx.layout).get(task_id)
    assert record is not None
    assert layout_only in record.denied_reads
    graph = project_graph(ctx)
    assert record.denied_reads == denied_reads_for(
        ctx, graph, record.role, record.allowed_writes
    )  # the one function

    out = root / record.allowed_writes[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("// first try\n", encoding="utf-8")
    first = _submit(root, task_id)
    assert first["status"] == "rejected" and "withheld" in first["redo"]
    text = str(first)
    assert SECRET not in text and task_paths[task_id] not in text
