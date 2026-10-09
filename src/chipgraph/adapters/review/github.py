"""The `github` review adapter: opens pull requests and reads their reviews as `pr` gates.

Implements `chipgraph.core.plugin_api.protocols.ReviewAdapter` over the GitHub REST
API (DESIGN.md 6.1: a heavy gate's decision *is* the PR review, with no extra file).

What it does, and what it never does
------------------------------------
- `open_pr` opens a PR from a branch that is already pushed (pushing is the VCS
  adapter's job). It is idempotent: an open PR for the same head branch is returned.
- `pull_request` / `reviews` read a PR's state, head commit and each reviewer's latest
  review. `approvals(gate_id)` turns the reviews of the PR bound to `gate_id` into
  `Approval`s.
- It never merges, never turns on automatic merging, never submits, approves or
  dismisses a review, never updates or closes a PR and never deletes a branch. The HTTP
  layer only allows `GET` of PRs and reviews, and `POST` to the PR-create endpoint;
  any other request is refused before it is sent. `record()` always raises
  `ReviewError`: a decision on a PR gate is made on the PR itself, and waivers and
  baselines belong to the `file` adapter.

Approvals and the head-commit pin
---------------------------------
Only a reviewer's latest *decisive* review counts (`APPROVED`, `CHANGES_REQUESTED` or
`DISMISSED`; a later `COMMENTED` review does not undo an approval, as on GitHub), and
only when it was made on the PR's **current** head commit: a later push makes it stale.
`APPROVED` becomes `approve`, `CHANGES_REQUESTED` becomes `reject`; `COMMENTED`,
`DISMISSED` and `PENDING` are ignored, and so is the PR author's own review. `by` is
the reviewer's login and `at` the review's `submitted_at`.

Every approval is pinned to the head commit through `artifact_hashes` with one key::

    "<owner>/<name>:pull/<number>/head" -> sha256(<head commit sha, ASCII>)

(`head_key` and `head_digest`). `current_hashes(gate_id)` returns the same key for the
PR's current head, so `approval.is_current(review.current_hashes(gate_id))` tells an
old approval from a new one. `GitHubGateChecker` packages this as the scheduler's
`GateChecker` shape: `core.engine.gate.GateEvaluator` compares approvals with the
gate's *file* hashes on disk, which a PR review is not pinned to, so PR gates are
evaluated here and every other gate is delegated to a fallback (normally the
`GateEvaluator` over the `file` adapter).

Binding a gate to a PR
----------------------
`prs={"pr:timer": 12}` in the constructor, `bind(gate_id, number)`, or
`open_pr(..., gate_id=...)`. With a `state_dir`, bindings persist in
`<state_dir>/review/github.json` (machine-local run state, never in the repo).

Profile options (`review:` in `.chipgraph.yml`)
-----------------------------------------------
::

    review:
      use: github
      repo: owner/name                # required
      base: main                      # default base branch of opened PRs
      api_url: https://api.github.com # GitHub Enterprise: https://<host>/api/v3
      allow_gh_fallback: true         # use `gh auth token` if no env token; off when CI is set
      timeout_s: 30                   # per HTTP request
      max_retries: 3                  # on 5xx, rate limits and (GET) network errors

`api_url` must be `https://` (plain `http://` only for `localhost`/`127.0.0.1`).

Authentication
--------------
The token comes from `CHIPGRAPH_GITHUB_TOKEN`: a fine-grained token for this one repo
with *Contents* and *Pull requests* read/write and nothing else (no admin, no
workflows). If it is unset and `allow_gh_fallback` is true, `gh auth token` is used
(local convenience only); when the `CI` environment variable is set the fallback is
always off. `token_source` records which source was used (`"env"` or `"gh"`). The
token is never logged, printed, put in an exception message or in a `repr`, and
redirects are never followed (so it is never sent to another host).

Manual end-to-end test
----------------------
Run by a reviewer with a token for the private sandbox repo, never in CI::

    export CHIPGRAPH_GITHUB_TOKEN=...   # fine-grained, sandbox repo only
    CHIPGRAPH_SANDBOX_REPO=quynhonsemiconductor/chipgraph-sandbox \\
        uv run pytest tests/e2e_github -q

It refuses any repo whose name does not end in `-sandbox`, never uses the `gh`
fallback, and cleans up the branch and PR it made.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from chipgraph.adapters.review.file import ReviewError
from chipgraph.core.config.models import AdapterCfg
from chipgraph.core.contracts import Approval, RuleInstance
from chipgraph.core.engine.scheduler import GateChecker, GateStatus

__all__ = [
    "TOKEN_ENV",
    "GitHubGateChecker",
    "GitHubReview",
    "HttpRequest",
    "HttpResponse",
    "PullRequest",
    "Review",
    "ReviewError",
    "head_digest",
    "urllib_transport",
]

_log = logging.getLogger(__name__)

TOKEN_ENV = "CHIPGRAPH_GITHUB_TOKEN"
"""The environment variable the token is read from."""

_DEFAULT_API_URL = "https://api.github.com"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_OPTIONS = frozenset({"repo", "base", "api_url", "allow_gh_fallback", "timeout_s", "max_retries"})
_RETRY_STATUSES = frozenset({500, 502, 503, 504})
_MAX_RETRY_AFTER_S = 60.0
_MAX_PAGES = 50
_PER_PAGE = 100
_BINDINGS_FILE = ("review", "github.json")

ReviewState = Literal["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"]
_DECISIVE: frozenset[str] = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
_DECISIONS: dict[str, Literal["approve", "reject"]] = {
    "APPROVED": "approve",
    "CHANGES_REQUESTED": "reject",
}


# --- HTTP transport -----------------------------------------------------------------


@dataclass(frozen=True)
class HttpRequest:
    """One HTTP request. `headers` carries the token, so it is kept out of `repr`."""

    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes | None = field(default=None, repr=False)
    timeout_s: float = 30.0


@dataclass(frozen=True)
class HttpResponse:
    """One HTTP response; `headers` keys are lower-case."""

    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[HttpRequest], HttpResponse]
"""Sends one request. Raises `OSError` (incl. timeouts) for network failures."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would forward the `Authorization` header."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(request: HttpRequest) -> HttpResponse:
    """The default `Transport`: stdlib `urllib`, no redirects, per-request timeout."""
    req = urllib.request.Request(
        request.url, data=request.body, method=request.method, headers=dict(request.headers)
    )
    try:
        with _OPENER.open(req, timeout=request.timeout_s) as resp:
            return HttpResponse(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=resp.read(),
            )
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        try:
            body = exc.read()
        except OSError:
            body = b""
        return HttpResponse(status=exc.code, headers=headers, body=body)


