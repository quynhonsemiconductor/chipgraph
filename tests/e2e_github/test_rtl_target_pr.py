"""Manual end-to-end test of target `rtl:<block>`'s pull request on a real sandbox repository.

Never runs in CI or `make check`: it is skipped unless both `CHIPGRAPH_GITHUB_TOKEN` and
`CHIPGRAPH_SANDBOX_REPO` are set. There is no `gh` fallback, and any repository whose name
does not end in `-sandbox` is refused.

How to run it (a reviewer, once per release of the target)::

    # 1. A private repository you can throw away, e.g. quynhonsemiconductor/chipgraph-sandbox,
    #    with a default branch (any commit). A fine-grained token for that repository only,
    #    with Contents: read/write and Pull requests: read/write.
    export CHIPGRAPH_GITHUB_TOKEN=github_pat_...      # never in a file, a command line or a log
    export CHIPGRAPH_SANDBOX_REPO=quynhonsemiconductor/chipgraph-sandbox
    uv sync --locked --extra sim
    uv run pytest tests/e2e_github/test_rtl_target_pr.py -q

    # GitHub Enterprise: also set CHIPGRAPH_SANDBOX_API_URL (https://ghe.example/api/v3)
    # and CHIPGRAPH_SANDBOX_GIT_URL (https://ghe.example/<owner>/<name>.git).

What it does, in order:

1. A throwaway local git repository (a tinysoc copy, with the `digital-rtl` and `dv` packs) whose
   remote `origin` is the sandbox repository over HTTPS. It pushes a base branch
   `e2e-base-<hex>` there (through `GitVcs.push_branch` and the `GIT_ASKPASS` helper: the
   token is in no command line, URL or git config).
2. The profile says `review: {use: github, repo: <sandbox>, base: e2e-base-<hex>}`. The target
   runs with the fake runtime (a planner, an RTL author, a testbench author and a Critic that
   write fixed files): the plan, its gate (approved by the test), three modules, the
   testbench, the review, then the `open_pr` step, which pushes `chipgraph/<run id>/timer` and
   opens the PR.
3. It asserts the PR exists with that head, that base and a body with the plan, the REQ
   coverage, the checks and the Critic's verdict; that it has no reviews; that the gate
   `pr:timer` is waiting; and that running `open_pr` again returns the same PR (same number).
4. In a `finally`: closes the PR and deletes the PR branch and the base branch through the
   API (the test does this, never chipgraph, which has no way to close, delete or merge).

Cost: a handful of API calls and two small branches, gone at the end.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

_HERE = Path(__file__).resolve().parents[1] / "packs" / "digital_rtl"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from rtl_target_helpers import (  # noqa: E402
    PR_GATE,
    PR_RECORD,
    ChainRuntime,
    approve,
    drive,
    git,
    planned_timer,
    target_project,
)

from chipgraph.adapters.review.github import TOKEN_ENV, GitHubGateChecker  # noqa: E402
from chipgraph.adapters.vcs.git import GitVcs  # noqa: E402
from chipgraph.app.build import project_graph  # noqa: E402
from chipgraph.app.context import AppContext  # noqa: E402
from chipgraph.packs.digital_rtl.pr.open import open_block_pr, read_record  # noqa: E402

_REPO_ENV = "CHIPGRAPH_SANDBOX_REPO"
_API = os.environ.get("CHIPGRAPH_SANDBOX_API_URL", "https://api.github.com").rstrip("/")

requires_sandbox = pytest.mark.skipif(
    not (os.environ.get(TOKEN_ENV) and os.environ.get(_REPO_ENV)),
    reason=f"manual e2e: set {TOKEN_ENV} and {_REPO_ENV} to run it",
)


def sandbox_repo(value: str) -> str:
    """Return `value` if it names an `owner/<name>-sandbox` repo, else raise."""
    owner, _, name = value.partition("/")
    if not owner or not name.endswith("-sandbox") or "/" in name:
        raise ValueError(f"refusing {value!r}: the e2e only runs on a repo named '*-sandbox'")
    return value


def test_sandbox_guard_refuses_other_repos() -> None:
    assert sandbox_repo("acme/chipgraph-sandbox") == "acme/chipgraph-sandbox"
    for bad in ("acme/chipgraph", "acme/sandbox-chip", "chipgraph-sandbox", "a/b/c-sandbox"):
        with pytest.raises(ValueError, match="refusing"):
            sandbox_repo(bad)


def test_the_module_documents_how_to_run_it() -> None:
    doc = __doc__ or ""
    for needle in (TOKEN_ENV, _REPO_ENV, "-sandbox", "uv run pytest tests/e2e_github/", "finally"):
        assert needle in doc


def _call(method: str, path: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "chipgraph-e2e",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def _delete_branch(repo: str, branch: str, token: str) -> None:
    try:
        _call("DELETE", f"/repos/{repo}/git/refs/heads/{branch}", token)
    except urllib.error.HTTPError as exc:  # pragma: no cover - cleanup best effort
        if exc.code not in (404, 422):
            raise


@requires_sandbox
def test_target_rtl_opens_a_pr_on_the_sandbox_and_waits_on_its_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = sandbox_repo(os.environ[_REPO_ENV])
    token = os.environ[TOKEN_ENV]
    git_url = os.environ.get("CHIPGRAPH_SANDBOX_GIT_URL", f"https://github.com/{repo}.git")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    base = f"e2e-base-{secrets.token_hex(4)}"
    number: int | None = None
    pr_branch: str | None = None

    root = target_project(tmp_path / "tinysoc", real_lint=False)
    git(root, "remote", "add", "origin", git_url)
    vcs = GitVcs()
    try:
        # 1. The base branch, pushed with the token through the askpass helper.
        vcs.push_branch(root, "origin", git(root, "rev-parse", "HEAD"), base, token=token)

        profile = root / ".chipgraph.yml"
        data = yaml.safe_load(profile.read_text(encoding="utf-8"))
        data["review"] = {
            "use": "github",
            "repo": repo,
            "base": base,
            "api_url": _API,
            "allow_gh_fallback": False,
            "remote": "origin",
        }
        profile.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

        # 2. The target, with the fake runtime, up to the open_pr step and the PR gate.
        runtime = ChainRuntime(root, [planned_timer()])
        first = drive(root, runtime, "rtl:timer")
        assert first.waiting_gate, first  # the plan gate
        approve(root)
        second = drive(root, runtime, "rtl:timer")
        assert second.waiting_gate == (PR_GATE,), second

        record = read_record(root / PR_RECORD)
        assert record is not None and record.number is not None and record.url is not None
        number, pr_branch = record.number, record.branch
        assert record.review == "github" and record.base == base and record.remote == "origin"

        # 3. The PR exists with the right head, base and body parts; nobody reviewed it.
        app = AppContext.load(root)
        assert app.pr_review is not None
        state = app.pr_review.pull_request(number)
        assert app.pr_review.token_source == "env"
        assert (state.state, state.head, state.base) == ("open", pr_branch, base)
        assert state.head_sha == record.head_sha
        assert state.url.startswith("https://") and state.url == record.url
        body = _call("GET", f"/repos/{repo}/pulls/{number}", token)["body"]
        for part in ("### Plan", "### Requirement coverage", "### Checks", "### Critic review"):
            assert part in body
        assert "Verdict: **changes_requested**" in body and "REQ-TIM-003" in body
        assert token not in body
        assert app.pr_review.reviews(number) == ()
        instance = project_graph(app).instances[PR_GATE]
        checker = GitHubGateChecker(app.pr_review)
        assert checker.status("pr:timer", instance) == "waiting"

        # Idempotent: the same step again finds the same PR.
        again = open_block_pr(app, "timer", review=app.pr_review, token=token)
        assert again.number == number and again.branch == pr_branch
        assert again.head_sha == record.head_sha
    finally:
        if number is not None:
            _call("PATCH", f"/repos/{repo}/pulls/{number}", token, {"state": "closed"})
        if pr_branch is not None:
            _delete_branch(repo, pr_branch, token)
        _delete_branch(repo, base, token)
