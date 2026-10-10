"""Fan-out: independent branches in separate workspaces, merged in id order (DESIGN 5.3).

```
run_fanout(branches)
  F2  write sets disjoint, none writes a shared file           (else FanoutError, nothing starts)
  sweep workspaces left by a killed fan-out of this repo
  per branch, at most `max_parallel` at once:
      workspace at the base commit (outside the user's tree), declared inputs copied in
      run the branch -> commit in its workspace -> diff against the seeded state
      a change outside the write set rejects the branch
  F7  a branch that failed can be re-run alone (`previous=`): done branches are kept
  F4  compare the branches' keyed assumptions; a mismatch stops with `needs_human`
  F3  apply each branch's patch onto one integration workspace, in ascending id order;
      a file changed twice, or a patch that does not apply, is a conflict (an error)
  F5  run the join checks on the integration workspace; a failure creates no branch
  commit the merged files, create the branch `chipgraph/<run_id>/<name>`
  always: remove every workspace and the run directory
```

The user's working tree is never written: the base is a commit (HEAD), declared inputs
are *copied* out of the user's tree, and the only thing a fan-out adds to the repo is the
result branch (plus the VCS's own workspace bookkeeping, pruned at the end).

Workspaces live in a per-run directory, ``<tmp>/chipgraph-fanout-<run_id>-<random>/``,
where ``<tmp>`` is ``<state>/tmp/fanout`` when the state directory is outside the repo,
else the system temp directory. The run directory holds a registry file (owner pid,
host, workspaces) so `sweep_stale` can remove what a killed process left behind.

The engine talks to version control through the narrow `FanoutVcs` port only; it never
runs a subprocess itself. Generic: no project, tool or file format is named here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import socket
import tempfile
import time
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.config.models import FanoutCfg
from chipgraph.core.contracts import AgentResult, CheckResult, Issue
from chipgraph.core.engine.scheduler import ExecContext
from chipgraph.core.state.layout import StateLayout, new_run_id

TMP_PREFIX = "chipgraph-fanout-"
"""Prefix of every fan-out run directory."""

REGISTRY_FILE = "fanout.json"
"""The registry file in a run directory: owner pid, host, repo and workspaces."""

JOIN_WORKSPACE = "join"
"""Name of the integration workspace inside a run directory."""

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class FanoutError(Exception):
    """A fan-out that must not start: overlapping write sets (F2), bad paths or ids."""


# --- the VCS port -----------------------------------------------------------------------

FileStatus = Literal["added", "modified", "deleted", "type_changed"]


class FileChange(BaseModel):
    """One file a branch changed, relative to the state it started from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(description="Repo-relative POSIX path.")
    status: FileStatus = Field(description="What happened to the file.")
    old_mode: str | None = Field(default=None, description="Mode before, e.g. '100644'.")
    new_mode: str | None = Field(default=None, description="Mode after, e.g. '100755'.")


@runtime_checkable
class FanoutVcs(Protocol):
    """The version control operations a fan-out needs (e.g. worktrees, commits, patches).

    Separate from the frozen `VcsAdapter`: an adapter implements both. Every method is
    synchronous; none may write the working tree, index or HEAD of the repo at `root`.
    """

    def head(self, root: Path) -> str:
        """The commit checked out at `root` (the base of a fan-out)."""
        ...

    def create_branch_workspace(self, root: Path, base: str, name: str, dest: Path) -> Path:
        """A disposable workspace at commit `base`, at `dest / name`; return its path."""
        ...

    def remove(self, path: Path) -> None:
        """Remove a workspace; a no-op if it is already gone."""
        ...

    def prune(self, root: Path) -> None:
        """Forget workspaces of the repo at `root` whose directory is gone."""
        ...

    def list_workspaces(self, root: Path) -> tuple[Path, ...]:
        """Every workspace of the repo at `root`, except the main one."""
        ...

    def commit_all(
        self,
        path: Path,
        message: str,
        paths: Sequence[str] | None = None,
        *,
        include_ignored: Sequence[str] = (),
    ) -> str:
        """Commit the workspace at `path` and return the commit (HEAD if nothing changed).

        `paths=None` commits every change that is not ignored, plus `include_ignored`;
        otherwise exactly `paths` (added, changed or deleted). No hooks run.
        """
        ...

    def diff(self, path: Path, since: str, until: str) -> tuple[FileChange, ...]:
        """The files changed between two commits, sorted by path (no rename detection)."""
        ...

    def patch(self, path: Path, since: str, until: str, paths: Sequence[str]) -> bytes:
        """A self-contained (binary-safe) patch of `paths` between two commits."""
        ...

    def apply_patch(self, path: Path, patch: bytes) -> str | None:
        """Apply `patch` to the files of the workspace at `path`, all or nothing.

        Returns `None` when it applied, else why not (a conflict). Never auto-resolves.
        """
        ...

    def tree_hash(self, path: Path, commit: str) -> str:
        """The tree id of `commit`."""
        ...

    def create_branch(self, root: Path, name: str, commit: str) -> None:
        """Create branch `name` at `commit` in the repo at `root`; fails if it exists."""
        ...


