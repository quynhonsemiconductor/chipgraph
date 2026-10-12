"""M2-11a: `GitVcs.push_branch` publishes a result branch, never by force, never with a
token anywhere git or a log could show it.

- a local-path or `file://` remote needs no token; a new branch is created, a later push
  of a descendant is a fast-forward, a push that is not a fast-forward is refused and the
  remote is unchanged;
- an HTTPS token reaches git only through a `GIT_ASKPASS` helper: not in the command line,
  the URL, git's config, a file, an error, or any variable but `CHIPGRAPH_ASKPASS_SECRET`;
- the push runs no hook and no credential helper, and does not prompt.
"""

from __future__ import annotations

import base64
import http.server
import os
import stat
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from chipgraph.adapters.vcs import git as git_module
from chipgraph.adapters.vcs.git import ASKPASS_SECRET_VAR, GitVcs, VcsError, askpass_helper

TOKEN = "ghp_FAKE0123456789abcdefSECRETtoken"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, name: str, parent: str | None = None) -> str:
    (root / name).write_text(f"{name}\n")
    _git(root, "add", name)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", name)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "work"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "a.txt")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    _git(root, "remote", "add", "origin", str(bare))
    return root, bare


def test_a_new_branch_then_a_fast_forward(repo: tuple[Path, Path]) -> None:
    root, bare = repo
    vcs = GitVcs()
    first = _git(root, "rev-parse", "HEAD")
    vcs.push_branch(root, "origin", first, "chipgraph/r1/timer")
    assert _git(bare, "rev-parse", "refs/heads/chipgraph/r1/timer") == first
    second = _commit(root, "b.txt")
    vcs.push_branch(root, "origin", second, "chipgraph/r1/timer")
    assert _git(bare, "rev-parse", "refs/heads/chipgraph/r1/timer") == second
    vcs.push_branch(root, "origin", second, "chipgraph/r1/timer")  # nothing new: fine
    assert vcs.remote_url(root, "origin") == str(bare)


def test_a_file_url_remote_needs_no_token(repo: tuple[Path, Path]) -> None:
    root, bare = repo
    _git(root, "remote", "set-url", "origin", bare.as_uri())
    head = _git(root, "rev-parse", "HEAD")
    GitVcs().push_branch(root, "origin", head, "feat")
    assert _git(bare, "rev-parse", "refs/heads/feat") == head


def test_a_push_that_is_not_a_fast_forward_is_refused_and_changes_nothing(
    repo: tuple[Path, Path],
) -> None:
    root, bare = repo
    vcs = GitVcs()
    base = _git(root, "rev-parse", "HEAD")
    tip = _commit(root, "b.txt")
    vcs.push_branch(root, "origin", tip, "feat")
    _git(root, "checkout", "-q", "-b", "other", base)
    rewritten = _commit(root, "c.txt")
    with pytest.raises(VcsError, match="git push to 'origin' failed"):
        vcs.push_branch(root, "origin", rewritten, "feat")
    assert _git(bare, "rev-parse", "refs/heads/feat") == tip  # never forced


def test_a_bad_branch_name_or_remote_is_a_vcs_error(repo: tuple[Path, Path]) -> None:
    root, _ = repo
    head = _git(root, "rev-parse", "HEAD")
    with pytest.raises(VcsError):
        GitVcs().push_branch(root, "origin", head, "bad..name")
    with pytest.raises(VcsError, match="failed"):
        GitVcs().push_branch(root, "nowhere", head, "feat")
    with pytest.raises(VcsError):
        GitVcs().remote_url(root, "nowhere")


def test_no_hook_runs_on_a_push(repo: tuple[Path, Path]) -> None:
    root, _ = repo
    marker = root / "hook-ran"
    hook = root / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    GitVcs().push_branch(root, "origin", _git(root, "rev-parse", "HEAD"), "feat")
    assert not marker.exists()


# --- the token ----------------------------------------------------------------------------


