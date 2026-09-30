"""`ClaudeCodeRuntime`, the executor choice in `make_scheduler`, and the diff snapshot
(`chipgraph.adapters.runtime.claude_code`, M1-11)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from chipgraph.adapters.runtime.claude_code import RUN_COMMAND, ClaudeCodeRuntime
from chipgraph.adapters.runtime.claude_code.service import snapshot
from chipgraph.app.build import agent_executor
from chipgraph.app.context import AppContext
from chipgraph.core.contracts import (
    AgentResult,
    ArtifactRef,
    Budget,
    InputSpec,
    RuleInstance,
    RuleSpec,
)
from chipgraph.core.engine.scheduler import AgentStub
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import AgentRuntimeExecutor, TaskQueue

TASK = "demo/write_a[]"


def _project(root: Path, runtime: str | None = None) -> AppContext:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    profile = "project: demo\n" + (f"runtime: {runtime}\n" if runtime else "")
    (root / ".chipgraph.yml").write_text(profile)
    (root / "spec.md").write_text("spec v1\n")
    return AppContext.load(root)


def _task() -> AgentTask:
    instance = RuleInstance(
        rule_id="demo/write_a",
        inputs=(ArtifactRef(kind="doc", path="spec.md"),),
        outputs=(ArtifactRef(kind="rtl", path="rtl/a.sv"),),
        instance_id=TASK,
    )
    return AgentTask(
        instance=instance, role="author", allowed_writes=("rtl/a.sv",), budget=Budget(tries=1)
    )


@pytest.fixture
def ctx(tmp_path: Path) -> AppContext:
    return _project(tmp_path / "proj")


def _run(runtime: ClaudeCodeRuntime) -> AgentResult:
    return asyncio.run(runtime.run_task(_task()))


def test_pending_task_waits_for_the_plugin_command(ctx: AppContext) -> None:
    runtime = ClaudeCodeRuntime(TaskQueue(ctx.layout), ctx.store)
    result = _run(runtime)
    assert result.status == "needs_human"
    assert RUN_COMMAND in result.open_questions[0]
    assert "is ready" in result.open_questions[0]
    assert _run(runtime) == result  # idempotent
    assert [r.status for r in TaskQueue(ctx.layout).all()] == ["ready"]


def _accept(ctx: AppContext) -> TaskQueue:
    queue = TaskQueue(ctx.layout)
    asyncio.run(ClaudeCodeRuntime(queue, ctx.store).run_task(_task()))
    queue.dispatch(TASK, run_id="r1", baseline={})
    (ctx.root / "rtl").mkdir()
    (ctx.root / "rtl" / "a.sv").write_text("module a; endmodule\n")
    queue.mark_submitted(TASK)
    hashes = ctx.store.current_hashes(_task().instance.outputs)
    queue.accept(
        TASK,
        result=AgentResult(status="done"),
        output_hashes={"rtl/a.sv": hashes[".:rtl/a.sv"]},
    )
    return queue


def test_accepted_task_is_done_while_its_outputs_hash_as_accepted(ctx: AppContext) -> None:
    queue = _accept(ctx)
    runtime = ClaudeCodeRuntime(queue, ctx.store)
    assert _run(runtime) == AgentResult(status="done", files_written=("rtl/a.sv",))

    (ctx.root / "rtl" / "a.sv").write_text("module a; // edited\nendmodule\n")
    reopened = _run(runtime)
    assert reopened.status == "needs_human"
    assert queue.require(TASK).status == "ready"


def test_changed_input_reopens_an_accepted_task(ctx: AppContext) -> None:
    queue = _accept(ctx)
    (ctx.root / "spec.md").write_text("spec v2\n")
    assert _run(ClaudeCodeRuntime(queue, ctx.store)).status == "needs_human"
    assert queue.require(TASK).status == "ready"


def test_budget_exhausted_is_reported_with_the_last_rejection(ctx: AppContext) -> None:
    queue = TaskQueue(ctx.layout)
    runtime = ClaudeCodeRuntime(queue, ctx.store)
    _run(runtime)
    queue.dispatch(TASK, run_id="r1", baseline={})
    queue.mark_submitted(TASK)
    queue.reject(TASK, result=AgentResult(status="failed"), reasons=("check lint fail: x",))
    result = _run(runtime)
    assert result.status == "budget_exhausted"
    assert "used all 1 tries" in result.open_questions[0]
    assert "check lint fail: x" in result.open_questions[0]

    outcome = asyncio.run(AgentRuntimeExecutor(runtime).execute(_rule(), _task().instance))
    assert (outcome.ok, outcome.failure_label) == (False, "verification")
    assert "HANDOFF.md" in outcome.message


def _rule() -> RuleSpec:
    return RuleSpec(
        id="demo/write_a",
        kind="agent",
        role="author",
        inputs=(InputSpec(source="path", selector="spec.md"),),
        outputs=("rtl/a.sv",),
        budget=Budget(tries=1),
    )


def test_needs_human_carries_the_open_questions(ctx: AppContext) -> None:
    queue = TaskQueue(ctx.layout)
    runtime = ClaudeCodeRuntime(queue, ctx.store)
    _run(runtime)
    queue.dispatch(TASK, run_id="r1", baseline={})
    queue.needs_human(
        TASK, result=AgentResult(status="needs_human", open_questions=("which reset?",))
    )
    assert _run(runtime).open_questions == ("which reset?",)


@pytest.mark.parametrize(
    ("runtime", "kind"),
    [
        ("claude-code", AgentRuntimeExecutor),
        ("generic", AgentStub),
        ("claude-agent-sdk", AgentStub),
    ],
)
def test_agent_executor_follows_profile_runtime(tmp_path: Path, runtime: str, kind: type) -> None:
    ctx = _project(tmp_path / "p", runtime=runtime)
    assert isinstance(agent_executor(ctx), kind)


def test_default_runtime_is_claude_code(ctx: AppContext) -> None:
    executor = agent_executor(ctx)
    assert isinstance(executor, AgentRuntimeExecutor)
    assert isinstance(executor.runtime, ClaudeCodeRuntime)


def test_snapshot_lists_tracked_and_untracked_but_not_ignored_or_state(ctx: AppContext) -> None:
    root = ctx.root
    (root / ".gitignore").write_text("build/\n")
    (root / "build").mkdir()
    (root / "build" / "out.json").write_text("{}")
    (root / "new.txt").write_text("new")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text("{}")
    subprocess.run(["git", "add", "spec.md"], cwd=root, check=True)
    hashes = asyncio.run(snapshot(ctx, extra=("build/out.json",)))
    assert "spec.md" in hashes and "new.txt" in hashes and ".gitignore" in hashes
    assert "build/out.json" in hashes  # an output is always included
    assert not any(p.startswith((".chipgraph/state/", ".claude/", ".git/")) for p in hashes)


def test_snapshot_without_git_walks_the_tree(tmp_path: Path) -> None:
    root = tmp_path / "nogit"
    root.mkdir()
    (root / ".chipgraph.yml").write_text("project: demo\n")
    (root / "a").mkdir()
    (root / "a" / "b.txt").write_text("b")
    ctx = AppContext.load(root)
    hashes = asyncio.run(snapshot(ctx))
    assert set(hashes) == {".chipgraph.yml", "a/b.txt"}
