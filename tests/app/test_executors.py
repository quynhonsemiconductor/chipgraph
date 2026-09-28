"""Tests for `chipgraph.app.executors`."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import init_git, make_instance, write_profile

from chipgraph.app.context import AppContext
from chipgraph.app.executors import HumanExecutor, RunExecutor
from chipgraph.core.contracts import RunSpec
from chipgraph.core.contracts.rule import RuleSpec


def test_run_executor_runs_gen_rule_and_writes_output(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")
    ctx = AppContext.load(tmp_path)

    rule = RuleSpec(
        id="demo/gen",
        kind="gen",
        outputs=("out.txt",),
        run=RunSpec(
            use="cmd",
            args={"cmd": ["python3", "-c", "open('out.txt', 'w').write('hi')"]},
        ),
    )
    instance = make_instance("demo/gen", output_path="out.txt")

    outcome = asyncio.run(RunExecutor(ctx).execute(rule, instance))

    assert outcome.ok
    assert (tmp_path / "out.txt").read_text() == "hi"


def test_run_executor_reports_tool_failure(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")
    ctx = AppContext.load(tmp_path)

    rule = RuleSpec(
        id="demo/gen",
        kind="gen",
        outputs=("out.txt",),
        run=RunSpec(use="cmd", args={"cmd": ["python3", "-c", "raise SystemExit(1)"]}),
    )
    instance = make_instance("demo/gen", output_path="out.txt")

    outcome = asyncio.run(RunExecutor(ctx).execute(rule, instance))

    assert not outcome.ok
    assert outcome.failure_label == "verification"


def test_human_executor_waits_for_missing_outputs(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")
    ctx = AppContext.load(tmp_path)

    rule = RuleSpec(id="demo/write_spec", kind="human", outputs=("doc/spec.md",))
    instance = make_instance("demo/write_spec", output_path="doc/spec.md")

    outcome = asyncio.run(HumanExecutor(ctx).execute(rule, instance))

    assert not outcome.ok
    assert outcome.failure_label == "planning"
    assert "doc/spec.md" in outcome.message


def test_human_executor_ok_once_outputs_exist(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc" / "spec.md").write_text("written by a human")
    ctx = AppContext.load(tmp_path)

    rule = RuleSpec(id="demo/write_spec", kind="human", outputs=("doc/spec.md",))
    instance = make_instance("demo/write_spec", output_path="doc/spec.md")

    outcome = asyncio.run(HumanExecutor(ctx).execute(rule, instance))

    assert outcome.ok
