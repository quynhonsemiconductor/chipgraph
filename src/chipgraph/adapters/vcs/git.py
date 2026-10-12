"""The `git` VCS adapter: head, changed files, and disposable worktrees.

Implements `chipgraph.core.plugin_api.protocols.VcsAdapter`, and the fan-out port
`chipgraph.core.engine.fanout.FanoutVcs` (worktrees at a commit, commits, patches,
the result branch). All git invocations go through `subprocess.run` with argument lists
(never `shell=True`); a failed command raises `VcsError` naming the command and its
stderr.

`push_branch` (M2-11a) publishes a result branch: a fast-forward push, never a force,
with an HTTPS token given to git only through a `GIT_ASKPASS` helper (`askpass_helper`).

The fan-out methods never touch the working tree, index or HEAD of the user's checkout:
they run in their own worktree (or only add a ref), with no hooks
(`core.hooksPath=/dev/null`, `--no-verify`), no signing, no auto gc, and an environment
from which variables that would redirect git (`GIT_DIR`, `GIT_INDEX_FILE`, ...) are
removed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from chipgraph.core.engine.fanout import FileChange, FileStatus

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class VcsError(Exception):
    """Raised when a git command fails, or a workspace operation is misused."""


def main_repo(path: Path) -> Path:
    """Return the main repository directory for the (possibly worktree) checkout at `path`."""
    result = _run(["git", "-C", str(path), "rev-parse", "--git-common-dir"], cwd=path)
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (path / common_dir).resolve()
    # `--git-common-dir` points at the `.git` directory; the repo root is its parent,
    # unless the repo's `.git` dir does not sit directly under the worktree root (rare
    # for our purposes), in which case the common dir itself is still a safe `-C` target.
    if common_dir.name == ".git":
        return common_dir.parent
    return common_dir


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise VcsError(f"command {' '.join(cmd)!r} failed in {cwd}: {exc.stderr.strip()}") from exc
    except FileNotFoundError as exc:
        raise VcsError(f"command {' '.join(cmd)!r} failed in {cwd}: {exc}") from exc


# Variables that point git at another repo, index or object store: a fan-out must
# never inherit them (e.g. when chipgraph runs from inside a git hook).
_REDIRECT_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES",
)
_SAFE_CONFIG = (
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "-c",
    "core.autocrlf=false",
)
_IDENTITY = {
    "GIT_AUTHOR_NAME": "chipgraph",
    "GIT_AUTHOR_EMAIL": "chipgraph@localhost",
    "GIT_COMMITTER_NAME": "chipgraph",
    "GIT_COMMITTER_EMAIL": "chipgraph@localhost",
}
_STATUS: dict[str, FileStatus] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "T": "type_changed",
}


def _fanout_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REDIRECT_VARS}
    # The identity comes from the environment when set there, else a fixed one; the
    # user's git config identity is not needed (and may be missing on a CI runner).
    for key, value in _IDENTITY.items():
        env.setdefault(key, value)
    return env


def _git(
    args: Sequence[str], *, cwd: Path, input: bytes | None = None, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    """Run `git <safe config> <args>` in `cwd` for the fan-out; bytes in and out."""
    cmd = ["git", *_SAFE_CONFIG, *args]
    try:
        return subprocess.run(
            cmd, check=check, capture_output=True, cwd=cwd, input=input, env=_fanout_env()
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace").strip()
        raise VcsError(f"command {' '.join(cmd)!r} failed in {cwd}: {stderr}") from exc
    except FileNotFoundError as exc:
        raise VcsError(f"command {' '.join(cmd)!r} failed in {cwd}: {exc}") from exc


def _out(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def _parse_raw_z(output: bytes) -> tuple[FileChange, ...]:
    """Parse `git diff --raw -z --no-renames` output into `FileChange`s, sorted by path."""
    fields = output.decode("utf-8", "surrogateescape").split("\0")
    changes: list[FileChange] = []
    i = 0
    while i + 1 < len(fields):
        meta, path = fields[i], fields[i + 1]
        i += 2
        if not meta.startswith(":"):
            continue
        old_mode, new_mode, _old, _new, status = meta[1:].split(" ", 4)
        changes.append(
            FileChange(
                path=path,
                status=_STATUS.get(status[:1], "modified"),
                old_mode=None if old_mode == "000000" else old_mode,
                new_mode=None if new_mode == "000000" else new_mode,
            )
        )
    return tuple(sorted(changes, key=lambda c: c.path))


def _parse_porcelain_z(output: str) -> set[str]:
    """Parse `git status --porcelain=v1 -z` output into a set of repo-relative paths."""
    paths: set[str] = set()
    fields = output.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        if entry == "":
            i += 1
            continue
        status = entry[:2]
        path = entry[3:]
        paths.add(path)
        i += 1
        if status[0] in ("R", "C") and i < len(fields) and fields[i] != "":
            # Renamed/copied entries carry the original path as a following NUL field.
            paths.add(fields[i])
            i += 1
    return paths


class GitVcs:
    """The `git` implementation of `VcsAdapter` and of the fan-out port `FanoutVcs`."""

    name = "git"

    def __init__(self, workspaces_dir: Path | None = None) -> None:
        self._workspaces_dir = workspaces_dir

    def head(self, root: Path) -> str:
        """Return the current HEAD revision (full sha) of the repo at `root`."""
        result = _run(["git", "rev-parse", "HEAD"], cwd=root)
        return result.stdout.strip()

    def changed_files(self, root: Path, since: str | None = None) -> tuple[str, ...]:
        """Return repo-relative paths changed in the working tree/index, and since `since`."""
        paths: set[str] = set()

        status = _run(["git", "status", "--porcelain=v1", "-z"], cwd=root)
        paths |= _parse_porcelain_z(status.stdout)

        if since is not None:
            diff = _run(["git", "diff", "--name-only", "-z", since, "HEAD"], cwd=root)
            paths |= {p for p in diff.stdout.split("\0") if p}

        return tuple(sorted(paths))

    def create_workspace(self, root: Path, name: str) -> Path:
        """Create a detached worktree named `name` under the workspaces directory."""
        if not _NAME_RE.match(name):
            raise VcsError(f"invalid workspace name {name!r}: must match {_NAME_RE.pattern!r}")
        base = self._workspaces_dir or (root / ".chipgraph" / "state" / "worktrees")
        target = base / name
        if target.exists():
            raise VcsError(f"workspace path already exists: {target}")
        base.mkdir(parents=True, exist_ok=True)
        _run(["git", "worktree", "add", "--detach", str(target), "HEAD"], cwd=root)
        return target

    def remove_workspace(self, path: Path) -> None:
        """Remove a worktree previously created by `create_workspace`."""
        repo = main_repo(path)
        _run(["git", "worktree", "remove", "--force", str(path)], cwd=repo)
        _run(["git", "worktree", "prune"], cwd=repo)

    # --- the fan-out port (`chipgraph.core.engine.fanout.FanoutVcs`) -------------------

    def create_branch_workspace(self, root: Path, base: str, name: str, dest: Path) -> Path:
        """A detached worktree of `root` at commit `base`, at `dest / name`."""
        if not _NAME_RE.match(name):
            raise VcsError(f"invalid workspace name {name!r}: must match {_NAME_RE.pattern!r}")
        target = dest / name
        if target.exists():
            raise VcsError(f"workspace path already exists: {target}")
        dest.mkdir(parents=True, exist_ok=True)
        _git(["worktree", "add", "--detach", "--quiet", str(target), base], cwd=root)
        return target

    def remove(self, path: Path) -> None:
        """Remove the worktree at `path` (even with changes, even if locked); no-op if gone."""
        if not path.exists():
            return
        try:
            repo = main_repo(path)
            _git(["worktree", "remove", "--force", "--force", str(path)], cwd=repo)
        except VcsError:
            pass
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def prune(self, root: Path) -> None:
        """Forget worktrees of `root` whose directory is gone."""
        _git(["worktree", "prune"], cwd=root)

    def list_workspaces(self, root: Path) -> tuple[Path, ...]:
        """Every linked worktree of the repo at `root` (not the main one)."""
        listing = _out(_git(["worktree", "list", "--porcelain", "-z"], cwd=root))
        paths = [
            Path(entry[len("worktree ") :])
            for entry in listing.split("\0")
            if entry.startswith("worktree ")
        ]
        return tuple(paths[1:])

    def commit_all(
        self,
        path: Path,
        message: str,
        paths: Sequence[str] | None = None,
        *,
        include_ignored: Sequence[str] = (),
    ) -> str:
        """Commit the worktree at `path`; HEAD when nothing is staged. No hooks run."""
        if paths is None:
            _git(["add", "-A"], cwd=path)
            staged = list(include_ignored)
        else:
            staged = list(paths)
        for rel in staged:
            if (path / rel).exists() or (path / rel).is_symlink():
                _git(["add", "-f", "--", rel], cwd=path)
            else:
                _git(["rm", "--cached", "-q", "--ignore-unmatch", "--", rel], cwd=path)
        quiet = _git(["diff", "--cached", "--quiet"], cwd=path, check=False)
        if quiet.returncode == 0:
            return _out(_git(["rev-parse", "HEAD"], cwd=path))
        _git(["commit", "-q", "--no-verify", "--no-gpg-sign", "-m", message], cwd=path)
        return _out(_git(["rev-parse", "HEAD"], cwd=path))

    def diff(self, path: Path, since: str, until: str) -> tuple[FileChange, ...]:
        """The files changed between two commits, sorted by path."""
        result = _git(["diff", "--raw", "-z", "--no-renames", since, until], cwd=path)
        return _parse_raw_z(result.stdout)

    def patch(self, path: Path, since: str, until: str, paths: Sequence[str]) -> bytes:
        """A binary-safe patch of `paths` between two commits."""
        if not paths:
            return b""
        result = _git(
            [
                "diff",
                "--binary",
                "--full-index",
                "--no-renames",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                since,
                until,
                "--",
                *paths,
            ],
            cwd=path,
        )
        return result.stdout

    def apply_patch(self, path: Path, patch: bytes) -> str | None:
        """Apply `patch` to the worktree at `path`, all or nothing; why not, if it fails."""
        if not patch:
            return None
        result = _git(
            ["apply", "--binary", "--whitespace=nowarn", "-"], cwd=path, input=patch, check=False
        )
        if result.returncode == 0:
            return None
        return result.stderr.decode("utf-8", "replace").strip() or "the patch does not apply"

    def tree_hash(self, path: Path, commit: str) -> str:
        """The tree id of `commit`."""
        return _out(_git(["rev-parse", f"{commit}^{{tree}}"], cwd=path))

    def create_branch(self, root: Path, name: str, commit: str) -> None:
        """Create branch `name` at `commit`; fails if it exists. HEAD is not moved."""
        _git(["check-ref-format", "--branch", name], cwd=root)
        _git(["branch", "--no-track", "--", name, commit], cwd=root)

    # --- publishing a result branch (M2-11a) -----------------------------------------

    def branch_tip(self, root: Path, name: str) -> str | None:
        """The commit branch `name` points at, or `None` if there is no such branch."""
        _git(["check-ref-format", "--branch", name], cwd=root)
        result = _git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}^{{commit}}"],
            cwd=root,
            check=False,
        )
        if result.returncode != 0:
            return None
        return _out(result) or None

    def advance_branch(self, root: Path, name: str, commit: str, old: str) -> None:
        """Move branch `name` from `old` to `commit`, only if `commit` descends from `old`.

        A fast-forward only, and only if the branch is still at `old` (compare and swap);
        HEAD, the index and the working tree are not touched.
        """
        if commit == old:
            return
        ancestor = _git(["merge-base", "--is-ancestor", old, commit], cwd=root, check=False)
        if ancestor.returncode != 0:
            raise VcsError(
                f"refusing to move branch {name!r}: {commit} does not descend from {old}"
            )
        _git(["update-ref", f"refs/heads/{name}", commit, old], cwd=root)

    def remote_url(self, root: Path, remote: str) -> str:
        """The URL git pushes to for `remote` (raises `VcsError` if there is none)."""
        return _out(_git(["remote", "get-url", "--push", "--", remote], cwd=root))

    def push_branch(
        self,
        root: Path,
        remote: str,
        commit: str,
        branch: str,
        *,
        token: str | None = None,
        username: str = "x-access-token",
    ) -> None:
        """Push `commit` to `refs/heads/<branch>` of `remote`: a fast-forward, never a force.

        With `token` (an HTTPS remote), git gets it through a `GIT_ASKPASS` helper script
        in a private temporary directory, removed afterwards, which prints the token from one
        environment variable of the git process: never in the command line, the URL, git
        config, a file or an error message. No credential
        helper runs (so nothing is stored), no terminal prompt, no hook. A remote that
        refuses (not a fast-forward, no access) raises `VcsError`.
        """
        _git(["check-ref-format", "--branch", branch], cwd=root)
        args = [
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "push",
            "--porcelain",
            "--no-verify",
            "--",
            remote,
            f"{commit}:refs/heads/{branch}",
        ]
        env = {k: v for k, v in _fanout_env().items() if k not in _TOKEN_VARS}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.pop("SSH_ASKPASS", None)
        if token is None:
            env.pop("GIT_ASKPASS", None)
            _push(args, root, env, secret=None)
            return
        with tempfile.TemporaryDirectory(prefix="chipgraph-askpass-") as tmp:
            helper = askpass_helper(Path(tmp), username=username)
            env["GIT_ASKPASS"] = str(helper)
            env[ASKPASS_VALUE_VAR] = token
            _push(args, root, env, secret=token)


_TOKEN_VARS = (
    "CHIPGRAPH_GITHUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GIT_ASKPASS",
    "CHIPGRAPH_ASKPASS_VALUE",
)
"""Variables a push never passes on to git: tokens, and any other askpass helper."""


ASKPASS_VALUE_VAR = "CHIPGRAPH_ASKPASS_VALUE"
"""The variable of the git process that the askpass helper prints as the password."""


def askpass_helper(directory: Path, *, username: str = "x-access-token") -> Path:
    """Write a `GIT_ASKPASS` helper in `directory` (private): it prints `username` for git's
    username prompt and the value of `ASKPASS_VALUE_VAR` for its password prompt.

    The script holds no secret and nothing secret is written to disk: the token is given to
    the git process only as that one environment variable (never in the command line, the
    URL or git's config), and is gone when the process ends.
    """
    directory.chmod(0o700)
    script = directory / "askpass.sh"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  Username*|username*) printf '%s\\n' {_sh_quote(username)} ;;\n"
        f"  *) printf '%s\\n' \"${{{ASKPASS_VALUE_VAR}}}\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _push(args: Sequence[str], root: Path, env: dict[str, str], *, secret: str | None) -> None:
    cmd = ["git", *_SAFE_CONFIG, *args]
    try:
        subprocess.run(cmd, check=True, capture_output=True, cwd=root, env=env)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace").strip()
        stdout = exc.stdout.decode("utf-8", "replace").strip()
        message = f"git push to {args[-2]!r} failed in {root}: {stderr or stdout}"
        if secret:
            message = message.replace(secret, "***")
        raise VcsError(message) from None
    except FileNotFoundError as exc:
        raise VcsError(f"git push failed in {root}: {exc}") from None
