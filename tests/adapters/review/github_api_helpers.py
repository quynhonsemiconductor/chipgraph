"""An in-process fake of the GitHub REST endpoints the `github` review adapter uses.

`FakeGitHub` is a `Transport` (call it with an `HttpRequest`) and can also be served
over real HTTP on 127.0.0.1 (`serve()`), so the default `urllib` transport is tested
too. It models pull requests, reviews and branches, records every request, and can be
told to fail (`fail(...)`) with any status and headers. No network beyond localhost.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from chipgraph.adapters.review.github import GitHubReview, HttpRequest, HttpResponse

TOKEN = "ghp_FAKE0123456789abcdefSECRETtoken"
REPO = "acme/chip"
BOT = "chipgraph-bot"


@dataclass
class _Failure:
    method: str
    path: re.Pattern[str]
    status: int
    headers: dict[str, str]
    body: bytes
    times: int


@dataclass(frozen=True)
class Seen:
    """A request the fake received: method, path (no query) and query parameters."""

    method: str
    path: str
    query: dict[str, str]


class FakeGitHub:
    """A tiny GitHub: one repo, its branches, pull requests and reviews."""

    def __init__(
        self,
        *,
        repo: str = REPO,
        token: str = TOKEN,
        author: str = BOT,
        page_size: int = 100,
        base_url: str = "https://api.github.test",
    ) -> None:
        self.repo = repo
        self.token = token
        self.author = author
        self.page_size = page_size
        self.base_url = base_url
        self.branches: dict[str, str] = {"main": "0" * 40}
        self.prs: dict[int, dict[str, Any]] = {}
        self.pr_reviews: dict[int, list[dict[str, Any]]] = {}
        self.requests: list[Seen] = []
        self.delay_s = 0.0
        self._failures: list[_Failure] = []
        self._numbers = itertools.count(1)
        self._review_ids = itertools.count(1000)
        self._clock = itertools.count(1)

    # --- scenario helpers --------------------------------------------------------

    def push(self, branch: str, sha: str) -> None:
        """Create or move `branch` to `sha`, updating any open PR from it."""
        self.branches[branch] = sha
        for pr in self.prs.values():
            if pr["head"]["ref"] == branch and pr["state"] == "open":
                pr["head"]["sha"] = sha

    def add_pr(self, branch: str, sha: str, *, base: str = "main") -> int:
        """Push `branch` and open a PR from it as the bot; return its number."""
        self.push(branch, sha)
        return self._create(branch, base, "t", "", False)["number"]

    def review(
        self, number: int, login: str, state: str, *, commit_id: str | None = None
    ) -> dict[str, Any]:
        """Add a review by `login` (on the PR's current head unless `commit_id`)."""
        tick = next(self._clock)
        item = {
            "id": next(self._review_ids),
            "user": {"login": login},
            "state": state,
            "commit_id": commit_id or self.prs[number]["head"]["sha"],
            "submitted_at": None
            if state == "PENDING"
            else f"2026-10-01T00:{tick // 60:02d}:{tick % 60:02d}Z",
            "html_url": f"https://github.test/{self.repo}/pull/{number}#review",
            "body": "",
        }
        self.pr_reviews.setdefault(number, []).append(item)
        return item

    def fail(
        self,
        method: str,
        path_regex: str,
        status: int,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | dict[str, Any] = b"",
        times: int = 1,
    ) -> None:
        """Answer the next `times` matching requests with `status` instead."""
        raw = json.dumps(body).encode() if isinstance(body, dict) else body
        self._failures.append(
            _Failure(method, re.compile(path_regex), status, dict(headers or {}), raw, times)
        )

    def adapter(self, **kwargs: Any) -> GitHubReview:
        """A `GitHubReview` wired to this fake, with the token in a private env."""
        env = kwargs.pop("env", {"CHIPGRAPH_GITHUB_TOKEN": self.token})
        kwargs.setdefault("sleep", lambda _s: None)
        return GitHubReview(self.repo, api_url=self.base_url, env=env, transport=self, **kwargs)

    # --- transport ---------------------------------------------------------------

    def __call__(self, request: HttpRequest) -> HttpResponse:
        status, headers, body = self.handle(
            request.method, request.url, dict(request.headers), request.body
        )
        return HttpResponse(status, {k.lower(): v for k, v in headers.items()}, body)

    def handle(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        parts = urllib.parse.urlsplit(url)
        prefix = urllib.parse.urlsplit(self.base_url).path.rstrip("/")
        path = parts.path[len(prefix) :] if parts.path.startswith(prefix) else parts.path
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.requests.append(Seen(method, path, query))

        for failure in self._failures:
            if failure.times > 0 and failure.method == method and failure.path.search(path):
                failure.times -= 1
                return failure.status, failure.headers, failure.body

        auth = {k.lower(): v for k, v in headers.items()}.get("authorization")
        if auth != f"Bearer {self.token}":
            return _json(401, {"message": "Bad credentials"})
        repo_prefix = f"/repos/{self.repo}"
        if not path.startswith(repo_prefix + "/"):
            return _json(404, {"message": "Not Found"})
        rest = path[len(repo_prefix) :]

        if method == "GET" and rest == "/pulls":
            head = query.get("head", "")
            branch = head.split(":", 1)[1] if ":" in head else head
            state = query.get("state", "open")
            found = [
                pr
                for pr in self.prs.values()
                if (not branch or pr["head"]["ref"] == branch)
                and (state == "all" or pr["state"] == state)
            ]
            return _json(200, found)
        if method == "POST" and rest == "/pulls":
            payload = json.loads(body or b"{}")
            branch = payload.get("head")
            if branch not in self.branches:
                return _json(
                    422,
                    {
                        "message": "Validation Failed",
                        "errors": [{"resource": "PullRequest", "field": "head", "code": "invalid"}],
                    },
                )
            if any(
                pr["head"]["ref"] == branch and pr["state"] == "open" for pr in self.prs.values()
            ):
                return _json(
                    422,
                    {
                        "message": "Validation Failed",
                        "errors": [{"message": f"A pull request already exists for {branch}."}],
                    },
                )
            pr = self._create(
                branch,
                payload.get("base", "main"),
                payload.get("title", ""),
                payload.get("body", ""),
                bool(payload.get("draft")),
            )
            return _json(201, pr)
        match = re.fullmatch(r"/pulls/(\d+)", rest)
        if method == "GET" and match:
            pr = self.prs.get(int(match.group(1)))
            return _json(200, pr) if pr else _json(404, {"message": "Not Found"})
        match = re.fullmatch(r"/pulls/(\d+)/reviews", rest)
        if method == "GET" and match:
            number = int(match.group(1))
            if number not in self.prs:
                return _json(404, {"message": "Not Found"})
            items = self.pr_reviews.get(number, [])
            per_page = min(int(query.get("per_page", "30")), self.page_size)
            page = int(query.get("page", "1"))
            chunk = items[(page - 1) * per_page : page * per_page]
            extra: dict[str, str] = {}
            if page * per_page < len(items):
                next_q = urllib.parse.urlencode({"per_page": per_page, "page": page + 1})
                extra["Link"] = f'<{self.base_url}{path}?{next_q}>; rel="next"'
            status, hdrs, raw = _json(200, chunk)
            return status, {**hdrs, **extra}, raw
        return _json(404, {"message": "Not Found"})

    def _create(self, branch: str, base: str, title: str, body: str, draft: bool) -> dict[str, Any]:
        number = next(self._numbers)
        pr = {
            "number": number,
            "html_url": f"https://github.test/{self.repo}/pull/{number}",
            "state": "open",
            "draft": draft,
            "merged": False,
            "title": title,
            "body": body,
            "user": {"login": self.author},
            "head": {"ref": branch, "sha": self.branches[branch]},
            "base": {"ref": base},
        }
        self.prs[number] = pr
        return pr

    # --- real HTTP -----------------------------------------------------------------

    @contextmanager
    def serve(self) -> Iterator[str]:
        """Serve this fake on 127.0.0.1; yields the API base URL to give the adapter."""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _any(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                if fake.delay_s:
                    time.sleep(fake.delay_s)
                url = f"{fake.base_url}{self.path}"
                status, headers, raw = fake.handle(self.command, url, dict(self.headers), body)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

            def log_message(self, format: str, *args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        old = self.base_url
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield self.base_url
        finally:
            server.shutdown()
            server.server_close()
            self.base_url = old


def _json(status: int, payload: object) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/json"}, json.dumps(payload).encode()
