"""M2-02a: `make_scheduler` wires the agent rule loop for an in-process runtime.

The project is built in `tmp_path`: a git repo, a profile with `runtime: generic`, a
project pack with one agent rule and one `gen` rule, and a `cmd` check. The runtime is
the test-only fake, registered in the context's plugin registry under the profile's
runtime name (no production runtime ships yet).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
import yaml
from agent_rule_helpers import FakeRuntime, Step

from chipgraph.adapters.runtime.claude_code import ClaudeCodeRuntime
from chipgraph.app.build import agent_executor, make_scheduler
from chipgraph.app.context import AppContext
from chipgraph.core.engine.agent_rule import AgentRuleExecutor
from chipgraph.core.engine.scheduler import AgentStub
from chipgraph.core.runtime import AgentRuntimeExecutor
from chipgraph.core.state.journal import read

TASK = "demo/write_a[]"
OUT = "rtl/a.sv"
_LINT = (
    "import pathlib, sys; t = pathlib.Path('rtl/a.sv').read_text(); "
    "ok = 'module a' in t; print('' if ok else 'rtl/a.sv: no module a'); "
    "sys.exit(0 if ok else 1)"
)


def _project(root: Path, runtime: str = "generic") -> Path:
    pack = root / ".chipgraph" / "packs" / "demo"
    (pack / "rules").mkdir(parents=True)
    (pack / "pack.yml").write_text(
        yaml.safe_dump({"name": "demo", "version": "0.1.0", "provides": {"rules": ["rules"]}})
    )
    (pack / "rules" / "write_a.yml").write_text(
        yaml.safe_dump(
            {
                "rule": "write_a",
                "kind": "agent",
                "role": "author",
                "inputs": [{"path": "spec.md"}],
                "outputs": [OUT],
                "checks": ["demo_lint"],
                "budget": {"tries": 2},
            }
        )
    )
    (pack / "rules" / "copy_a.yml").write_text(
        yaml.safe_dump(
            {
                "rule": "copy_a",
                "kind": "gen",
                "inputs": [{"path": OUT}],
                "outputs": ["build/a.txt"],
                "run": {
                    "use": "cmd",
                    "args": {
                        "cmd": [
                            "python3",
                            "-c",
                            "import pathlib; pathlib.Path('build').mkdir(exist_ok=True); "
                            "pathlib.Path('build/a.txt').write_text('copied')",
                        ]
                    },
                },
            }
        )
    )
    profile = {
        "project": "demo",
        "runtime": runtime,
        "packs": ["demo"],
        "adapters": {"demo_lint": {"use": "cmd", "cmd": ["python3", "-c", _LINT]}},
    }
    (root / ".chipgraph.yml").write_text(yaml.safe_dump(profile))
    (root / "spec.md").write_text("write module a\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    return root


def test_a_registered_runtime_gets_the_agent_rule_loop(tmp_path: Path) -> None:
    ctx = AppContext.load(_project(tmp_path))
    runtime = FakeRuntime(tmp_path, [Step(content="// todo\n"), Step(content="module a;\n")])
    ctx.registry.register("runtime", "generic", runtime)
    executor = agent_executor(ctx)
    assert isinstance(executor, AgentRuleExecutor)
    assert executor.runtime is runtime

    scheduler = make_scheduler(ctx, "*")
    summary = asyncio.run(scheduler.run("*"))

    assert summary.ok, summary
    assert set(summary.done) == {TASK, "demo/copy_a[]"}
    assert runtime.calls == 2
    assert "check `demo_lint`: fail" in runtime.tasks[1].context["feedback"]
    events = read(ctx.layout.journal(summary.run_id)).events
    checks = [e for e in events if e.type == "check_result" and e.rule_instance == TASK]
    assert [e.payload["status"] for e in checks] == ["fail", "pass"]  # once per try


def test_exhausted_under_make_scheduler_stays_stopped_until_rewound(tmp_path: Path) -> None:
    ctx = AppContext.load(_project(tmp_path))
    runtime = FakeRuntime(tmp_path, lambda n, _t: Step(content=f"// todo {n}\n"))
    ctx.registry.register("runtime", "generic", runtime)

    first = asyncio.run(make_scheduler(ctx, "*").run("*"))
    assert first.failed == (TASK,)
    assert first.blocked == ("demo/copy_a[]",)
    assert runtime.calls == 2
    handoff = (ctx.layout.run_dir(first.run_id) / "HANDOFF.md").read_text()
    assert "budget exhausted (tries) after 2 of 2 tries" in handoff

    again = asyncio.run(make_scheduler(ctx, "*").run("*"))
    assert again.failed == (TASK,)
    assert runtime.calls == 2  # a fresh scheduler remembers the stop

    make_scheduler(ctx, "*").rewind(TASK)
    asyncio.run(make_scheduler(ctx, "*").run("*"))
    assert runtime.calls == 4


@pytest.mark.parametrize("runtime", ["generic", "claude-agent-sdk"])
def test_no_registered_runtime_keeps_the_stub(tmp_path: Path, runtime: str) -> None:
    ctx = AppContext.load(_project(tmp_path, runtime=runtime))
    assert isinstance(agent_executor(ctx), AgentStub)


def test_claude_code_is_unchanged(tmp_path: Path) -> None:
    ctx = AppContext.load(_project(tmp_path, runtime="claude-code"))
    executor = agent_executor(ctx)
    assert isinstance(executor, AgentRuntimeExecutor)
    assert isinstance(executor.runtime, ClaudeCodeRuntime)
