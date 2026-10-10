"""The `github` review adapter over real HTTP (stdlib `urllib`) against a local fake."""

from __future__ import annotations

import pytest
from github_api_helpers import REPO, FakeGitHub

from chipgraph.adapters.review.github import GitHubReview, ReviewError

SHA = "c" * 40


def _adapter(fake: FakeGitHub, base_url: str, **kwargs: object) -> GitHubReview:
    return GitHubReview(
        REPO,
        api_url=base_url,
        env={"CHIPGRAPH_GITHUB_TOKEN": fake.token},
        sleep=lambda _s: None,
        **kwargs,  # type: ignore[arg-type]
    )


def test_open_pr_reviews_and_pagination_over_http() -> None:
    fake = FakeGitHub(page_size=2)
    fake.push("feat/timer", SHA)
    with fake.serve() as base_url:
        review = _adapter(fake, base_url)
        pr = review.open_pr("feat/timer", None, "t", "b", gate_id="pr:timer")
        again = review.open_pr("feat/timer", None, "t", "b")
        for login in ("alice", "bob", "carol", "dave", "erin"):
            fake.review(pr.number, login, "APPROVED")

        assert again == pr
        assert len(review.reviews(pr.number)) == 5
        assert len(review.approvals("pr:timer")) == 5


def test_http_error_statuses_come_back_as_review_errors() -> None:
    fake = FakeGitHub()
    with fake.serve() as base_url:
        review = _adapter(fake, base_url)
        with pytest.raises(ReviewError, match="404"):
            review.pull_request(99)


def test_redirects_are_not_followed() -> None:
    fake = FakeGitHub()
    fake.add_pr("feat/timer", SHA)
    with fake.serve() as base_url:
        fake.fail("GET", r"/pulls/1$", 301, headers={"Location": f"{base_url}/repos/x/y/pulls/1"})
        review = _adapter(fake, base_url)
        with pytest.raises(ReviewError, match="redirects are not followed"):
            review.pull_request(1)
    assert [s.path for s in fake.requests] == [f"/repos/{REPO}/pulls/1"]


def test_timeouts_are_bounded_and_reported() -> None:
    fake = FakeGitHub()
    fake.add_pr("feat/timer", SHA)
    fake.delay_s = 0.5
    with fake.serve() as base_url:
        review = _adapter(fake, base_url, timeout_s=0.1, max_retries=1)
        with pytest.raises(ReviewError, match="timed out"):
            review.pull_request(1)
