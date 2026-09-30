"""`chipgraph try`: run chipgraph read-only against a repo, writing nothing into it.

`try` lets a user evaluate chipgraph on a repository before committing a `.chipgraph.yml`
(DESIGN.md 8.4). It infers a draft profile (or reads one), then runs only *read* commands:
`ingest` into a temporary state directory, `check`, and `audit` when that pack's API is
importable. It guarantees three things the tests pin:

- the repository is left byte-identical (its `.chipgraph/` is never created): every write
  goes to a private working copy under a temp directory, so the repo is only ever read;
- local plugins are disabled (`CHIPGRAPH_LOCAL_PLUGINS=0`): an untrusted repo must not run
  its own code during a `try`;
- it runs the `assist` pack's audit over the temporary state (no second ingest).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.core.contracts import ArtifactRef, CheckResult, RuleInstance
from chipgraph.learn.draft import build_naming_rules, build_profile_dict, dump_naming_rules_yaml
from chipgraph.learn.infer import DEFAULT_THRESHOLD, learn
from chipgraph.learn.models import LearnResult

_LOCAL_PLUGINS_ENV = "CHIPGRAPH_LOCAL_PLUGINS"
_NAMING_RULES_NAME = "chipgraph.draft.naming.yml"
_SKIP_COPY = frozenset({".git", ".chipgraph"})


@dataclass(slots=True)
class TryReport:
    """What `try` did: the draft it used, the ingest stats, check results, audit status."""

    result: LearnResult
    ingest_ok: bool
    ingest_summary: str
    checks: list[tuple[str, str | None, CheckResult]] = field(default_factory=list)
    audit_available: bool = False
    audit_summary: str = ""

    @property
    def check_files_with_issues(self) -> int:
        """Number of distinct files any check reported an issue for."""
        files: set[str] = set()
        for _cid, _blk, result in self.checks:
            for issue in result.issues:
                if issue.file:
                    files.add(issue.file)
        return len(files)


def try_run(
    repo_root: Path,
    *,
    profile_path: Path | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> TryReport:
    """Learn (or read a draft), then run ingest/check/audit read-only against `repo_root`.

    `profile_path`, when given, is a draft profile written by `learn --out`; otherwise the
    repo is learned fresh. Nothing is ever written into `repo_root`: a private working copy
    is made under a temp dir, and all state and the model DB land there.
    """
    repo_root = repo_root.resolve()
    result = learn(repo_root, threshold=threshold)

    with _local_plugins_disabled(), tempfile.TemporaryDirectory(prefix="chipgraph-try-") as tmp:
        work = Path(tmp) / "repo"
        _mirror_repo(repo_root, work)
        _place_profile(work, result, profile_path)
        ctx = AppContext.load(work)
        report = _run_readonly(ctx, result)
    return report


# --------------------------------------------------------------------------------------
# read-only run against the working copy
# --------------------------------------------------------------------------------------


def _run_readonly(ctx: AppContext, result: LearnResult) -> TryReport:
    ingest_ok, ingest_summary = _try_ingest(ctx)
    checks = _run_checks(ctx)
    audit_available, audit_summary = _try_audit(ctx)
    return TryReport(
        result=result,
        ingest_ok=ingest_ok,
        ingest_summary=ingest_summary,
        checks=checks,
        audit_available=audit_available,
        audit_summary=audit_summary,
    )


def _try_ingest(ctx: AppContext) -> tuple[bool, str]:
    from chipgraph.app.ingest import run_ingest

    try:
        report = run_ingest(ctx)
    except Exception as exc:  # a draft may not ingest cleanly; try must not crash
        return False, f"ingest failed: {exc}"
    stats = report.result.stats
    return (
        not report.has_error,
        f"inputs={stats.inputs} entities={stats.entities} relations={stats.relations}",
    )


def _run_checks(ctx: AppContext) -> list[tuple[str, str | None, CheckResult]]:
    resolved = ctx.require_profile()
    runner = ProfileCheckRunner(ctx)
    check_ids = sorted(resolved.profile.adapters)
    raw_blocks = sorted(resolved.profile.blocks)
    out: list[tuple[str, str | None, CheckResult]] = []

    async def _run() -> None:
        for cid in check_ids:
            blocks: list[str | None] = (
                list(raw_blocks) if runner.runs_per_block(cid) and raw_blocks else [None]
            )
            for block in blocks:
                instance = _instance(cid, block)
                result = await runner.run(cid, instance)
                out.append((cid, block, result))

    asyncio.run(_run())
    return out


def _instance(check_id: str, block: str | None) -> RuleInstance:
    params = {"block": block} if block is not None else {}
    rule_id = "chipgraph/try"
    return RuleInstance(
        rule_id=rule_id,
        params=params,
        outputs=(ArtifactRef(kind="report", path=f".chipgraph/tmp/try-{check_id}.json"),),
        instance_id=RuleInstance.make_id(rule_id, params),
    )


def _try_audit(ctx: AppContext) -> tuple[bool, str]:
    """Run the `assist` pack's audit over what `try` already ingested; its one-line summary.

    Ingest has already run into the temporary state, so the audit does not repeat it. An
    audit that fails is reported, not raised: `try` is a first look at a repo.
    """
    from chipgraph.packs.assist import run_audit

    try:
        report = run_audit(ctx, ingest=False)
    except Exception as exc:
        return True, f"audit ran with errors: {exc}"
    return True, f"audit: {report.summary_line()}"


# --------------------------------------------------------------------------------------
# working copy and profile placement
# --------------------------------------------------------------------------------------


def _mirror_repo(src: Path, dest: Path) -> None:
    """Copy `src` into `dest`, skipping `.git` and any existing `.chipgraph/` state.

    `try` reads the repo and writes only into this copy, so the real repo is untouched.
    """
    shutil.copytree(
        src,
        dest,
        symlinks=True,
        ignore=shutil.ignore_patterns(*_SKIP_COPY),
        ignore_dangling_symlinks=True,
    )


def _place_profile(work: Path, result: LearnResult, profile_path: Path | None) -> None:
    """Write the profile (draft or given) and its naming rules into the working copy."""
    if profile_path is not None:
        data = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        rules_src = profile_path.parent / _NAMING_RULES_NAME
        if rules_src.is_file():
            (work / _NAMING_RULES_NAME).write_text(
                rules_src.read_text(encoding="utf-8"), encoding="utf-8"
            )
    else:
        data = _draft_for_work(work, result)
    (work / ".chipgraph.yml").write_text(
        yaml.safe_dump(data, sort_keys=True, default_flow_style=False), encoding="utf-8"
    )


def _draft_for_work(work: Path, result: LearnResult) -> dict[str, Any]:
    rules = build_naming_rules(result)
    naming_ref: str | None = None
    if rules is not None:
        (work / _NAMING_RULES_NAME).write_text(dump_naming_rules_yaml(rules), encoding="utf-8")
        naming_ref = _NAMING_RULES_NAME
    return build_profile_dict(result, naming_rules_ref=naming_ref)


@contextmanager
def _local_plugins_disabled() -> Iterator[None]:
    """Force `CHIPGRAPH_LOCAL_PLUGINS=0` for the duration, restoring the prior value."""
    prior = os.environ.get(_LOCAL_PLUGINS_ENV)
    os.environ[_LOCAL_PLUGINS_ENV] = "0"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(_LOCAL_PLUGINS_ENV, None)
        else:
            os.environ[_LOCAL_PLUGINS_ENV] = prior


__all__ = ["TryReport", "try_run"]
