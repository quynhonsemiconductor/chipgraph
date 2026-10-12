"""The GitHub steps of `run.sh` with SANDBOX=1 (the lead runs them; CI never does).

    uv run python docs/rtl-target-claude-code/sandbox.py push-base OUT_DIR BRANCH
    uv run python docs/rtl-target-claude-code/sandbox.py check OUT_DIR
    uv run python docs/rtl-target-claude-code/sandbox.py cleanup OUT_DIR

Each refuses to run unless `CHIPGRAPH_SANDBOX_REPO` is `owner/<name>-sandbox` and
`CHIPGRAPH_GITHUB_TOKEN` is set. The token goes to git through chipgraph's `GIT_ASKPASS`
helper only (`GitVcs.push_branch`) and to the API in the `Authorization` header only; it
is never printed, and every message is redacted (`redact_secret`).

- `push-base`: pushes the fixture's `main` as the PR's base branch `BRANCH` (a new
  branch), so the PR from `chipgraph/<run>/pwm` holds only the block's files. Recorded in
  OUT_DIR/sandbox.json.
- `check`: reads the PR of `pr/pwm.json` (open, its head the record's commit, its base
  the base branch) and its files; writes them to OUT_DIR/sandbox.json for `report.py`.
- `cleanup` (run.sh's `trap ... EXIT`): closes the PR and deletes the PR branch and the
  base branch. It never merges.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.github.com"
REPO_ENV = "CHIPGRAPH_SANDBOX_REPO"
TOKEN_ENV = "CHIPGRAPH_GITHUB_TOKEN"
STATE = "sandbox.json"
PR_RECORD = "tinysoc/pr/pwm.json"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+-sandbox$")


class SandboxError(Exception):
    """A sandbox step failed (the message holds no secret)."""


def _env() -> tuple[str, str]:
    repo = os.environ.get(REPO_ENV, "")
    token = os.environ.get(TOKEN_ENV, "")
    if not _REPO_RE.match(repo) or not token:
        raise SandboxError(f"{REPO_ENV} must be owner/<name>-sandbox and {TOKEN_ENV} set")
    return repo, token


def _redact(text: str, token: str) -> str:
    from chipgraph.adapters.vcs.git import redact_secret

    return redact_secret(text, token)


def _api(method: str, path: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "chipgraph-e2e",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SandboxError(_redact(f"{method} {path}: HTTP {exc.code} {detail}", token)) from None
    except urllib.error.URLError as exc:
        raise SandboxError(_redact(f"{method} {path}: {exc.reason}", token)) from None
    return json.loads(raw) if raw else None


def _state(out: Path) -> dict[str, Any]:
    path = out / STATE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _save(out: Path, state: dict[str, Any]) -> None:
    (out / STATE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def push_base(out: Path, branch: str) -> None:
    from chipgraph.adapters.vcs.git import GitVcs

    _repo, token = _env()
    proj = out / "tinysoc"
    vcs = GitVcs()
    vcs.push_branch(proj, "origin", vcs.head(proj), branch, token=token)
    _save(out, {**_state(out), "base": branch, "base_pushed": True})
    print(f"pushed the base branch {branch}")


def _record(out: Path) -> dict[str, Any] | None:
    path = out / PR_RECORD
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def check(out: Path) -> None:
    repo, token = _env()
    state = _state(out)
    record = _record(out)
    if record is None or not isinstance(record.get("number"), int):
        state["pr"] = None
        _save(out, state)
        raise SandboxError(f"no pull request in {PR_RECORD}")
    number = record["number"]
    pull = _api("GET", f"/repos/{repo}/pulls/{number}", token)
    files = _api("GET", f"/repos/{repo}/pulls/{number}/files?per_page=100", token)
    state["pr"] = {
        "number": number,
        "url": pull.get("html_url"),
        "state": pull.get("state"),
        "merged": pull.get("merged"),
        "head_ref": pull.get("head", {}).get("ref"),
        "head_sha": pull.get("head", {}).get("sha"),
        "base_ref": pull.get("base", {}).get("ref"),
        "files": sorted(f["filename"] for f in files),
    }
    state["branch"] = record.get("branch")
    _save(out, state)
    print(f"pull request #{number}: {state['pr']['state']}, {len(files)} file(s)")


def cleanup(out: Path) -> None:
    repo, token = _env()
    state = _state(out)
    record = _record(out) or {}
    done: list[str] = []
    number = record.get("number")
    if isinstance(number, int):
        _api("PATCH", f"/repos/{repo}/pulls/{number}", token, {"state": "closed"})
        done.append(f"closed #{number}")
    for branch in (record.get("branch"), state.get("base")):
        if not isinstance(branch, str) or not branch:
            continue
        ref = urllib.parse.quote(f"heads/{branch}", safe="/")
        try:
            _api("DELETE", f"/repos/{repo}/git/refs/{ref}", token)
            done.append(f"deleted {branch}")
        except SandboxError as exc:
            done.append(f"could not delete {branch}: {exc}")
    state["cleanup"] = done
    _save(out, state)
    print("; ".join(done) or "nothing to clean up")


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == "push-base":
            push_base(Path(argv[1]), argv[2])
        elif len(argv) == 2 and argv[0] == "check":
            check(Path(argv[1]))
        elif len(argv) == 2 and argv[0] == "cleanup":
            cleanup(Path(argv[1]))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except Exception as exc:  # every message is redacted before it is printed
        token = os.environ.get(TOKEN_ENV, "")
        print(f"sandbox: {_redact(str(exc), token) if token else exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