# --- branches and their results ---------------------------------------------------------

FanoutEmit = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class BranchContext:
    """What a branch callable gets: its own workspace and what it may write there."""

    branch_id: str
    run_id: str
    path: Path
    """The workspace root (absolute); valid only while the branch runs."""
    write_set: tuple[str, ...]
    inputs: tuple[str, ...]
    emit: FanoutEmit
    """Journal one event for this branch (`event`, payload)."""

    def file(self, rel: str) -> Path:
        """The workspace path of repo-relative `rel`."""
        return self.path / validate_path(rel)


class BranchOutcome(BaseModel):
    """What a branch callable returns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool = Field(description="Whether the branch did its work.")
    message: str = Field(default="", description="Why not, or a short summary.")
    assumptions: tuple[str, ...] = Field(default=(), description="Assumptions made (F4).")
    assumption_keys: dict[str, str] = Field(
        default_factory=dict,
        description="Assumption key -> assumed value, compared across branches (F4).",
    )
    agent: AgentResult | None = Field(default=None, description="The agent's result, if any.")

    @classmethod
    def from_agent(
        cls,
        result: AgentResult,
        *,
        assumption_keys: Mapping[str, str] | None = None,
        message: str = "",
    ) -> BranchOutcome:
        """An outcome from an `AgentResult`; keys default to its `key=value` assumptions."""
        keys = (
            dict(assumption_keys)
            if assumption_keys is not None
            else keyed_assumptions(result.assumptions)
        )
        return cls(
            ok=result.status == "done",
            message=message or ("" if result.status == "done" else f"agent {result.status}"),
            assumptions=result.assumptions,
            assumption_keys=keys,
            agent=result,
        )


BranchFn = Callable[[BranchContext], Awaitable[BranchOutcome]]
JoinCheck = Callable[[Path], Awaitable[CheckResult]]
"""A join check (F5): runs on the integration workspace root, returns a `CheckResult`."""


@dataclass(frozen=True)
class FanoutBranch:
    """One branch: an id (merge order), the work, the files it reads and may write."""

    id: str
    run: BranchFn
    write_set: tuple[str, ...]
    inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip() or "\0" in self.id:
            raise FanoutError(f"invalid branch id {self.id!r}")
        object.__setattr__(self, "write_set", tuple(validate_path(p) for p in self.write_set))
        object.__setattr__(self, "inputs", tuple(validate_path(p) for p in self.inputs))
        if len(set(self.write_set)) != len(self.write_set):
            raise FanoutError(f"branch {self.id!r} lists a write path twice")


BranchStatus = Literal["done", "failed", "rejected", "timeout", "error"]


class BranchResult(BaseModel):
    """The result of one branch; `patch` is kept so the merge (and a re-run) can use it."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, ser_json_bytes="base64", val_json_bytes="base64"
    )

    id: str
    status: BranchStatus
    message: str = ""
    write_set: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    inputs_hash: str = Field(description="Hash of the seeded inputs, as read from the user's tree.")
    files: tuple[FileChange, ...] = Field(default=(), description="Files the branch changed.")
    outside_writes: tuple[str, ...] = Field(
        default=(), description="Changed files outside the write set (the branch is rejected)."
    )
    assumptions: tuple[str, ...] = ()
    assumption_keys: dict[str, str] = Field(default_factory=dict)
    agent: AgentResult | None = None
    patch: bytes = Field(default=b"", repr=False, description="The branch's changes.")
    duration_s: float = Field(default=0.0, ge=0)
    reused: bool = Field(default=False, description="Kept from a previous run (F7).")

    @property
    def ok(self) -> bool:
        """True when the branch finished and stayed inside its write set."""
        return self.status == "done"


class Conflict(BaseModel):
    """A merge conflict (F3): never resolved automatically."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str | None = Field(default=None, description="The file, when known.")
    branches: tuple[str, ...] = Field(description="The branches involved.")
    message: str


class AssumptionStatement(BaseModel):
    """What one branch assumed for a key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch: str
    value: str


