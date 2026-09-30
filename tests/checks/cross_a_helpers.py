"""Helpers for the M1-07 part A cross-check tests (`spec_schema`, `cross_chip`,
`ports_diff`, `duplicate`).

Each accept case seeds a FAIL on a copy of `examples/tinysoc`: copy the example to
`tmp_path`, `git init`, commit, `chipgraph ingest`, apply one edit, re-ingest, then run
the check. `tinysoc_project` gives a ready pass-case project; `edit_and_reingest`
applies an edit and rebuilds the model; `run_check` runs one check against the copy.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.types import RunResult, ToolContext

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


class _NoRunner:
    """A `Runner` the model-based cross checks never call."""

    name = "no-runner"

    async def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> RunResult:
        raise AssertionError("cross checks must not run subprocess commands")


def tinysoc_project(tmp_path: Path) -> Path:
    """Copy `examples/tinysoc` to `tmp_path`, init git, commit, and ingest it.

    Returns the project root, with a Design Model already built (the pass case).
    """
    root = tmp_path / "tinysoc"
    shutil.copytree(_EXAMPLE, root)
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.st", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    _ingest(root)
    return root


def edit_and_reingest(root: Path, rel: str, old: str, new: str) -> None:
    """Replace `old` with `new` in `root/rel` (must occur exactly once), then re-ingest."""
    path = root / rel
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"expected {old!r} exactly once in {rel}, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    _ingest(root)


def write_and_reingest(root: Path, rel: str, content: str) -> None:
    """Write `content` to `root/rel` (creating parents), then re-ingest."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _ingest(root)


def run_check(
    check: object, root: Path, *, args: dict[str, object] | None = None, block: str | None = None
) -> CheckResult:
    """Run a built-in check instance against the project at `root`."""
    params = {"block": block} if block is not None else {}
    ctx = ToolContext(repo_root=root, runner=_NoRunner(), params=params)
    check_id = check.id  # type: ignore[attr-defined]
    spec = CheckSpec(id=check_id, capability=check_id, adapter=check_id, args=args or {})
    return asyncio.run(check.run(spec, ctx))  # type: ignore[attr-defined]


def rules(result: CheckResult) -> list[str]:
    """The rule ids of `result`'s issues, in order."""
    return [i.rule for i in result.issues]


def make_instance(block: str | None = None) -> object:
    """A throwaway `RuleInstance` for driving `ProfileCheckRunner`."""
    from chipgraph.core.contracts import ArtifactRef, RuleInstance

    params = {"block": block} if block is not None else {}
    return RuleInstance(
        rule_id="demo/instance",
        params=params,
        outputs=(ArtifactRef(kind="report", path="out/r.json"),),
        instance_id=RuleInstance.make_id("demo/instance", params),
    )


def stage_model(root: Path, entities: Sequence[object], relations: Sequence[object] = ()) -> None:
    """Write a hand-built Design Model to `root`'s default store (no ingest needed).

    Lets a test exercise a check on a small, precise model (e.g. an interrupt with no
    block) without seeding it through the full example project.
    """
    from chipgraph.core.model.model import DesignModel
    from chipgraph.core.model.store import ModelStore, default_model_db_path

    model = DesignModel.build(entities, relations)  # type: ignore[arg-type]
    db_path = default_model_db_path(root)
    ModelStore(db_path).write(model, build_inputs_hash="test")


def _ingest(root: Path) -> None:
    from chipgraph.app.context import AppContext
    from chipgraph.app.ingest import run_ingest

    ctx = AppContext.load(root)
    ctx.require_profile()
    run_ingest(ctx)
