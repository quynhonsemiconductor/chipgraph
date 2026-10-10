"""Manual end-to-end test of the `github` review adapter on a real sandbox repository.

Never runs in CI or `make check`: it is skipped unless both `CHIPGRAPH_GITHUB_TOKEN` and
`CHIPGRAPH_SANDBOX_REPO` are set. A reviewer runs it with a fine-grained token for the
private sandbox repo only (Contents and Pull requests read/write)::

    CHIPGRAPH_SANDBOX_REPO=quynhonsemiconductor/chipgraph-sandbox uv run pytest tests/e2e_github -q

It refuses any repo whose name does not end in `-sandbox` and never uses the `gh`
fallback. The test (not the adapter) creates a branch and a commit through the contents
API, and in a `finally` closes the PR and deletes the branch it made.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import urllib.error
import urllib.request
from typing import Any

import pytest

from chipgraph.adapters.review.github import TOKEN_ENV, GitHubReview, head_digest

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


@requires_sandbox
def test_open_read_and_clean_up_a_sandbox_pr() -> None:
    repo = sandbox_repo(os.environ[_REPO_ENV])
    token = os.environ[TOKEN_ENV]
    base = _call("GET", f"/repos/{repo}", token)["default_branch"]
    base_sha = _call("GET", f"/repos/{repo}/git/ref/heads/{base}", token)["object"]["sha"]
    branch = f"chipgraph-e2e-{secrets.token_hex(4)}"
    review = GitHubReview(repo, base=base, api_url=_API, allow_gh_fallback=False, gh_token=_no_gh)
    number: int | None = None

    _call(
        "POST", f"/repos/{repo}/git/refs", token, {"ref": f"refs/heads/{branch}", "sha": base_sha}
    )
    try:
        content = base64.b64encode(f"e2e {branch}\n".encode()).decode()
        _call(
            "PUT",
            f"/repos/{repo}/contents/e2e/{branch}.txt",
            token,
            {"message": f"test: e2e {branch}", "content": content, "branch": branch},
        )

        pr = review.open_pr(branch, None, f"test: chipgraph e2e {branch}", "e2e", gate_id="pr:e2e")
        number = pr.number
        again = review.open_pr(branch, None, "a different title", "")

        assert again.number == pr.number
        assert review.token_source == "env"
        state = review.pull_request(pr.number)
        assert (state.state, state.head, state.base) == ("open", branch, base)
        assert state.head_sha == pr.head_sha
        assert review.reviews(pr.number) == ()
        assert review.approvals("pr:e2e") == ()
        assert review.current_hashes("pr:e2e") == {
            review.head_key(pr.number): head_digest(state.head_sha)
        }
        assert not [name for name in dir(review) if "merge" in name.lower()]
    finally:
        if number is not None:
            _call("PATCH", f"/repos/{repo}/pulls/{number}", token, {"state": "closed"})
        try:
            _call("DELETE", f"/repos/{repo}/git/refs/heads/{branch}", token)
        except urllib.error.HTTPError as exc:  # pragma: no cover - cleanup best effort
            if exc.code != 422:
                raise


def _no_gh(_host: str) -> str | None:
    raise AssertionError("the e2e must never use the gh fallback")
