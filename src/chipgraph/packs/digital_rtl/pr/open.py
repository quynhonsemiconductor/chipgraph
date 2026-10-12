"""`OpenPrTool`: the `open_pr` tool of rule `digital-rtl/open_pr` (M2-11a).

For one block with an approved plan, once its modules, its testbench and its review are
built (the rule's inputs), it

1. builds the branch `chipgraph/<run id>/<block>` **without touching the user's working
   tree, index or HEAD**: a workspace (a git worktree) at HEAD, or at the branch's tip
   when it exists already; exactly the block's RTL files (every module's write set) and
   testbench copied into it from the user's tree; one commit with hooks off; the branch
   created, or moved forward (a fast-forward, compare and swap), never rewritten. The
   workspace is removed on every exit;
2. with `review: {use: github}`: pushes the branch to the profile's `review.remote`
   (default `origin`), a fast-forward push, never a force. An `https://` remote needs the
   token in `CHIPGRAPH_GITHUB_TOKEN`, which git gets through a `GIT_ASKPASS` helper only
   (`GitVcs.push_branch`); a missing token is a clear error; a `file://` or local-path
   remote needs none. Then opens the pull request with `GitHubReview.open_pr`
   (idempotent: an open PR from the branch is reused, never re-opened) into
   `review.base`, with the body of `body.render_body`, and binds gate `pr:<block>` to
   it. With the `file` review adapter nothing is pushed or opened: the gate is approved
   with `chipgraph approve pr:<block>`, offline;
3. writes `pr/<block>.json` (`PrRecord`): the branch, its head commit, the PR number and
   URL (none with the `file` adapter). A push after review changes the head, so the
   record changes, `digital-rtl/pr_gate` goes stale and the old approval with it.

Rule args (`{block}` templates): `review` (default `reports/review/{block}.json`) and
`out` (default `pr/{block}.json`). Nothing here merges.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.adapters.review.github import TOKEN_ENV, GitHubReview, ReviewError
from chipgraph.adapters.vcs.git import GitVcs, VcsError, redact_secret, url_host
from chipgraph.app.build import load_rules
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.checks._common import compute_idempotency_key
from chipgraph.checks.plan_check import PlanEnvError
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue, RuleSpec
from chipgraph.core.engine.gate import gate_id_for
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.core.state.layout import StateLayout, new_run_id
from chipgraph.packs.digital_rtl.plan.resolver import (
    ApprovedPlan,
    approved_plan,
    block_files,
    plan_gates,
)
from chipgraph.packs.digital_rtl.plan.show import plan_summary
from chipgraph.packs.digital_rtl.pr.body import last_checks, read_review, render_body

DEFAULT_REVIEW = "reports/review/{block}.json"
DEFAULT_OUT = "pr/{block}.json"
PR_GATE_RULE = "digital-rtl/pr_gate"
DEFAULT_GATE = "pr:{block}"
BRANCH_PREFIX = "chipgraph"


class OpenPrError(Exception):
    """The block's pull request cannot be built (a message for a person)."""