class AssumptionMismatch(BaseModel):
    """Branches that assumed different values for the same key (F4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    statements: tuple[AssumptionStatement, ...]

    def question(self) -> str:
        """The question a person answers."""
        said = "; ".join(f"branch {s.branch!r} assumes {s.value!r}" for s in self.statements)
        return f"The branches disagree on {self.key}: {said}. Which is right?"


FanoutStatus = Literal["done", "failed", "needs_human"]


class FanoutResult(BaseModel):
    """The outcome of a fan-out. Paths are repo-relative; nothing names the machine."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, ser_json_bytes="base64", val_json_bytes="base64"
    )

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    run_id: str
    name: str
    status: FanoutStatus
    message: str = ""
    base: str = Field(description="The commit every workspace started from.")
    branches: tuple[BranchResult, ...] = Field(description="Per branch, in ascending id order.")
    files: tuple[str, ...] = Field(default=(), description="Merged files, sorted.")
    branch: str | None = Field(default=None, description="The result branch, when created.")
    commit: str | None = Field(default=None, description="The merged commit.")
    tree: str | None = Field(default=None, description="The merged commit's tree id.")
    conflicts: tuple[Conflict, ...] = ()
    mismatches: tuple[AssumptionMismatch, ...] = ()
    open_questions: tuple[str, ...] = ()
    join_checks: tuple[CheckResult, ...] = ()
    duration_s: float = Field(default=0.0, ge=0)

    @property
    def ok(self) -> bool:
        """True when every branch merged and the join checks passed."""
        return self.status == "done"

    def branch_result(self, branch_id: str) -> BranchResult | None:
        """The result of `branch_id`, if it was part of this fan-out."""
        return next((b for b in self.branches if b.id == branch_id), None)


@dataclass
class FanoutSession:
    """What `open_fanout` yields: the result, and the integrated tree while it lives."""

    result: FanoutResult
    integration: Path | None
    """The integration workspace (absolute); removed when the context exits."""


# --- pure pieces ------------------------------------------------------------------------


def validate_path(path: str) -> str:
    """`path` if it is a normalised repo-relative POSIX path outside `.git`, else raise."""
    if not isinstance(path, str) or not path:
        raise FanoutError(f"invalid path {path!r}: empty")
    if "\0" in path or "\\" in path:
        raise FanoutError(f"invalid path {path!r}: NUL or backslash")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise FanoutError(f"invalid path {path!r}: absolute")
    for part in path.split("/"):
        if part in ("", ".", ".."):
            raise FanoutError(f"invalid path {path!r}: not a normalised relative path")
        if part.casefold() == ".git":
            raise FanoutError(f"invalid path {path!r}: under .git")
    return path


def check_write_sets(write_sets: Mapping[str, Iterable[str]], shared: Iterable[str] = ()) -> None:
    """F2: raise `FanoutError` if two write sets overlap or one includes a shared file.

    Paths are compared case-insensitively too, since two names that differ only in case
    are the same file on a case-insensitive file system.
    """
    shared_keys = {validate_path(p).casefold(): p for p in shared}
    owners: dict[str, tuple[str, str]] = {}
    for branch_id in sorted(write_sets):
        for path in write_sets[branch_id]:
            key = validate_path(path).casefold()
            if key in shared_keys:
                raise FanoutError(
                    f"branch {branch_id!r} writes {path!r}, a shared file: only a gen rule "
                    "or the one integration task may write it (F2)"
                )
            other = owners.get(key)
            if other is not None and other[0] != branch_id:
                raise FanoutError(
                    f"branches {other[0]!r} and {branch_id!r} both write {path!r} (F2)"
                )
            owners[key] = (branch_id, path)


_KEYED_RE = re.compile(r"^\s*([^\s=]+)\s*=\s*(\S.*?)\s*$")


def keyed_assumptions(statements: Iterable[str]) -> dict[str, str]:
    """The `key=value` statements among `statements`, as key -> value (later wins)."""
    keys: dict[str, str] = {}
    for text in statements:
        match = _KEYED_RE.match(text)
        if match is not None:
            keys[match.group(1)] = match.group(2)
    return keys


