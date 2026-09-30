"""Helpers for the M1-07 part B cross-check tests (`trace`, `connect`, `hardcode`).

Not a `conftest.py` (the brief forbids one here): the check tests import these directly.
The core helper copies `examples/tinysoc` into a `tmp_path`, `git init`s it, ingests the
Design Model, optionally applies one edit and re-ingests -- exactly how the accept
criterion seeds each fail case on a throwaway copy, never touching the source tree.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from chipgraph.cli import app
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.protocols import Check
from chipgraph.core.plugin_api.types import RunResult, ToolContext

_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


class _FakeRunner:
    """A `Runner` the cross checks never call (they read the model, run no subprocess)."""

    name = "fake-runner"

    async def run(self, cmd: object, **kwargs: object) -> RunResult:  # type: ignore[override]
        raise AssertionError("cross checks must not run subprocess commands")


def ingest(root: Path) -> None:
    """Run `chipgraph ingest` on `root`, asserting it succeeds."""
    result = CliRunner().invoke(app, ["-C", str(root), "ingest"])
    assert result.exit_code == 0, result.output


def make_project(
    tmp_path: Path,
    *,
    edit: Callable[[Path], None] | None = None,
    name: str = "tinysoc",
) -> Path:
    """Copy `examples/tinysoc` to `tmp_path/name`, git-init, ingest, optionally edit+re-ingest.

    Returns the project root. When `edit` is given it is called after the first ingest,
    then the model is rebuilt, mirroring how a fail case is seeded on a copy.
    """
    root = tmp_path / name
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


def run_check(check: Check, args: dict[str, object], ctx: ToolContext) -> CheckResult:
    """Run `check` with `args` under `ctx`, synchronously."""
    spec = CheckSpec(id=check.id, capability=check.id, adapter=check.id, args=args)
    return asyncio.run(check.run(spec, ctx))


def replace_in_file(root: Path, rel: str, old: str, new: str) -> None:
    """Replace the first occurrence of `old` with `new` in `root/rel` (asserts it is there)."""
    path = root / rel
    text = path.read_text()
    assert old in text, f"{old!r} not found in {rel}"
    path.write_text(text.replace(old, new, 1))


def rules(result: CheckResult) -> set[str]:
    """The set of rule ids in `result`'s issues."""
    return {issue.rule for issue in result.issues}


def find_issue(result: CheckResult, rule: str):  # type: ignore[no-untyped-def]
    """The first issue in `result` with `rule`, or None."""
    for issue in result.issues:
        if issue.rule == rule:
            return issue
    return None


def assert_findings_layer(root: Path, check_id: str, *, expected_layer: int) -> None:
    """Run `check_id` through `ProfileCheckRunner` and assert its findings land in `expected_layer`.

    Exercises that a check's issues become findings at the DESIGN.md 4.8 layer
    `app.findings.LAYER_BY_CHECK` maps it to (M1-18 wiring).
    """
    from chipgraph.app.checks import ProfileCheckRunner
    from chipgraph.app.context import AppContext
    from chipgraph.core.contracts import ArtifactRef, RuleInstance
    from chipgraph.core.state.findings import FindingStore

    ctx = AppContext.load(root)
    runner = ProfileCheckRunner(ctx)
    rule_id = f"tinysoc/{check_id}"
    instance = RuleInstance(
        rule_id=rule_id,
        params={},
        outputs=(ArtifactRef(kind="other", path="chip.yml"),),
        instance_id=RuleInstance.make_id(rule_id, {}),
    )
    result = asyncio.run(runner.run(check_id, instance))
    assert result.status == "fail", [i.msg for i in result.issues]
    findings = FindingStore(ctx.layout).list(source=f"check:{check_id}")
    assert findings, "expected findings to be stored"
    assert {f.layer for f in findings} == {expected_layer}


__all__ = [
    "assert_findings_layer",
    "find_issue",
    "ingest",
    "make_ctx",
    "make_project",
    "replace_in_file",
    "rules",
    "run_check",
]