def _gh_auth_token(hostname: str) -> str | None:
    """`gh auth token --hostname <host>`, or None if `gh` is missing or fails."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", hostname],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


# --- data returned to callers -------------------------------------------------------


class PullRequest(BaseModel):
    """A pull request as the engine needs it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    number: int = Field(description="The PR number.")
    url: str = Field(description="The PR's web URL.")
    state: Literal["open", "closed"] = Field(description="Whether the PR is open.")
    merged: bool = Field(default=False, description="Whether the PR has been merged.")
    draft: bool = Field(default=False, description="Whether the PR is a draft.")
    head: str = Field(description="The head branch name.")
    head_sha: str = Field(description="The head commit sha.")
    base: str = Field(description="The base branch name.")
    author: str = Field(description="The login of the PR's author.")


class Review(BaseModel):
    """One review on a pull request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    id: int = Field(description="The review id.")
    by: str = Field(description="The reviewer's login.")
    state: ReviewState = Field(description="The review state.")
    commit_id: str | None = Field(description="The commit the review was made on.")
    submitted_at: AwareDatetime | None = Field(description="When it was submitted.")
    url: str = Field(default="", description="The review's web URL.")


def head_digest(head_sha: str) -> str:
    """The `artifact_hashes` value pinning an approval to commit `head_sha`."""
    return hashlib.sha256(head_sha.encode("ascii")).hexdigest()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _login(user: object) -> str:
    if isinstance(user, dict) and isinstance(user.get("login"), str):
        return str(user["login"])
    return "ghost"  # GitHub's name for a deleted account


# --- the adapter --------------------------------------------------------------------


class GitHubReview:
    """Opens PRs and reads their reviews; never merges (see the module docstring)."""

    name = "github"
    token_source: Literal["env", "gh"] | None
    """Where the token came from, once one has been resolved."""

    _ALLOWED_GET: ClassVar[re.Pattern[str]] = re.compile(r"^/pulls(/[0-9]+(/reviews)?)?$")

    def __init__(
        self,
        repo: str | None = None,
        *,
        base: str = "main",
        api_url: str = _DEFAULT_API_URL,
        allow_gh_fallback: bool = True,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        prs: Mapping[str, int] | None = None,
        state_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        transport: Transport | None = None,
        gh_token: Callable[[str], str | None] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        # The registry builds adapters with no arguments; such an instance has no
        # `repo` and every API method raises. `from_config` builds a configured one.
        if repo is not None and not _REPO_RE.match(repo):
            raise ReviewError(f"review.repo must be 'owner/name', got {repo!r}")
        api_url = api_url.rstrip("/")
        parsed = urllib.parse.urlsplit(api_url)
        if not (
            parsed.scheme == "https"
            or (parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS)
        ):
            raise ReviewError(
                f"review.api_url must be an https:// URL (http:// only for localhost), "
                f"got {api_url!r}"
            )
        if timeout_s <= 0:
            raise ReviewError("review.timeout_s must be > 0")
        if max_retries < 0:
            raise ReviewError("review.max_retries must be >= 0")

        self.repo = repo
        self.base = base
        self.api_url = api_url
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._env: Mapping[str, str] = env if env is not None else os.environ
        # The gh fallback is a local convenience: CI always uses the env token.
        self.allow_gh_fallback = allow_gh_fallback and not self._env.get("CI")
        self._transport: Transport = transport or urllib_transport
        self._gh_token = gh_token or _gh_auth_token
        self._sleep = sleep or time.sleep
        self._token: str | None = None
        self.token_source = None
        self._bindings_path = state_dir.joinpath(*_BINDINGS_FILE) if state_dir is not None else None
        self._prs: dict[str, int] = self._load_bindings()
        self._prs.update(prs or {})

    @classmethod
    def from_config(
        cls,
        cfg: AdapterCfg | Mapping[str, object],
        *,
        state_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        transport: Transport | None = None,
        gh_token: Callable[[str], str | None] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> GitHubReview:
        """Build an adapter from a profile's `review:` options (see the module docstring)."""
        raw = cfg.model_dump() if isinstance(cfg, AdapterCfg) else dict(cfg)
        raw.pop("use", None)
        unknown = sorted(set(raw) - _OPTIONS)
        if unknown:
            raise ReviewError(
                f"unknown review option(s) {unknown} for 'github'; known: {sorted(_OPTIONS)}"
            )
        repo = raw.get("repo")
        if not isinstance(repo, str) or not repo:
            raise ReviewError("review.repo is required for 'github', e.g. 'repo: owner/name'")
        base = raw.get("base", "main")
        api_url = raw.get("api_url", _DEFAULT_API_URL)
        fallback = raw.get("allow_gh_fallback", True)
        timeout_s = raw.get("timeout_s", 30.0)
        max_retries = raw.get("max_retries", 3)
        if not isinstance(base, str) or not base:
            raise ReviewError("review.base must be a branch name")
        if not isinstance(api_url, str):
            raise ReviewError("review.api_url must be a URL")
        if not isinstance(fallback, bool):
            raise ReviewError("review.allow_gh_fallback must be true or false")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
            raise ReviewError("review.timeout_s must be a number of seconds")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise ReviewError("review.max_retries must be an integer")
        return cls(
            repo,
            base=base,
            api_url=api_url,
            allow_gh_fallback=fallback,
            timeout_s=float(timeout_s),
            max_retries=max_retries,
            state_dir=state_dir,
            env=env,
            transport=transport,
            gh_token=gh_token,
            sleep=sleep,
        )

    def __repr__(self) -> str:
        return (
            f"GitHubReview(repo={self.repo!r}, api_url={self.api_url!r}, "
            f"token_source={self.token_source!r})"
        )

    # --- ReviewAdapter -------------------------------------------------------------

    def record(self, approval: Approval) -> None:
        """Always raises: this adapter never records a decision anywhere."""
        if approval.decision in ("approve", "reject"):
            raise ReviewError(
                f"gate {approval.gate_id!r} is decided on its pull request: approve or "
                "request changes in the PR review on GitHub; chipgraph never approves on "
                "GitHub itself"
            )
        raise ReviewError(
            f"the github review adapter does not store {approval.decision!r} decisions "
            f"(gate {approval.gate_id!r}); waivers and baselines go through the 'file' adapter"
        )

    def approvals(self, gate_id: str) -> tuple[Approval, ...]:
        """Approvals from the reviews of the PR bound to `gate_id`, at its current head.

        Returns `()` if no PR is bound to `gate_id`. Sorted by `at`.
        """
        number = self.pr_for(gate_id)
        if number is None:
            return ()
        pr = self.pull_request(number)
        pin = {self.head_key(number): head_digest(pr.head_sha)}
        result: list[Approval] = []
        for review in self.reviews(number):
            decision = _DECISIONS.get(review.state)
            if (
                decision is None
                or review.by == pr.author
                or review.commit_id != pr.head_sha
                or review.submitted_at is None
            ):
                continue
            result.append(
                Approval(
                    gate_id=gate_id,
                    by=review.by,
                    at=review.submitted_at,
                    artifact_hashes=dict(pin),
                    decision=decision,
                    note=(
                        f"GitHub review {review.id} ({review.state}) on {self.repo}#{number} "
                        f"at {pr.head_sha}"
                    ),
                )
            )
        result.sort(key=lambda approval: approval.at)
        return tuple(result)

    # --- head pin ------------------------------------------------------------------

    def head_key(self, number: int) -> str:
        """The `artifact_hashes` key of PR `number`: `<owner>/<name>:pull/<number>/head`."""
        return f"{self._require_repo()}:pull/{number}/head"

    def current_hashes(self, gate_id: str) -> dict[str, str]:
        """`{head_key: head_digest(current head)}` for the PR bound to `gate_id`, or `{}`."""
        number = self.pr_for(gate_id)
        if number is None:
            return {}
        return {self.head_key(number): head_digest(self.pull_request(number).head_sha)}

    # --- gate binding --------------------------------------------------------------

    def pr_for(self, gate_id: str) -> int | None:
        """The PR number bound to `gate_id`, if any."""
        return self._prs.get(gate_id)

    def bind(self, gate_id: str, number: int) -> None:
        """Bind `gate_id` to PR `number`, persisting it when a `state_dir` was given."""
        if number <= 0:
            raise ReviewError(f"invalid PR number {number!r} for gate {gate_id!r}")
        self._prs[gate_id] = number
        self._save_bindings()

    def _load_bindings(self) -> dict[str, int]:
        path = self._bindings_path
        if path is None or not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewError(f"malformed PR binding file {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ReviewError(f"malformed PR binding file {path}: not a JSON object")
        if raw.get("repo") != self.repo:
            return {}  # bindings of another repo (the profile changed): not ours
        gates = raw.get("gates")
        if not isinstance(gates, dict) or not all(
            isinstance(k, str) and isinstance(v, int) for k, v in gates.items()
        ):
            raise ReviewError(f"malformed PR binding file {path}: 'gates' must map id -> int")
        return dict(gates)

    def _save_bindings(self) -> None:
        path = self._bindings_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "repo": self.repo, "gates": dict(sorted(self._prs.items()))}
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-github-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    # --- pull requests -------------------------------------------------------------

    def open_pr(
        self,
        head: str,
        base: str | None,
        title: str,
        body: str = "",
        draft: bool = False,
        *,
        gate_id: str | None = None,
    ) -> PullRequest:
        """Open a PR from the already-pushed branch `head` into `base` (default: config).

        Idempotent: if an open PR from `head` exists, it is returned unchanged. With
        `gate_id`, the gate is bound to the PR.
        """
        if not head:
            raise ReviewError("open_pr needs a head branch name")
        pr = self._find_open_pr(head)
        if pr is None:
            payload = {
                "head": head,
                "base": base or self.base,
                "title": title,
                "body": body,
                "draft": draft,
            }
            try:
                data = self._json("POST", "/pulls", body=payload)
                pr = self._pull_from(data)
            except ReviewError:
                # A concurrent (or retried) create may have opened it already.
                pr = self._find_open_pr(head)
                if pr is None:
                    raise
        if gate_id is not None:
            self.bind(gate_id, pr.number)
        return pr

    def pull_request(self, number: int) -> PullRequest:
        """The current state and head commit of PR `number`."""
        return self._pull_from(self._json("GET", f"/pulls/{int(number)}"))

    def reviews(self, number: int) -> tuple[Review, ...]:
        """Each reviewer's latest review on PR `number` (all pages).

        "Latest" prefers decisive reviews: a reviewer's newest `APPROVED`,
        `CHANGES_REQUESTED` or `DISMISSED` review, else their newest other review.
        """
        items = self._paged(f"/pulls/{int(number)}/reviews")
        all_reviews = sorted((self._review_from(item) for item in items), key=lambda r: r.id)
        latest: dict[str, Review] = {}
        for review in all_reviews:
            current = latest.get(review.by)
            if current is None or (review.state in _DECISIVE) >= (current.state in _DECISIVE):
                latest[review.by] = review
        return tuple(sorted(latest.values(), key=lambda r: r.id))

    def _find_open_pr(self, head: str) -> PullRequest | None:
        owner = self._require_repo().split("/", 1)[0]
        data = self._json("GET", "/pulls", query={"state": "open", "head": f"{owner}:{head}"})
        if not isinstance(data, list):
            raise ReviewError("unexpected GitHub response listing pull requests")
        for item in data:
            pr = self._pull_from(item)
            if pr.head == head and pr.state == "open":
                return pr
        return None

    @staticmethod
    def _pull_from(data: object) -> PullRequest:
        try:
            if not isinstance(data, dict):
                raise TypeError("not a JSON object")
            return PullRequest(
                number=data["number"],
                url=data.get("html_url") or "",
                state=data["state"],
                merged=bool(data.get("merged") or data.get("merged_at")),
                draft=bool(data.get("draft", False)),
                head=data["head"]["ref"],
                head_sha=data["head"]["sha"],
                base=data["base"]["ref"],
                author=_login(data.get("user")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewError(f"unexpected GitHub pull request data: {exc}") from None

    @staticmethod
    def _review_from(data: object) -> Review:
        try:
            if not isinstance(data, dict):
                raise TypeError("not a JSON object")
            return Review(
                id=data["id"],
                by=_login(data.get("user")),
                state=data["state"],
                commit_id=data.get("commit_id"),
                submitted_at=_parse_time(data.get("submitted_at")),
                url=data.get("html_url") or "",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewError(f"unexpected GitHub review data: {exc}") from None

    # --- HTTP ----------------------------------------------------------------------

    def _require_repo(self) -> str:
        if self.repo is None:
            raise ReviewError("review.repo is not set: configure 'review: {use: github, repo: …}'")
        return self.repo

    def _token_value(self) -> str:
        if self._token is not None:
            return self._token
        token = self._env.get(TOKEN_ENV, "").strip()
        source: Literal["env", "gh"] = "env"
        if not token and self.allow_gh_fallback:
            hostname = urllib.parse.urlsplit(self.api_url).hostname or "github.com"
            if hostname == "api.github.com":
                hostname = "github.com"
            token = (self._gh_token(hostname) or "").strip()
            source = "gh"
        if not token:
            why = (
                "the `gh auth token` fallback found none"
                if self.allow_gh_fallback
                else "the `gh` fallback is off (allow_gh_fallback is false or CI is set)"
            )
            raise ReviewError(
                f"no GitHub token: set {TOKEN_ENV} to a fine-grained token for "
                f"{self.repo or 'the review repo'} with Contents and Pull requests "
                f"read/write ({why})"
            )
        self._token = token
        self.token_source = source
        return token

    def _check_allowed(self, method: str, path: str) -> None:
        """Refuse anything but reading PRs/reviews and creating a PR (no-merge guard)."""
        allowed = (method == "GET" and self._ALLOWED_GET.match(path)) or (
            method == "POST" and path == "/pulls"
        )
        if not allowed:
            raise ReviewError(
                f"refusing {method} {path}: the github review adapter only reads pull "
                "requests and reviews and opens pull requests"
            )

    def _json(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> Any:
        response = self._send(method, path, query=query, body=body)
        return self._decode(response, method, path)

    def _paged(self, path: str) -> list[object]:
        items: list[object] = []
        url: str | None = None
        for _ in range(_MAX_PAGES):
            if url is None:
                response = self._send("GET", path, query={"per_page": str(_PER_PAGE)})
            else:
                response = self._send("GET", path, next_url=url)
            data = self._decode(response, "GET", path)
            if not isinstance(data, list):
                raise ReviewError(f"unexpected GitHub response for GET {path}: not a list")
            items.extend(data)
            url = _next_link(response.headers.get("link", ""))
            if url is None:
                return items
        raise ReviewError(f"GET {path}: more than {_MAX_PAGES} pages; refusing to continue")

    @staticmethod
    def _decode(response: HttpResponse, method: str, path: str) -> Any:
        try:
            return json.loads(response.body or b"null")
        except ValueError:
            raise ReviewError(f"GitHub returned invalid JSON for {method} {path}") from None

    def _send(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, object] | None = None,
        next_url: str | None = None,
    ) -> HttpResponse:
        """Send one API request with retries; return a 2xx response or raise `ReviewError`.

        `path` is relative to `/repos/<owner>/<name>` and checked by `_check_allowed`.
        `next_url` (a pagination link) must stay on `api_url` and the same path.
        """
        repo = self._require_repo()
        self._check_allowed(method, path)
        full_path = f"/repos/{repo}{path}"
        if next_url is not None:
            url = next_url
            if urllib.parse.urlsplit(url).path != urllib.parse.urlsplit(
                self.api_url + full_path
            ).path or not url.startswith(self.api_url + "/"):
                raise ReviewError(f"refusing a pagination link off {self.api_url}{full_path}")
        else:
            url = self.api_url + full_path
            if query:
                url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token_value()}",
            "User-Agent": "chipgraph",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = HttpRequest(method, url, headers, data, self.timeout_s)

        attempt = 0
        while True:
            try:
                response = self._transport(request)
            except OSError as exc:
                if method == "GET" and attempt < self.max_retries:
                    attempt += 1
                    _log.debug("%s %s: network error, retry %d", method, full_path, attempt)
                    self._sleep(float(2 ** (attempt - 1)))
                    continue
                reason = "timed out" if isinstance(exc, TimeoutError) else type(exc).__name__
                if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError):
                    reason = "timed out"
                raise ReviewError(
                    f"could not reach GitHub at {self.api_url} ({method} {full_path}): {reason}"
                ) from None
            _log.debug("%s %s -> %d", method, full_path, response.status)
            if 200 <= response.status < 300:
                return response
            wait = self._retry_wait(response, attempt)
            if wait is not None and attempt < self.max_retries:
                attempt += 1
                self._sleep(wait)
                continue
            raise self._error(response, method, full_path) from None

    @staticmethod
    def _retry_wait(response: HttpResponse, attempt: int) -> float | None:
        """Seconds to wait before retrying `response`, or None if it is not retryable."""
        if response.status in _RETRY_STATUSES:
            return float(2**attempt)
        if response.status not in (403, 429):
            return None
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                seconds = float(retry_after)
            except ValueError:
                seconds = _MAX_RETRY_AFTER_S
            return seconds if seconds <= _MAX_RETRY_AFTER_S else None
        if response.status == 429 or b"secondary rate limit" in response.body.lower():
            return _MAX_RETRY_AFTER_S
        return None

    def _error(self, response: HttpResponse, method: str, path: str) -> ReviewError:
        status = response.status
        where = f"{method} {path}"
        if status == 401:
            return ReviewError(
                f"GitHub rejected the token (401) on {where}: check {TOKEN_ENV} is a valid, "
                "unexpired token"
            )
        if status in (403, 429):
            if _is_rate_limited(response):
                return ReviewError(
                    f"GitHub rate limit hit ({status}) on {where} and the wait is too long "
                    "or the retries ran out; try again later"
                )
            return ReviewError(
                f"GitHub refused {where} (403): check the token has 'Pull requests' and "
                f"'Contents' read/write access on {self.repo}"
            )
        if status == 404:
            return ReviewError(
                f"GitHub returned 404 for {where}: check review.repo {self.repo!r} and the PR "
                "number exist, and that the token can see the repo (private repos return 404 "
                "to tokens without access)"
            )
        if status == 422:
            return ReviewError(f"GitHub rejected {where} (422): {_validation_message(response)}")
        if 300 <= status < 400:
            return ReviewError(
                f"GitHub redirected {where} ({status}); redirects are not followed: check "
                f"review.repo {self.repo!r} (renamed or moved?) and review.api_url"
            )
        return ReviewError(f"GitHub API error {status} on {where}")


def _is_rate_limited(response: HttpResponse) -> bool:
    return (
        "retry-after" in response.headers
        or response.headers.get("x-ratelimit-remaining") == "0"
        or b"rate limit" in response.body.lower()
    )


def _validation_message(response: HttpResponse) -> str:
    """GitHub's own `message` and `errors[].message` texts, shortened."""
    try:
        data = json.loads(response.body)
    except ValueError:
        return "validation failed"
    if not isinstance(data, dict):
        return "validation failed"
    parts = [str(data.get("message") or "validation failed")]
    for err in data.get("errors") or ():
        if isinstance(err, dict) and err.get("message"):
            parts.append(str(err["message"]))
        elif isinstance(err, dict) and err.get("code"):
            parts.append(f"{err.get('field', '')} {err['code']}".strip())
    return "; ".join(parts)[:300]


_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


def _next_link(link_header: str) -> str | None:
    match = _LINK_NEXT.search(link_header)
    return match.group(1) if match else None


# --- gate checker -------------------------------------------------------------------


class GitHubGateChecker:
    """The scheduler's `GateChecker` for PR gates, delegating the rest to `fallback`.

    A gate whose id prefix (the part before `:`) is in `pr_gates` is `rejected` if a
    current review requests changes, `approved` if one approves, else `waiting`
    (also when no PR is bound yet). "Current" means pinned to the PR's head commit
    now (`current_hashes`). Other gates go to `fallback` (`waiting` if none).
    """

    def __init__(
        self,
        review: GitHubReview,
        fallback: GateChecker | None = None,
        *,
        pr_gates: Collection[str] = ("pr",),
    ) -> None:
        self.review = review
        self.fallback = fallback
        self.pr_gates = frozenset(pr_gates)

    def status(self, gate_id: str, instance: RuleInstance) -> GateStatus:
        """Return the status of `gate_id` (see the class docstring)."""
        if gate_id.split(":", 1)[0] not in self.pr_gates:
            return self.fallback.status(gate_id, instance) if self.fallback else "waiting"
        current = self.review.current_hashes(gate_id)
        if not current:
            return "waiting"
        decisions = [
            approval
            for approval in self.review.approvals(gate_id)
            if approval.is_current(current) and current.keys() <= approval.artifact_hashes.keys()
        ]
        if any(approval.decision == "reject" for approval in decisions):
            return "rejected"
        if any(approval.decision == "approve" for approval in decisions):
            return "approved"
        return "waiting"
