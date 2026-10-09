"""Tests for the `github` review adapter against the in-process fake API."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from github_api_helpers import BOT, REPO, TOKEN, FakeGitHub

from chipgraph.adapters.review.github import (
    TOKEN_ENV,
    GitHubGateChecker,
    GitHubReview,
    HttpRequest,
    ReviewError,
    head_digest,
)
from chipgraph.core.contracts import Approval, ArtifactRef, RuleInstance

SHA_A = "a" * 40
SHA_B = "b" * 40


def _instance() -> RuleInstance:
    return RuleInstance(
        rule_id="demo/review",
        params={"block": "timer"},
        outputs=(ArtifactRef(kind="report", path="out/r.json"),),
        instance_id=RuleInstance.make_id("demo/review", {"block": "timer"}),
    )


def _bound(fake: FakeGitHub, sha: str = SHA_A) -> tuple[GitHubReview, int]:
    number = fake.add_pr("feat/timer", sha)
    review = fake.adapter(prs={"pr:timer": number})
    return review, number


def _approval(decision: str) -> Approval:
    return Approval(
        gate_id="pr:timer",
        by="lead",
        at=datetime.now(UTC),
        artifact_hashes={f"{REPO}:pull/1/head": head_digest(SHA_A)},
        decision=decision,
    )


# --- open_pr / pull_request -----------------------------------------------------------


def test_open_pr_creates_a_pr_from_a_pushed_branch() -> None:
    fake = FakeGitHub()
    fake.push("feat/timer", SHA_A)
    review = fake.adapter()

    pr = review.open_pr("feat/timer", None, "feat: timer", "body", draft=True)

    assert pr.number == 1
    assert pr.head == "feat/timer"
    assert pr.head_sha == SHA_A
    assert pr.base == "main"
    assert pr.state == "open"
    assert pr.draft is True
    assert pr.url.endswith(f"{REPO}/pull/1")
    assert fake.prs[1]["title"] == "feat: timer"


def test_open_pr_is_idempotent_for_the_same_head() -> None:
    fake = FakeGitHub()
    fake.push("feat/timer", SHA_A)
    review = fake.adapter()

    first = review.open_pr("feat/timer", "main", "one", "")
    second = review.open_pr("feat/timer", "main", "two", "")

    assert first == second
    assert len(fake.prs) == 1
    assert [s.method for s in fake.requests].count("POST") == 1


def test_open_pr_returns_the_pr_a_racing_create_opened() -> None:
    fake = FakeGitHub()
    fake.push("feat/timer", SHA_A)
    review = fake.adapter()
    # The lookup sees nothing, then the create loses a race: GitHub answers 422.
    fake.fail("GET", r"/pulls$", 200, body=b"[]")
    fake.add_pr("feat/timer", SHA_A)

    pr = review.open_pr("feat/timer", None, "t", "")

    assert pr.number == 1
    assert len(fake.prs) == 1


def test_open_pr_for_an_unpushed_branch_is_a_clear_422() -> None:
    fake = FakeGitHub()
    with pytest.raises(ReviewError, match=r"422.*head invalid"):
        fake.adapter().open_pr("not-pushed", None, "t", "")


def test_open_pr_with_gate_id_binds_and_persists(tmp_path: Path) -> None:
    fake = FakeGitHub()
    fake.push("feat/timer", SHA_A)
    review = fake.adapter(state_dir=tmp_path)

    pr = review.open_pr("feat/timer", None, "t", "", gate_id="pr:timer")

    assert review.pr_for("pr:timer") == pr.number
    stored = json.loads((tmp_path / "review" / "github.json").read_text())
    assert stored == {"schema_version": 1, "repo": REPO, "gates": {"pr:timer": pr.number}}
    assert fake.adapter(state_dir=tmp_path).pr_for("pr:timer") == pr.number
    # Bindings of another repo are not reused.
    other = GitHubReview("acme/other", state_dir=tmp_path, env={})
    assert other.pr_for("pr:timer") is None


def test_bind_and_constructor_mapping() -> None:
    fake = FakeGitHub()
    review = fake.adapter(prs={"pr:a": 3})
    review.bind("pr:b", 4)
    assert review.pr_for("pr:a") == 3
    assert review.pr_for("pr:b") == 4
    assert review.pr_for("pr:c") is None
    with pytest.raises(ReviewError):
        review.bind("pr:c", 0)


def test_pull_request_reports_state_and_head() -> None:
    fake = FakeGitHub()
    review, number = _bound(fake)
    fake.push("feat/timer", SHA_B)
    pr = review.pull_request(number)
    assert (pr.state, pr.head_sha, pr.author, pr.merged) == ("open", SHA_B, BOT, False)


# --- reviews and approvals --------------------------------------------------------------


def test_reviews_latest_per_reviewer_and_comment_does_not_undo_approval() -> None:
    fake = FakeGitHub()
    review, number = _bound(fake)
    fake.review(number, "alice", "CHANGES_REQUESTED")
    fake.review(number, "alice", "APPROVED")
    fake.review(number, "alice", "COMMENTED")
    fake.review(number, "bob", "COMMENTED")

    latest = {r.by: r.state for r in review.reviews(number)}

    assert latest == {"alice": "APPROVED", "bob": "COMMENTED"}


def test_approvals_map_states_and_pin_the_head() -> None:
    fake = FakeGitHub()
    review, number = _bound(fake)
    fake.review(number, "alice", "APPROVED")
    fake.review(number, "bob", "CHANGES_REQUESTED")
    fake.review(number, "carol", "COMMENTED")
    fake.review(number, "dave", "PENDING")
    fake.review(number, "erin", "APPROVED")
    fake.review(number, "erin", "DISMISSED")

    approvals = review.approvals("pr:timer")

    assert {(a.by, a.decision) for a in approvals} == {("alice", "approve"), ("bob", "reject")}
    key = f"{REPO}:pull/{number}/head"
    assert review.head_key(number) == key
    for approval in approvals:
        assert approval.gate_id == "pr:timer"
        assert approval.artifact_hashes == {key: head_digest(SHA_A)}
        assert approval.is_current(review.current_hashes("pr:timer"))
    assert review.current_hashes("pr:timer") == {key: head_digest(SHA_A)}


def test_two_reviewers_latest_wins() -> None:
    fake = FakeGitHub()
    review, number = _bound(fake)
    fake.review(number, "alice", "APPROVED")
    fake.review(number, "bob", "APPROVED")
    fake.review(number, "bob", "CHANGES_REQUESTED")
    fake.review(number, "alice", "CHANGES_REQUESTED")
    fake.review(number, "alice", "APPROVED")

    decisions = {a.by: a.decision for a in review.approvals("pr:timer")}

    assert decisions == {"alice": "approve", "bob": "reject"}


def test_author_review_does_not_count() -> None:
    fake = FakeGitHub()
    review, number = _bound(fake)
    fake.review(number, BOT, "APPROVED")
    assert review.approvals("pr:timer") == ()


def test_a_new_head_makes_older_approvals_stale() -> None:
    fake = FakeGitHub()
    review, number = _bound(fake)
    fake.review(number, "alice", "APPROVED")
    (old,) = review.approvals("pr:timer")

    fake.push("feat/timer", SHA_B)

    assert review.approvals("pr:timer") == ()
    assert not old.is_current(review.current_hashes("pr:timer"))
    fake.review(number, "alice", "APPROVED")
    (new,) = review.approvals("pr:timer")
    assert new.is_current(review.current_hashes("pr:timer"))
    assert new.artifact_hashes != old.artifact_hashes


def test_unbound_gate_has_no_approvals_and_no_hashes() -> None:
    fake = FakeGitHub()
    review = fake.adapter()
    assert review.approvals("pr:none") == ()
    assert review.current_hashes("pr:none") == {}
    assert fake.requests == []


def test_reviews_follow_link_pagination() -> None:
    fake = FakeGitHub(page_size=2)
    review, number = _bound(fake)
    for i in range(7):
        fake.review(number, f"user{i}", "APPROVED")

    assert len(review.reviews(number)) == 7
    pages = [s.query.get("page", "1") for s in fake.requests if s.path.endswith("/reviews")]
    assert pages == ["1", "2", "3", "4"]


def test_pagination_link_to_another_host_is_refused() -> None:
    fake = FakeGitHub()
    review, number = _bound(fake)
    fake.fail(
        "GET",
        r"/reviews$",
        200,
        headers={"link": '<https://evil.test/repos/acme/chip/pulls/1/reviews?page=2>; rel="next"'},
        body=b"[]",
    )
    with pytest.raises(ReviewError, match="pagination link"):
        review.reviews(number)


# --- record -----------------------------------------------------------------------------


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_record_approve_or_reject_says_decide_on_the_pr(decision: str) -> None:
    fake = FakeGitHub()
    with pytest.raises(ReviewError, match="decided on its pull request"):
        fake.adapter().record(_approval(decision))
    assert fake.requests == []


@pytest.mark.parametrize("decision", ["waive", "baseline"])
def test_record_waive_or_baseline_points_to_the_file_adapter(decision: str) -> None:
    fake = FakeGitHub()
    with pytest.raises(ReviewError, match="'file' adapter"):
        fake.adapter().record(_approval(decision))
    assert fake.requests == []


# --- robustness -----------------------------------------------------------------------


def test_retries_5xx_then_succeeds() -> None:
    fake = FakeGitHub()
    sleeps: list[float] = []
    number = fake.add_pr("feat/timer", SHA_A)
    review = fake.adapter(sleep=sleeps.append)
    fake.fail("GET", r"/pulls/\d+$", 502, times=2)

    assert review.pull_request(number).number == number
    assert sleeps == [1.0, 2.0]


def test_5xx_retries_are_bounded() -> None:
    fake = FakeGitHub()
    number = fake.add_pr("feat/timer", SHA_A)
    review = fake.adapter(max_retries=2)
    fake.fail("GET", r"/pulls/\d+$", 503, times=10)

    with pytest.raises(ReviewError, match="503"):
        review.pull_request(number)
    assert len([s for s in fake.requests if s.method == "GET"]) == 3


@pytest.mark.parametrize("status", [403, 429])
def test_secondary_rate_limit_waits_retry_after(status: int) -> None:
    fake = FakeGitHub()
    sleeps: list[float] = []
    number = fake.add_pr("feat/timer", SHA_A)
    review = fake.adapter(sleep=sleeps.append)
    fake.fail(
        "GET",
        r"/pulls/\d+$",
        status,
        headers={"Retry-After": "7"},
        body={"message": "You have exceeded a secondary rate limit."},
    )

    assert review.pull_request(number).number == number
    assert sleeps == [7.0]


def test_rate_limit_with_a_long_retry_after_fails_fast() -> None:
    fake = FakeGitHub()
    sleeps: list[float] = []
    number = fake.add_pr("feat/timer", SHA_A)
    review = fake.adapter(sleep=sleeps.append)
    fake.fail("GET", r"/pulls/\d+$", 403, headers={"Retry-After": "3600"})

    with pytest.raises(ReviewError, match="rate limit"):
        review.pull_request(number)
    assert sleeps == []


def test_network_errors_on_get_are_retried_then_reported() -> None:
    calls: list[HttpRequest] = []

    def broken(request: HttpRequest) -> object:
        calls.append(request)
        raise TimeoutError("timed out")

    review = GitHubReview(
        REPO, env={TOKEN_ENV: TOKEN}, transport=broken, sleep=lambda _s: None, max_retries=2
    )
    with pytest.raises(ReviewError, match="timed out"):
        review.pull_request(1)
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (401, f"check {TOKEN_ENV}"),
        (403, "'Pull requests' and 'Contents'"),
        (404, "check review.repo"),
        (422, "Validation Failed"),
        (301, "redirects are not followed"),
    ],
)
def test_error_statuses_say_what_to_check(status: int, needle: str) -> None:
    fake = FakeGitHub()
    number = fake.add_pr("feat/timer", SHA_A)
    review = fake.adapter()
    fake.fail("GET", r"/pulls/\d+$", status, body={"message": "Validation Failed"})
    with pytest.raises(ReviewError, match=needle) as info:
        review.pull_request(number)
    assert TOKEN not in str(info.value)


def test_bad_token_is_a_401_error() -> None:
    fake = FakeGitHub()
    review = fake.adapter(env={TOKEN_ENV: "wrong-token"})
    with pytest.raises(ReviewError, match="401") as info:
        review.pull_request(1)
    assert "wrong-token" not in str(info.value)


def test_api_url_must_be_https_except_localhost() -> None:
    with pytest.raises(ReviewError, match="https"):
        GitHubReview(REPO, api_url="http://ghe.example.test/api/v3")
    GitHubReview(REPO, api_url="http://127.0.0.1:9/api/v3")
    GitHubReview(REPO, api_url="https://ghe.example.test/api/v3")


def test_enterprise_api_url_prefix_is_used() -> None:
    fake = FakeGitHub(base_url="https://ghe.example.test/api/v3")
    number = fake.add_pr("feat/timer", SHA_A)
    assert fake.adapter().pull_request(number).number == number
    assert fake.requests[-1].path == f"/repos/{REPO}/pulls/{number}"


def test_unconfigured_adapter_says_repo_is_not_set() -> None:
    review = GitHubReview()
    with pytest.raises(ReviewError, match=r"review\.repo is not set"):
        review.pull_request(1)


# --- auth and redaction ---------------------------------------------------------------


def test_token_from_env_records_source() -> None:
    fake = FakeGitHub()
    gh_calls: list[str] = []
    number = fake.add_pr("feat/timer", SHA_A)
    review = fake.adapter(gh_token=lambda host: gh_calls.append(host) or "x")
    assert review.token_source is None
    review.pull_request(number)
    assert review.token_source == "env"
    assert gh_calls == []


def test_gh_fallback_is_used_locally_when_allowed() -> None:
    fake = FakeGitHub()
    hosts: list[str] = []
    number = fake.add_pr("feat/timer", SHA_A)

    def gh(host: str) -> str:
        hosts.append(host)
        return TOKEN

    review = fake.adapter(env={}, gh_token=gh)
    review.pull_request(number)
    assert review.token_source == "gh"
    assert hosts == ["api.github.test"]


def test_gh_fallback_is_forced_off_when_ci_is_set() -> None:
    fake = FakeGitHub()
    gh_calls: list[str] = []
    review = fake.adapter(
        env={"CI": "true"}, allow_gh_fallback=True, gh_token=lambda h: gh_calls.append(h) or TOKEN
    )
    assert review.allow_gh_fallback is False
    with pytest.raises(ReviewError, match=TOKEN_ENV):
        review.pull_request(1)
    assert gh_calls == []
    assert fake.requests == []


def test_missing_token_names_the_env_var() -> None:
    fake = FakeGitHub()
    review = fake.adapter(env={}, allow_gh_fallback=False)
    with pytest.raises(ReviewError, match=f"set {TOKEN_ENV}"):
        review.pull_request(1)
    review = fake.adapter(env={}, gh_token=lambda _h: None)
    with pytest.raises(ReviewError, match="gh auth token"):
        review.pull_request(1)


def test_token_never_appears_in_errors_logs_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeGitHub()
    seen: list[HttpRequest] = []

    def spy(request: HttpRequest) -> object:
        seen.append(request)
        return fake(request)

    review = GitHubReview(REPO, env={TOKEN_ENV: TOKEN}, transport=spy, sleep=lambda _s: None)
    fake.push("feat/timer", SHA_A)
    texts: list[str] = []
    review.open_pr("feat/timer", None, "t", "", gate_id="pr:timer")
    for status in (401, 403, 404, 422, 500, 429):
        fake.fail("GET", r"/pulls/\d+$", status, headers={"Retry-After": "999"}, times=10)
        with pytest.raises(ReviewError) as info:
            review.pull_request(1)
        texts.append(str(info.value))
        texts.append(repr(info.value))
        fake._failures.clear()
    with pytest.raises(ReviewError) as info:
        review.record(_approval("approve"))
    texts.append(str(info.value))

    texts += [repr(review), str(review), *(repr(r) for r in seen)]
    texts += [record.getMessage() for record in caplog.records]
    assert seen, "requests were sent"
    assert caplog.records, "debug logs were written"
    assert all(TOKEN not in text for text in texts)


# --- gate checker -----------------------------------------------------------------------


class _Fallback:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self, gate_id: str, instance: RuleInstance) -> str:
        self.calls.append(gate_id)
        return "approved"


def test_gate_checker_approved_rejected_waiting_and_stale() -> None:
    fake = FakeGitHub()
    review, number = _bound(fake)
    checker = GitHubGateChecker(review)
    inst = _instance()

    assert checker.status("pr:timer", inst) == "waiting"
    fake.review(number, "alice", "APPROVED")
    assert checker.status("pr:timer", inst) == "approved"
    fake.review(number, "bob", "CHANGES_REQUESTED")
    assert checker.status("pr:timer", inst) == "rejected"
    fake.review(number, "bob", "APPROVED")
    assert checker.status("pr:timer", inst) == "approved"
    fake.push("feat/timer", SHA_B)
    assert checker.status("pr:timer", inst) == "waiting"
    assert checker.status("pr:unbound", inst) == "waiting"


def test_gate_checker_delegates_other_gates() -> None:
    fake = FakeGitHub()
    fallback = _Fallback()
    checker = GitHubGateChecker(fake.adapter(), fallback)
    assert checker.status("spec:timer", _instance()) == "approved"
    assert fallback.calls == ["spec:timer"]
    assert fake.requests == []
    assert GitHubGateChecker(fake.adapter()).status("spec:timer", _instance()) == "waiting"