def normalize_assumption(text: str) -> str:
    """Case, whitespace and punctuation folded away; a decimal point between digits kept."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = re.sub(r"(?<=\d)\.(?=\d)", "\x00", folded)
    folded = re.sub(r"[^\w\x00]+", " ", folded).replace("\x00", ".")
    return " ".join(folded.split())


def _normalize_key(key: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", key).casefold().split())


def compare_assumptions(
    per_branch: Mapping[str, Mapping[str, str]],
) -> tuple[AssumptionMismatch, ...]:
    """F4: the keys two or more branches assumed different values for (no model needed).

    A key only one branch mentions is fine. Keys and values are compared after
    `normalize_assumption`; the statements are reported as the branches wrote them.
    """
    by_key: dict[str, list[tuple[str, str, str]]] = {}
    for branch_id in sorted(per_branch):
        for key, value in sorted(per_branch[branch_id].items()):
            by_key.setdefault(_normalize_key(key), []).append((branch_id, key, value))
    mismatches: list[AssumptionMismatch] = []
    for norm in sorted(by_key):
        entries = by_key[norm]
        if len({normalize_assumption(value) for _, _, value in entries}) > 1:
            mismatches.append(
                AssumptionMismatch(
                    key=entries[0][1],
                    statements=tuple(AssumptionStatement(branch=b, value=v) for b, _, v in entries),
                )
            )
    return tuple(mismatches)


# --- files: seeding and hashing (read-only on the user's tree) --------------------------


def _within(path: Path, base: Path) -> bool:
    return path.resolve().is_relative_to(base.resolve())


def _input_files(root: Path, rel: str) -> list[str] | None:
    """The repo-relative files `rel` stands for in `root` (a file or a directory).

    `None` when `rel` does not exist. Symlinks count as files and are never followed.
    """
    src = root / rel
    if not _within(src.parent, root):
        raise FanoutError(f"input {rel!r} resolves outside the repo")
    if src.is_symlink() or src.is_file():
        return [rel]
    if not src.is_dir():
        return None
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(src):
        here = Path(dirpath)
        links = [d for d in dirnames if (here / d).is_symlink()]
        dirnames[:] = sorted(d for d in dirnames if d != ".git" and d not in links)
        found.extend((here / name).relative_to(root).as_posix() for name in (*filenames, *links))
    return sorted(found)


def hash_inputs(root: Path, inputs: Sequence[str]) -> str:
    """A hash of the inputs' paths, modes and contents in `root` (missing ones count)."""
    digest = hashlib.sha256()
    for rel in sorted(set(inputs)):
        files = _input_files(root, rel)
        if files is None:
            digest.update(f"{rel}\0missing\0".encode())
            continue
        for item in files:
            path = root / item
            if path.is_symlink():
                content = os.readlink(path).encode()
                kind = "link"
            else:
                content = path.read_bytes()
                kind = "exec" if os.access(path, os.X_OK) else "file"
            digest.update(f"{item}\0{kind}\0".encode())
            digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _clear(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def seed_workspace(root: Path, workspace: Path, inputs: Sequence[str]) -> None:
    """Copy the declared inputs from the user's tree `root` into `workspace`.

    Only reads `root`. Files keep their mode; symlinks are copied as links; an input
    missing in `root` is removed from the workspace, so it sees what the user sees.
    """
    for rel in inputs:
        files = _input_files(root, rel)
        if files is None:
            target = workspace / rel
            if _within(target.parent, workspace):
                _clear(target)
            continue
        for item in files:
            src, dst = root / item, workspace / item
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not _within(dst.parent, workspace):
                raise FanoutError(f"input {item!r} resolves outside the workspace")
            _clear(dst)
            if src.is_symlink():
                os.symlink(os.readlink(src), dst)
            else:
                shutil.copy2(src, dst)


# --- the run directory, its registry, and stale runs ------------------------------------


def fanout_tmp_root(root: Path, layout: StateLayout | None = None) -> Path:
    """Where run directories go: `<state>/tmp/fanout` if outside the repo, else system temp."""
    if layout is not None:
        candidate = layout.tmp_dir / "fanout"
        if not _within(candidate, root):
            return candidate
    return Path(tempfile.gettempdir())


class FanoutRegistry(BaseModel):
    """The registry file of one run directory (machine-local, never committed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    run_id: str
    pid: int
    host: str
    repo: str = Field(description="The resolved repo root the workspaces belong to.")
    started_at: datetime
    workspaces: tuple[str, ...] = ()


def _write_registry(run_dir: Path, registry: FanoutRegistry) -> None:
    path = run_dir / REGISTRY_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_registry(run_dir: Path) -> FanoutRegistry | None:
    try:
        return FanoutRegistry.model_validate_json(
            (run_dir / REGISTRY_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


class StaleRun(BaseModel):
    """A run directory whose owner process is gone (machine-local report)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_dir: str
    run_id: str
    pid: int
    workspaces: tuple[str, ...]


def find_stale(
    root: Path, *, tmp_root: Path | None = None, layout: StateLayout | None = None
) -> tuple[StaleRun, ...]:
    """Run directories of the repo at `root` whose owner pid is dead on this host.

    A run directory without a readable registry is skipped (it may be starting up).
    """
    base = tmp_root if tmp_root is not None else fanout_tmp_root(root, layout)
    if not base.is_dir():
        return ()
    repo = str(root.resolve())
    host = socket.gethostname()
    stale: list[StaleRun] = []
    for run_dir in sorted(base.glob(f"{TMP_PREFIX}*")):
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        registry = _read_registry(run_dir)
        if registry is None or registry.host != host or registry.repo != repo:
            continue
        if _pid_alive(registry.pid):
            continue
        stale.append(
            StaleRun(
                run_dir=str(run_dir),
                run_id=registry.run_id,
                pid=registry.pid,
                workspaces=registry.workspaces,
            )
        )
    return tuple(stale)


def sweep_stale(
    root: Path,
    vcs: FanoutVcs,
    *,
    tmp_root: Path | None = None,
    layout: StateLayout | None = None,
) -> tuple[StaleRun, ...]:
    """Remove the workspaces and run directories `find_stale` reports; prune the repo."""
    stale = find_stale(root, tmp_root=tmp_root, layout=layout)
    if not stale:
        return ()
    try:
        known = vcs.list_workspaces(root)
    except Exception:
        known = ()
    for run in stale:
        run_dir = Path(run.run_dir)
        paths = [Path(p) for p in run.workspaces]
        paths += [p for p in known if _within(p, run_dir) and p not in paths]
        for path in paths:
            with contextlib.suppress(Exception):
                vcs.remove(path)
        shutil.rmtree(run_dir, ignore_errors=True)
    with contextlib.suppress(Exception):
        vcs.prune(root)
    return stale


# --- the run ----------------------------------------------------------------------------


def _noop_emit(_event: str, _payload: dict[str, Any]) -> None:
    return None


def _emitter(ctx: ExecContext | None, run_id: str) -> FanoutEmit:
    """Fan-out events as `tool_call` journal events through the scheduler's context."""
    if ctx is None:
        return _noop_emit
    exec_ctx = ctx

    def emit(event: str, payload: dict[str, Any]) -> None:
        exec_ctx.emit("tool_call", {"tool": "fanout", "event": event, "fanout": run_id, **payload})

    return emit


def _join_error(index: int, exc: BaseException) -> CheckResult:
    message = f"{type(exc).__name__}: {exc}"
    check_id = f"fanout.join[{index}]"
    return CheckResult(
        check_id=check_id,
        status="error",
        issues=(Issue(rule="join.error", msg=message),),
        log_tail=message,
        duration_s=0.0,
        idempotency_key=f"{check_id}:error",
    )


def _order(branches: Sequence[FanoutBranch]) -> tuple[FanoutBranch, ...]:
    seen: set[str] = set()
    for branch in branches:
        if not isinstance(branch, FanoutBranch):
            raise FanoutError(f"not a FanoutBranch: {branch!r}")
        if branch.id in seen:
            raise FanoutError(f"branch id {branch.id!r} is used twice")
        seen.add(branch.id)
    return tuple(sorted(branches, key=lambda b: b.id))


@dataclass
class _Run:
    root: Path
    vcs: FanoutVcs
    branches: tuple[FanoutBranch, ...]
    run_id: str
    name: str
    join_checks: tuple[JoinCheck, ...]
    limit: int
    timeout: float | None
    previous: FanoutResult | None
    run_dir: Path
    emit: FanoutEmit
    make_branch: bool
    started: float = field(default_factory=time.monotonic)
    base: str = ""
    workspaces: list[Path] = field(default_factory=list)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    registry: FanoutRegistry | None = None

    # --- workspaces --------------------------------------------------------------------

    def write_registry(self) -> None:
        self.registry = FanoutRegistry(
            run_id=self.run_id,
            pid=os.getpid(),
            host=socket.gethostname(),
            repo=str(self.root),
            started_at=datetime.now(UTC),
            workspaces=tuple(str(p) for p in self.workspaces),
        )
        _write_registry(self.run_dir, self.registry)

    def workspace(self, name: str, inputs: Sequence[str]) -> Path:
        """A workspace at the base commit with `inputs` copied in (registered first)."""
        self.workspaces.append(self.run_dir / name)
        self.write_registry()  # before it exists: a kill now still leaves a record
        path = self.vcs.create_branch_workspace(self.root, self.base, name, self.run_dir)
        seed_workspace(self.root, path, inputs)
        return path

    def drop(self, path: Path) -> None:
        with contextlib.suppress(Exception):
            self.vcs.remove(path)
        if path.exists() or path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)

    async def stop_tasks(self) -> None:
        pending = [task for task in self.tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            with contextlib.suppress(BaseException):
                await asyncio.gather(*pending, return_exceptions=True)

    def cleanup(self) -> None:
        """Remove every workspace and the run directory; prune the repo. Never raises."""
        for path in self.workspaces:
            self.drop(path)
        shutil.rmtree(self.run_dir, ignore_errors=True)
        with contextlib.suppress(Exception):
            self.vcs.prune(self.root)

    # --- one branch --------------------------------------------------------------------

    def reusable(self, branch: FanoutBranch, inputs_hash: str) -> BranchResult | None:
        """F7: a done result of `branch` from `previous`, if nothing it depends on changed."""
        if self.previous is None or self.previous.base != self.base:
            return None
        old = self.previous.branch_result(branch.id)
        if (
            old is None
            or not old.ok
            or old.write_set != branch.write_set
            or old.inputs != branch.inputs
            or old.inputs_hash != inputs_hash
        ):
            return None
        return old.model_copy(update={"reused": True})

    def branch_emit(self, branch_id: str) -> FanoutEmit:
        def emit(event: str, payload: dict[str, Any]) -> None:
            self.emit(event, {"branch": branch_id, **payload})

        return emit

    async def run_branch(self, index: int, branch: FanoutBranch, inputs_hash: str) -> BranchResult:
        started = time.monotonic()
        common: dict[str, Any] = {
            "id": branch.id,
            "write_set": branch.write_set,
            "inputs": branch.inputs,
            "inputs_hash": inputs_hash,
        }

        def result(status: BranchStatus, message: str, **extra: Any) -> BranchResult:
            done = BranchResult(
                status=status,
                message=message,
                duration_s=time.monotonic() - started,
                **common,
                **extra,
            )
            self.emit(
                "branch_done",
                {"branch": branch.id, "status": status, "message": message},
            )
            return done

        self.emit("branch_start", {"branch": branch.id})
        path: Path | None = None
        try:
            try:
                path = self.workspace(f"b{index:03d}", branch.inputs)
                seed = self.vcs.commit_all(path, f"chipgraph fan-out: seed {branch.id}")
            except Exception as exc:
                return result("error", f"workspace: {type(exc).__name__}: {exc}")

            ctx = BranchContext(
                branch_id=branch.id,
                run_id=self.run_id,
                path=path,
                write_set=branch.write_set,
                inputs=branch.inputs,
                emit=self.branch_emit(branch.id),
            )
            scope = asyncio.timeout(self.timeout)
            try:
                async with scope:
                    outcome = await branch.run(ctx)
            except TimeoutError as exc:
                if scope.expired():
                    return result("timeout", f"stopped after {self.timeout} s")
                return result("error", f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                return result("error", f"{type(exc).__name__}: {exc}")
            if not isinstance(outcome, BranchOutcome):
                return result("error", f"returned {type(outcome).__name__}, not a BranchOutcome")

            try:
                commit = self.vcs.commit_all(
                    path, f"chipgraph fan-out: {branch.id}", include_ignored=branch.write_set
                )
                changes = self.vcs.diff(path, seed, commit)
                allowed = set(branch.write_set)
                outside = tuple(c.path for c in changes if c.path not in allowed)
                patch = (
                    self.vcs.patch(path, seed, commit, [c.path for c in changes])
                    if changes and not outside
                    else b""
                )
            except Exception as exc:
                return result("error", f"collect: {type(exc).__name__}: {exc}")

            extra: dict[str, Any] = {
                "files": changes,
                "outside_writes": outside,
                "assumptions": outcome.assumptions,
                "assumption_keys": dict(outcome.assumption_keys)
                or keyed_assumptions(outcome.assumptions),
                "agent": outcome.agent,
            }
            if outside:
                return result(
                    "rejected",
                    f"changed files outside its write set: {', '.join(outside)}",
                    **extra,
                )
            if not outcome.ok:
                return result("failed", outcome.message or "the branch reported a failure", **extra)
            return result("done", outcome.message, patch=patch, **extra)
        finally:
            if path is not None:
                self.drop(path)

    # --- the whole fan-out ---------------------------------------------------------------

    def finish(
        self, status: FanoutStatus, message: str, branches: tuple[BranchResult, ...], **extra: Any
    ) -> FanoutResult:
        result = FanoutResult(
            run_id=self.run_id,
            name=self.name,
            status=status,
            message=message,
            base=self.base,
            branches=branches,
            duration_s=time.monotonic() - self.started,
            **extra,
        )
        self.emit(
            "done",
            {"status": status, "message": message, "branch_name": result.branch},
        )
        return result

    async def run_branches(self) -> tuple[BranchResult, ...]:
        results: dict[str, BranchResult] = {}
        pending: list[tuple[int, FanoutBranch, str]] = []
        for index, branch in enumerate(self.branches):
            inputs_hash = hash_inputs(self.root, branch.inputs)
            kept = self.reusable(branch, inputs_hash)
            if kept is not None:
                results[branch.id] = kept
                self.emit("branch_kept", {"branch": branch.id})
            else:
                pending.append((index, branch, inputs_hash))

        semaphore = asyncio.Semaphore(self.limit)
        fatal: list[BaseException] = []

        async def guarded(index: int, branch: FanoutBranch, inputs_hash: str) -> None:
            try:
                async with semaphore:
                    results[branch.id] = await self.run_branch(index, branch, inputs_hash)
            except (KeyboardInterrupt, SystemExit) as exc:
                # Raised out of a task, these would leave the event loop at once and skip
                # every `finally`: stop the other branches and re-raise after cleanup.
                fatal.append(exc)
                current = asyncio.current_task()
                for task in self.tasks:
                    if task is not current:
                        task.cancel()

        self.tasks = [asyncio.create_task(guarded(*item)) for item in pending]
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if fatal:
            raise fatal[0]
        for branch in self.branches:
            if branch.id not in results:
                results[branch.id] = BranchResult(
                    id=branch.id,
                    status="error",
                    message="did not finish",
                    write_set=branch.write_set,
                    inputs=branch.inputs,
                    inputs_hash="",
                )
        return tuple(results[b.id] for b in self.branches)

    async def run_join_checks(self, integration: Path) -> tuple[CheckResult, ...]:
        out: list[CheckResult] = []
        for index, check in enumerate(self.join_checks):
            try:
                checked = await check(integration)
            except Exception as exc:
                checked = _join_error(index, exc)
            out.append(checked)
            self.emit("join_check", {"check": checked.check_id, "status": checked.status})
        return tuple(out)

    async def execute(self) -> tuple[FanoutResult, Path | None]:
        self.base = self.vcs.head(self.root)
        self.emit(
            "start",
            {"name": self.name, "base": self.base, "branches": [b.id for b in self.branches]},
        )
        ordered = await self.run_branches()

        mismatches = compare_assumptions({r.id: r.assumption_keys for r in ordered if r.ok})
        bad = [r.id for r in ordered if not r.ok]
        if bad:
            return self.finish(
                "failed",
                f"branch(es) did not finish: {', '.join(bad)} (re-run them with `previous=`)",
                ordered,
                mismatches=mismatches,
            ), None
        if mismatches:
            return self.finish(
                "needs_human",
                "the branches' assumptions differ; a person must decide (F4)",
                ordered,
                mismatches=mismatches,
                open_questions=tuple(m.question() for m in mismatches),
            ), None

        conflicts: list[Conflict] = []
        owners: dict[str, str] = {}
        for res in ordered:
            for change in res.files:
                other = owners.setdefault(change.path.casefold(), res.id)
                if other != res.id:
                    conflicts.append(
                        Conflict(
                            path=change.path,
                            branches=(other, res.id),
                            message=f"{change.path} is changed by {other!r} and {res.id!r}",
                        )
                    )
        if conflicts:
            return self.finish(
                "failed", "merge conflict (F3)", ordered, conflicts=tuple(conflicts)
            ), None

        union = sorted({p for b in self.branches for p in b.inputs})
        integration = self.workspace(JOIN_WORKSPACE, union)
        for res in ordered:  # ascending id order (F7)
            if not res.patch:
                continue
            error = self.vcs.apply_patch(integration, res.patch)
            if error is not None:
                conflict = Conflict(
                    branches=(res.id,),
                    message=f"branch {res.id!r} does not apply onto the integration: {error}",
                )
                return self.finish(
                    "failed", "merge conflict (F3)", ordered, conflicts=(conflict,)
                ), None
        files = tuple(sorted(c.path for r in ordered for c in r.files))
        self.emit("merged", {"files": list(files)})

        checks = await self.run_join_checks(integration)
        failed_checks = [c.check_id for c in checks if not c.ok]
        if failed_checks:
            return self.finish(
                "failed",
                f"join check(s) failed: {', '.join(failed_checks)} (F5); no branch created",
                ordered,
                files=files,
                join_checks=checks,
            ), integration

        listing = "\n".join(f"- {r.id}" for r in ordered)
        message = (
            f"chipgraph fan-out {self.name} ({self.run_id})\n\n"
            f"Branches, in merge order:\n{listing}\n"
        )
        commit = self.vcs.commit_all(integration, message, paths=files)
        tree = self.vcs.tree_hash(integration, commit)
        branch_name: str | None = None
        if self.make_branch:
            branch_name = f"chipgraph/{self.run_id}/{self.name}"
            self.vcs.create_branch(self.root, branch_name, commit)
        return self.finish(
            "done",
            f"{len(ordered)} branch(es) merged",
            ordered,
            files=files,
            branch=branch_name,
            commit=commit,
            tree=tree,
            join_checks=checks,
        ), integration


@contextlib.asynccontextmanager
async def open_fanout(
    branches: Sequence[FanoutBranch],
    *,
    root: Path,
    vcs: FanoutVcs,
    name: str = "fanout",
    run_id: str | None = None,
    shared: Iterable[str] = (),
    join_checks: Sequence[JoinCheck] = (),
    max_parallel: int | None = None,
    branch_timeout_s: float | None = None,
    previous: FanoutResult | None = None,
    tmp_root: Path | None = None,
    layout: StateLayout | None = None,
    ctx: ExecContext | None = None,
    make_branch: bool = True,
) -> AsyncIterator[FanoutSession]:
    """Run a fan-out and yield its result with the integrated tree, until the context exits.

    `root` is the user's repo (only read, plus the result branch). `shared` lists files
    no branch may write (F2). `max_parallel` defaults to `min(4, cpu count)`; profiles set
    it as `fanout.max_parallel` (`FanoutCfg`). `previous` keeps the done branches of an
    earlier result (F7). `tmp_root` overrides where the run directory goes (it must be
    outside the working tree). `ctx` journals `tool_call` events. With `make_branch`
    false the merged commit is made but no branch points at it.

    Raises `FanoutError` before anything starts for an F2 overlap, bad paths, ids or
    limits. Every workspace and the run directory are removed when the context exits,
    whatever the reason (an exception, a cancel, KeyboardInterrupt).
    """
    repo = root.resolve()
    rid = run_id if run_id is not None else new_run_id()
    if not _RUN_ID_RE.match(rid):
        raise FanoutError(f"invalid run id {rid!r}")
    if not _NAME_RE.match(name):
        raise FanoutError(f"invalid fan-out name {name!r}: must match {_NAME_RE.pattern!r}")
    ordered = _order(branches)
    check_write_sets({b.id: b.write_set for b in ordered}, shared)
    limit = max_parallel if max_parallel is not None else FanoutCfg().effective_max_parallel()
    if limit < 1:
        raise FanoutError(f"max_parallel must be >= 1, got {limit}")
    if branch_timeout_s is not None and branch_timeout_s <= 0:
        raise FanoutError(f"branch_timeout_s must be > 0, got {branch_timeout_s}")
    base_root = tmp_root if tmp_root is not None else fanout_tmp_root(repo, layout)
    if _within(base_root, repo):
        raise FanoutError(f"the fan-out directory {base_root} is inside the working tree")

    sweep_stale(repo, vcs, tmp_root=base_root)
    base_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{TMP_PREFIX}{rid}-", dir=base_root)).resolve()
    run = _Run(
        root=repo,
        vcs=vcs,
        branches=ordered,
        run_id=rid,
        name=name,
        join_checks=tuple(join_checks),
        limit=limit,
        timeout=branch_timeout_s,
        previous=previous,
        run_dir=run_dir,
        emit=_emitter(ctx, rid),
        make_branch=make_branch,
    )
    try:
        run.write_registry()
        result, integration = await run.execute()
        yield FanoutSession(result=result, integration=integration)
    finally:
        await run.stop_tasks()
        run.cleanup()


async def run_fanout(
    branches: Sequence[FanoutBranch],
    *,
    root: Path,
    vcs: FanoutVcs,
    name: str = "fanout",
    run_id: str | None = None,
    shared: Iterable[str] = (),
    join_checks: Sequence[JoinCheck] = (),
    max_parallel: int | None = None,
    branch_timeout_s: float | None = None,
    previous: FanoutResult | None = None,
    tmp_root: Path | None = None,
    layout: StateLayout | None = None,
    ctx: ExecContext | None = None,
    make_branch: bool = True,
) -> FanoutResult:
    """`open_fanout` without the integrated tree: run, clean up, return the result."""
    async with open_fanout(
        branches,
        root=root,
        vcs=vcs,
        name=name,
        run_id=run_id,
        shared=shared,
        join_checks=join_checks,
        max_parallel=max_parallel,
        branch_timeout_s=branch_timeout_s,
        previous=previous,
        tmp_root=tmp_root,
        layout=layout,
        ctx=ctx,
        make_branch=make_branch,
    ) as session:
        return session.result


__all__ = [
    "JOIN_WORKSPACE",
    "REGISTRY_FILE",
    "TMP_PREFIX",
    "AssumptionMismatch",
    "AssumptionStatement",
    "BranchContext",
    "BranchFn",
    "BranchOutcome",
    "BranchResult",
    "BranchStatus",
    "Conflict",
    "FanoutBranch",
    "FanoutEmit",
    "FanoutError",
    "FanoutRegistry",
    "FanoutResult",
    "FanoutSession",
    "FanoutStatus",
    "FanoutVcs",
    "FileChange",
    "FileStatus",
    "JoinCheck",
    "StaleRun",
    "check_write_sets",
    "compare_assumptions",
    "fanout_tmp_root",
    "find_stale",
    "hash_inputs",
    "keyed_assumptions",
    "normalize_assumption",
    "open_fanout",
    "run_fanout",
    "seed_workspace",
    "sweep_stale",
    "validate_path",
]