class PrRecord(BaseModel):
    """`pr/<block>.json`: the branch and pull request of a block's planned RTL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this file.")
    block: str = Field(description="The block.")
    branch: str = Field(description="The branch, `chipgraph/<run id>/<block>`.")
    head_sha: str = Field(description="The branch's head commit.")
    files: tuple[str, ...] = Field(description="The files the branch adds or changes.")
    review: Literal["github", "file"] = Field(description="Where the gate is decided.")
    gate: str = Field(description="The gate the PR is bound to, `pr:<block>`.")
    remote: str | None = Field(default=None, description="The remote it was pushed to.")
    base: str | None = Field(default=None, description="The PR's base branch.")
    number: int | None = Field(default=None, description="The PR number (GitHub).")
    url: str | None = Field(default=None, description="The PR's web URL (GitHub).")


def _fill(template: str, block: str) -> str:
    return template.replace("{block}", block)


def needs_token(url: str) -> bool:
    """Whether pushing to `url` needs the HTTPS token (not for file://, a path, or SSH)."""
    return urllib.parse.urlsplit(url).scheme in ("http", "https")


def check_token_host(url: str, api_url: str) -> None:
    """Refuse to send the token to a git host that is not the API's host.

    The remote comes from the repo's git config; a token meant for `api_url`'s GitHub must
    not be handed to whatever host `origin` points at.
    """
    git_host = url_host(url)
    api_host = url_host(api_url)
    allowed = {api_host, api_host.removeprefix("api.") if api_host else None} - {None}
    if git_host is None or git_host not in allowed:
        raise OpenPrError(
            f"refusing to send the GitHub token to {git_host or url!r}: the API is at "
            f"{api_host!r}, so the remote must be on {sorted(str(h) for h in allowed)}"
        )


def _latest_run(layout: StateLayout) -> str:
    runs = layout.runs_dir
    if runs.is_dir():
        ids = sorted(p.name for p in runs.iterdir() if p.is_dir())
        if ids:
            return ids[-1]
    return new_run_id()


def _gate_id(rules: Sequence[RuleSpec], block: str) -> str:
    rule = next((r for r in rules if r.id == PR_GATE_RULE), None)
    template = rule.gate if rule is not None and rule.gate else DEFAULT_GATE
    return gate_id_for(template, {"block": block})


def read_record(path: Path) -> PrRecord | None:
    """The `PrRecord` at `path`, or None if it is missing or invalid."""
    try:
        return PrRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_branch(branch: str, block: str) -> None:
    """Refuse a branch name that is not `chipgraph/<run id>/<block>`.

    The name can come from `pr/<block>.json`, a file in the working tree that anyone may
    edit; moving or pushing `main` because that file says so would publish without a PR.
    """
    pattern = rf"{re.escape(BRANCH_PREFIX)}/[A-Za-z0-9][A-Za-z0-9._-]*/{re.escape(block)}"
    if re.fullmatch(pattern, branch) is None:
        raise OpenPrError(
            f"refusing branch {branch!r} for block {block!r}: a block's pull request branch "
            f"is {BRANCH_PREFIX}/<run id>/{block}"
        )


def _plain_path(base: Path, rel: str, *, what: str) -> Path:
    """`base / rel` with no symlink on the way and inside `base` (else `OpenPrError`).

    Every component is checked, not only the last: a symlinked directory in the user's tree
    would read from outside it, and one in the committed tree of the branch would write
    outside the workspace.
    """
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise OpenPrError(f"{what} path {rel!r} is not a plain relative path")
    path = base
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise OpenPrError(f"{what} path {rel!r}: {part!r} is a symlink")
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        raise OpenPrError(f"{what} path {rel!r} resolves outside {base}") from None
    return path


def _copy_file(src: Path, dest: Path) -> None:
    """Copy the regular file `src` to `dest` without following a link at either end."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(src, flags)
    except OSError as exc:
        raise OpenPrError(f"cannot read {src}: {exc.strerror or exc}") from None
    with os.fdopen(fd, "rb") as source:
        mode = os.fstat(source.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise OpenPrError(f"{src} is not a regular file")
        tmp = dest.with_name(f".{dest.name}.chipgraph-tmp")
        try:
            with open(tmp, "xb") as target:
                shutil.copyfileobj(source, target)
            os.chmod(tmp, stat.S_IMODE(mode))
            os.replace(tmp, dest)  # replaces a link itself, never what it points at
        finally:
            if tmp.exists() or tmp.is_symlink():
                tmp.unlink()


def build_branch(
    vcs: GitVcs, root: Path, branch: str, files: Sequence[str], message: str
) -> tuple[str, str | None]:
    """Commit exactly `files` (copied from `root`) onto `branch`; `(new head, old head)`.

    The commit is made in a workspace at the branch's tip (or HEAD for a new branch),
    outside the user's tree; the branch is then created or fast-forwarded. HEAD, the
    index and the working tree of `root` are never touched.
    """
    if branch in vcs.checked_out_branches(root):
        raise OpenPrError(
            f"refusing branch {branch!r}: it is checked out in a worktree; switch that "
            "worktree to another branch first (the PR branch is moved without a checkout)"
        )
    sources = {rel: _plain_path(root, rel, what="output") for rel in files}
    old = vcs.branch_tip(root, branch)
    base = old or vcs.head(root)
    holder = Path(tempfile.mkdtemp(prefix="chipgraph-pr-"))
    workspace: Path | None = None
    try:
        workspace = vcs.create_branch_workspace(root, base, "pr", holder)
        for rel in files:
            _plain_path(workspace, rel, what="branch")
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest = _plain_path(workspace, rel, what="branch")  # again: mkdir made the parents
            if dest.is_dir():
                raise OpenPrError(f"{rel} is a directory at {base[:12]}")
            _copy_file(sources[rel], dest)
        commit = vcs.commit_all(workspace, message, paths=list(files))
    finally:
        if workspace is not None:
            vcs.remove(workspace)
        vcs.prune(root)
        shutil.rmtree(holder, ignore_errors=True)
    if old is None:
        vcs.create_branch(root, branch, commit)
    else:
        vcs.advance_branch(root, branch, commit, old)
    return commit, old


def open_block_pr(
    app: AppContext,
    block: str,
    *,
    review_path: str = DEFAULT_REVIEW,
    out_path: str = DEFAULT_OUT,
    review: GitHubReview | None = None,
    vcs: GitVcs | None = None,
    token: str | None = None,
) -> PrRecord:
    """Build, push and open the PR of `block`; write and return its `PrRecord`.

    `review` is the GitHub adapter (None: the `file` adapter, nothing pushed or opened);
    `token` the HTTPS token, used only for an `http(s)://` remote. Raises `OpenPrError`.
    """
    vcs = vcs or GitVcs()
    root = app.root
    rules = load_rules(app)
    resolved = app.require_profile()
    if block not in resolved.profile.blocks:
        raise OpenPrError(f"unknown block {block!r}")
    gate = plan_gates(rules, resolved.profile.blocks, repo=app.store.repo)[block]
    try:
        approved: ApprovedPlan | None = approved_plan(app, rules, gate)
    except PlanEnvError as exc:
        raise OpenPrError(str(exc)) from exc
    if approved is None:
        raise OpenPrError(
            f"block {block!r} has no approved, valid plan: approve gate {gate.gate_id!r} first"
        )
    rtl, tb = block_files(rules, approved)
    files = [*rtl, *tb]
    missing = [rel for rel in files if not (root / rel).is_file()]
    if missing:
        raise OpenPrError(f"block {block!r}: not built yet: {', '.join(missing)}")

    out = root / _fill(out_path, block)
    previous = read_record(out)
    branch = (
        previous.branch
        if previous is not None and previous.block == block
        else f"{BRANCH_PREFIX}/{_latest_run(app.layout)}/{block}"
    )
    check_branch(branch, block)
    message = f"chipgraph: RTL and testbench of block {block} from its approved plan"
    head, _old = build_branch(vcs, root, branch, files, message)

    gate_id = _gate_id(rules, block)
    record = PrRecord(
        block=block, branch=branch, head_sha=head, files=tuple(files), review="file", gate=gate_id
    )
    if review is not None:
        remote = review.remote
        url = vcs.remote_url(root, remote)
        secret: str | None = None
        if needs_token(url):
            check_token_host(url, review.api_url)
            secret = token
            if not secret:
                raise OpenPrError(
                    f"no GitHub token: set {TOKEN_ENV} to push {branch!r} to remote "
                    f"{remote!r} (a fine-grained token with Contents and Pull requests "
                    "read/write on the repo)"
                )
        vcs.push_branch(root, remote, head, branch, token=secret)
        rel_review = _fill(review_path, block)
        body = render_body(
            block=block,
            summary=plan_summary(app, block),
            checks=last_checks(app, block),
            review=read_review(root / rel_review),
            review_path=rel_review,
            files=files,
        )
        pr = review.open_pr(branch, None, f"chipgraph: RTL of block {block}", body, gate_id=gate_id)
        record = record.model_copy(
            update={
                "review": "github",
                "remote": remote,
                "base": pr.base,
                "number": pr.number,
                "url": pr.url,
            }
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record


def _scrub(text: str, secret: str | None) -> str:
    return redact_secret(text, secret)


class OpenPrTool:
    """Builds, pushes and opens a block's pull request (see the module docstring)."""

    name = "open_pr"
    capability = "gen"

    review_for: ClassVar[Callable[[AppContext], GitHubReview | None]] = staticmethod(
        lambda app: app.pr_review
    )
    """How the tool gets the GitHub adapter of a project (tests swap in a fake API)."""

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        block = ctx.params.get("block")
        review_t = spec.args.get("review", DEFAULT_REVIEW)
        out_t = spec.args.get("out", DEFAULT_OUT)
        token = os.environ.get(TOKEN_ENV, "").strip() or None
        if not block or not isinstance(review_t, str) or not isinstance(out_t, str):
            return self._result(spec, "error", "open_pr needs a block and string templates", start)
        try:
            app = AppContext.load(ctx.repo_root)
            record = open_block_pr(
                app,
                block,
                review_path=review_t,
                out_path=out_t,
                review=type(self).review_for(app),
                token=token,
            )
        except (OpenPrError, AppError, VcsError, ReviewError, OSError) as exc:
            return self._result(spec, "error", _scrub(str(exc), token), start)
        where = record.url or f"branch {record.branch} (gate {record.gate}: chipgraph approve)"
        return self._result(spec, "pass", f"{record.head_sha[:12]} -> {where}", start)

    @staticmethod
    def _result(spec: CheckSpec, status: str, message: str, start: float) -> CheckResult:
        severity = "info" if status == "pass" else "error"
        return CheckResult(
            check_id=spec.id,
            status=status,  # type: ignore[arg-type]
            issues=(Issue(rule="open_pr", severity=severity, msg=message),),  # type: ignore[arg-type]
            duration_s=time.monotonic() - start,
            idempotency_key=compute_idempotency_key(spec.id, spec.args, ()),
        )


__all__ = [
    "BRANCH_PREFIX",
    "DEFAULT_OUT",
    "DEFAULT_REVIEW",
    "OpenPrError",
    "OpenPrTool",
    "PrRecord",
    "build_branch",
    "check_branch",
    "check_token_host",
    "needs_token",
    "open_block_pr",
    "read_record",
]
