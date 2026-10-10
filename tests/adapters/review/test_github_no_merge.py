"""The `github` review adapter can never merge, approve or change a PR on GitHub."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from github_api_helpers import REPO, FakeGitHub

import chipgraph.adapters.review.github as github_module
from chipgraph.adapters.review.github import GitHubGateChecker, GitHubReview, ReviewError
from chipgraph.core.contracts import ArtifactRef, RuleInstance

_FORBIDDEN = (
    r"/merge",  # PUT /repos/{o}/{r}/pulls/{n}/merge, POST /repos/{o}/{r}/merges
    r"enablePullRequestAutoMerge",
    r"auto_merge",
    r"automerge",
    r"graphql",
    r"\"PUT\"",
    r"\"PATCH\"",
    r"\"DELETE\"",
    r"git/refs",  # branch deletion
    r"state=closed",
    r"\"state\":\s*\"closed\"",
    r"/requested_reviewers",
    r"/dismissals",
    r"/events",  # submitting a pending review
)


def _source() -> str:
    return Path(inspect.getfile(github_module)).read_text(encoding="utf-8")


@pytest.mark.parametrize("pattern", _FORBIDDEN)
def test_module_source_has_no_merge_or_write_api(pattern: str) -> None:
    assert not re.search(pattern, _source(), flags=re.IGNORECASE), pattern


def test_module_never_submits_reviews() -> None:
    source = _source()
    # Reviews are only ever read: the only POST is the PR-create endpoint.
    assert re.findall(r"\"POST\"", source)
    for line in source.splitlines():
        if '"POST"' in line:
            assert "reviews" not in line


def test_adapter_has_no_merge_method() -> None:
    names = [name for name in dir(GitHubReview) if "merge" in name.lower()]
    assert names == []
    assert [n for n in dir(GitHubGateChecker) if "merge" in n.lower()] == []


def test_every_public_method_uses_only_read_or_pr_create_requests(tmp_path: Path) -> None:
    fake = FakeGitHub(page_size=1)
    fake.push("feat/timer", "a" * 40)
    review = fake.adapter(state_dir=tmp_path)
    instance = RuleInstance(
        rule_id="demo/review",
        params={},
        outputs=(ArtifactRef(kind="report", path="out/r.json"),),
        instance_id=RuleInstance.make_id("demo/review", {}),
    )

    pr = review.open_pr("feat/timer", None, "t", "", gate_id="pr:timer")
    review.open_pr("feat/timer", "main", "t", "")
    fake.review(pr.number, "alice", "APPROVED")
    fake.review(pr.number, "bob", "CHANGES_REQUESTED")
    review.pull_request(pr.number)
    review.reviews(pr.number)
    review.approvals("pr:timer")
    review.current_hashes("pr:timer")
    review.head_key(pr.number)
    review.bind("pr:other", pr.number)
    review.pr_for("pr:timer")
    for approval in review.approvals("pr:timer"):
        with pytest.raises(ReviewError):
            review.record(approval)
    GitHubGateChecker(review).status("pr:timer", instance)
    repr(review)

    public = {n for n in dir(GitHubReview) if not n.startswith("_")}
    exercised = {
        "open_pr",
        "pull_request",
        "reviews",
        "approvals",
        "current_hashes",
        "head_key",
        "bind",
        "pr_for",
        "record",
        "from_config",
        "name",
        "token_source",
    }
    assert public <= exercised, public - exercised

    pulls = f"/repos/{REPO}/pulls"
    allowed_get = re.compile(rf"^{re.escape(pulls)}(/\d+(/reviews)?)?$")
    for seen in fake.requests:
        assert (seen.method == "GET" and allowed_get.match(seen.path)) or (
            seen.method == "POST" and seen.path == pulls
        ), seen
    assert {s.method for s in fake.requests} == {"GET", "POST"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/pulls/1/merge"),
        ("POST", "/merges"),
        ("PATCH", "/pulls/1"),
        ("DELETE", "/git/refs/heads/feat"),
        ("POST", "/pulls/1/reviews"),
        ("GET", "/pulls/1/merge"),
    ],
)
def test_http_layer_refuses_anything_else_before_sending(method: str, path: str) -> None:
    fake = FakeGitHub()
    review = fake.adapter()
    with pytest.raises(ReviewError, match="refusing"):
        review._send(method, path)
    assert fake.requests == []
