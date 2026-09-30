"""Helpers for the structural CDC check tests (M1-19).

Not a `conftest.py` (the brief forbids adding one here): the CDC tests import these
directly. The core helper copies `examples/tinysoc` into a `tmp_path`, `git init`s it,
ingests the Design Model, applies one edit and re-ingests -- exactly how the accept
criterion seeds each case on a throwaway copy, never touching the source tree.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from chipgraph.checks import CdcStructCheck
from chipgraph.checks._cdc_analyze import Crossing, analyse
from chipgraph.checks._cdc_elaborate import elaborate
from chipgraph.cli import app
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.types import RunResult, ToolContext

_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"
_FIXTURES = Path(__file__).resolve().parent / "cdc_fixtures"

_TIMER_SYNC_CELL = "tiny_sync2"


class _FakeRunner:
    """A `Runner` the CDC check never calls (it reads the model and elaborates RTL)."""

    name = "fake-runner"

    async def run(self, cmd: object, **kwargs: object) -> RunResult:  # type: ignore[override]
        raise AssertionError("the CDC check must not run subprocess commands")


def ingest(root: Path) -> None:
    """Run `chipgraph ingest` on `root`, asserting it succeeds."""
    result = CliRunner().invoke(app, ["-C", str(root), "ingest"])
    assert result.exit_code == 0, result.output


def make_project(tmp_path: Path, *, edit: Callable[[Path], None] | None = None) -> Path:
    """Copy `examples/tinysoc` to `tmp_path`, git-init, ingest, optionally edit+re-ingest."""
    root = tmp_path / "tinysoc"
    shutil.copytree(_EXAMPLE_ROOT, root)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=root,
        check=True,
    )
    ingest(root)
    if edit is not None:
        edit(root)
        ingest(root)
    return root


def make_ctx(root: Path, *, block: str | None = None) -> ToolContext:
    """A `ToolContext` rooted at `root`, optionally scoped to `block`."""
    params = {"block": block} if block is not None else {}
    return ToolContext(repo_root=root, runner=_FakeRunner(), params=params)


def run_check(
    root: Path,
    *,
    block: str | None = None,
    sync_cells: list[str] | None = None,
    scope: list[str] | None = None,
) -> CheckResult:
    """Run `CdcStructCheck` on `root`, scoped to `block`, synchronously."""
    args: dict[str, object] = {
        "sync_cells": sync_cells if sync_cells is not None else [_TIMER_SYNC_CELL]
    }
    if scope is not None:
        args["scope"] = scope
    spec = CheckSpec(id="cdc_struct", capability="cdc_struct", adapter="cdc_struct", args=args)
    return asyncio.run(CdcStructCheck().run(spec, make_ctx(root, block=block)))


def analyse_fixture(name: str, *, sync_cells: list[str] | None = None) -> list[Crossing]:
    """Elaborate a `cdc_fixtures/<name>` file and return the crossings of every module."""
    import re

    path = _FIXTURES / name
    result = elaborate(
        [path],
        sync_cells=frozenset(sync_cells or []),
        clock_matchers=[re.compile(r"^i_clk"), re.compile(r"^clk")],
    )
    assert not result.diagnostics, [d.message for d in result.diagnostics]
    crossings: list[Crossing] = []
    for module in sorted(result.modules):
        crossings.extend(analyse(result.modules[module]))
    return crossings


def find_issue(result: CheckResult, rule: str):  # type: ignore[no-untyped-def]
    """The first issue in `result` with `rule`, or None."""
    for issue in result.issues:
        if issue.rule == rule:
            return issue
    return None


def rules(result: CheckResult) -> set[str]:
    """The set of rule ids in `result`'s issues."""
    return {issue.rule for issue in result.issues}


def assert_findings_layer(root: Path, *, block: str, expected_layer: int) -> None:
    """Run `cdc_struct` through `ProfileCheckRunner` and assert findings land in a layer."""
    from chipgraph.app.checks import ProfileCheckRunner
    from chipgraph.app.context import AppContext
    from chipgraph.core.contracts import ArtifactRef, RuleInstance
    from chipgraph.core.state.findings import FindingStore

    ctx = AppContext.load(root)
    runner = ProfileCheckRunner(ctx)
    rule_id = "tinysoc/cdc_struct"
    params = {"block": block}
    instance = RuleInstance(
        rule_id=rule_id,
        params=params,
        outputs=(ArtifactRef(kind="other", path="chip.yml"),),
        instance_id=RuleInstance.make_id(rule_id, params),
    )
    result = asyncio.run(runner.run("cdc_struct", instance))
    assert result.status == "fail", [i.msg for i in result.issues]
    findings = FindingStore(ctx.layout).list(source="check:cdc_struct")
    assert findings, "expected findings to be stored"
    assert {f.layer for f in findings} == {expected_layer}


__all__ = [
    "analyse_fixture",
    "assert_findings_layer",
    "find_issue",
    "ingest",
    "make_ctx",
    "make_project",
    "rules",
    "run_check",
]