def test_the_askpass_helper_holds_no_secret_and_prints_it_from_the_environment(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "ask"
    directory.mkdir()
    script = askpass_helper(directory)
    assert TOKEN not in script.read_text()
    assert stat.S_IMODE(os.stat(script).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert [p.name for p in directory.iterdir()] == ["askpass.sh"]  # nothing secret on disk

    def ask(prompt: str) -> str:
        env = {"PATH": os.environ["PATH"], ASKPASS_SECRET_VAR: TOKEN}
        return subprocess.run(
            [str(script), prompt], check=True, capture_output=True, text=True, env=env
        ).stdout.strip()

    assert ask("Username for 'https://github.com': ") == "x-access-token"
    assert ask("Password for 'https://x-access-token@github.com': ") == TOKEN


class _Spy:
    """Records every `subprocess.run` of the git adapter, then runs it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        real = subprocess.run

        def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            self.calls.append((list(cmd), dict(kwargs.get("env") or {})))
            return real(cmd, *args, **kwargs)

        monkeypatch.setattr(git_module.subprocess, "run", run)

    def pushes(self) -> list[tuple[list[str], dict[str, str]]]:
        return [c for c in self.calls if "push" in c[0]]


def test_the_token_is_only_in_one_variable_of_the_git_process(
    repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bare = repo
    for name in ("CHIPGRAPH_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.setenv(name, TOKEN)
    monkeypatch.setenv("GIT_ASKPASS", "/usr/bin/evil")
    monkeypatch.setenv("SSH_ASKPASS", "/usr/bin/evil")
    spy = _Spy(monkeypatch)
    head = _git(root, "rev-parse", "HEAD")
    GitVcs().push_branch(root, "origin", head, "feat", token=TOKEN)
    [(cmd, env)] = spy.pushes()
    assert not any(TOKEN in part for part in cmd)
    assert [k for k, v in env.items() if TOKEN in v] == [ASKPASS_SECRET_VAR]
    assert env["GIT_ASKPASS"].endswith("askpass.sh") and env["GIT_ASKPASS"] != "/usr/bin/evil"
    assert not {"CHIPGRAPH_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "SSH_ASKPASS"} & set(env)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "credential.helper=" in cmd and "--no-verify" in cmd
    assert not Path(env["GIT_ASKPASS"]).exists()  # the helper is removed afterwards
    assert TOKEN not in (root / ".git" / "config").read_text()
    assert _git(bare, "rev-parse", "refs/heads/feat") == head
    # Without a token, no helper at all, and no token variable either.
    spy.calls.clear()
    GitVcs().push_branch(root, "origin", head, "again")
    [(cmd, env)] = spy.pushes()
    assert "GIT_ASKPASS" not in env and TOKEN not in " ".join(cmd) + "".join(env.values())


@contextmanager
def _auth_server() -> Iterator[tuple[str, list[str]]]:
    """A local HTTP server that asks for Basic credentials, notes them and refuses."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            auth = self.headers.get("Authorization")
            if auth is None:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="sandbox"')
            else:
                seen.append(auth)
                self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/acme/chip.git", seen
    finally:
        server.shutdown()
        server.server_close()


def test_git_asks_the_helper_and_the_error_never_shows_the_token(
    repo: tuple[Path, Path],
) -> None:
    root, _ = repo
    with _auth_server() as (url, seen):
        _git(root, "remote", "set-url", "origin", url)
        with pytest.raises(VcsError) as caught:
            GitVcs().push_branch(
                root, "origin", _git(root, "rev-parse", "HEAD"), "feat", token=TOKEN
            )
    assert seen, "git never sent credentials: the askpass helper was not used"
    assert base64.b64decode(seen[0].split()[1]).decode() == f"x-access-token:{TOKEN}"
    assert TOKEN not in str(caught.value)
    assert TOKEN not in (root / ".git" / "config").read_text()
