"""Outputs moved aside for authors without a read tool, and put back when a task fails.

Claude Code's `Write` refuses to overwrite a file the agent has not read first ("File has
not been read yet"). A role with no `read_files` capability (`tb-author`, `rtl-author`:
no Read, on purpose) could therefore never rewrite its own rejected output, nor replace
a file that existed before its task. So at every dispatch of such a role, `next_task`
moves each of the task's outputs that exists out of the working tree into the state
backend: the next `Write` creates a new file, which needs no prior Read. `get_context`
shows the author what was moved (`previous_outputs`), and a file that existed before
the task is put back, byte for byte and with its mode, when the task ends without being
accepted. Roles with a Read tool are not touched: they edit in place.

On disk, per task (the key is the task id's sha256, as in the queue)::

    <state>/runtime/previous/<key>/cycle.json        the budget cycle: since which
                                                     dispatch, the files that existed
                                                     before it (`before_task`), the
                                                     last attempt's file per path
    <state>/runtime/previous/<key>/d<N>.json         what dispatch N moved: path,
                                                     origin, sha256, mode, size
    <state>/runtime/previous/<key>/d<N>/<path>       a moved file, byte for byte
    <state>/runtime/previous/<key>/e<N>.json         at an ending after dispatch N:
    <state>/runtime/previous/<key>/e<N>/<path>       the last attempt a restore replaced

A slot name already taken (a crash after a move, before the dispatch was recorded) gets
a suffix: `d<N>.2`, `d<N>.3`. Nothing under `previous/` is ever deleted: a person can
read every version. A cycle opens at the first dispatch after the last ending and
closes when the task is accepted (the new files stay), stops (`budget_exhausted`,
`needs_human`: the `before_task` files are restored), is rewound while not dispatched
(restored), or when the dispatch that moved the files fails in the engine (every moved
file goes back as it was).

Only the task's own outputs (`allowed_writes`) are ever moved, written or restored; a
path that is absolute, climbs out of the project, or is a symlink is left alone.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from chipgraph.core.runtime import TaskQueue
from chipgraph.core.runtime.queue import state_key
from chipgraph.core.runtime.roles import RoleSpec

MAX_FILE_CHARS = 20_000
"""Longest text of one moved file `get_context` shows; longer ones are cut."""

MAX_TOTAL_CHARS = 60_000
"""Most text of moved files `get_context` shows for one task, over every file."""

Origin = Literal["before_task", "attempt"]
"""Where a moved file came from: it existed before the task's cycle began, or an
earlier attempt of the task wrote it."""

Ending = Literal["accepted", "restored", "undone"]
"""How a cycle closed: the task was accepted (the new files stay), it ended without
being accepted (the `before_task` files were put back), or the dispatch that opened it
failed in the engine (every moved file was put back)."""


def _now() -> datetime:
    return datetime.now(UTC)


def moves_outputs(role: RoleSpec | None) -> bool:
    """Whether a role's outputs are moved aside at dispatch: it writes files and has no
    `read_files` capability (its harness cannot overwrite a file it did not read)."""
    return role is not None and "write_outputs" in role.tools and "read_files" not in role.tools


class MovedFile(BaseModel):
    """One file moved out of the working tree (or saved before a restore replaced it)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    path: str = Field(min_length=1, description="Repo-relative path of the file.")
    origin: Origin = Field(description="'before_task' or 'attempt' (see `Origin`).")
    slot: str = Field(min_length=1, description="The slot directory holding its bytes.")
    sha256: str = Field(min_length=64, max_length=64, description="Content hash.")
    mode: int = Field(ge=0, description="Permission bits (`stat.S_IMODE`).")
    size: int = Field(ge=0, description="Size in bytes.")


class SlotRecord(BaseModel):
    """`d<N>.json` or `e<N>.json`: the files one dispatch moved, or one ending saved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    task_id: str = Field(min_length=1, description="The task the files belong to.")
    dispatch: int = Field(ge=0, description="The task's dispatch count it was taken at.")
    kind: Literal["dispatch", "ending"] = Field(description="Moved at dispatch, or at an end.")
    at: AwareDatetime = Field(default_factory=_now, description="When.")
    files: tuple[MovedFile, ...] = Field(default=(), description="The files.")


class Cycle(BaseModel):
    """`cycle.json`: the files moved in the task's current (or last) budget cycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    task_id: str = Field(min_length=1, description="The task.")
    since: int = Field(ge=1, description="The dispatch that opened the cycle.")
    originals: tuple[MovedFile, ...] = Field(
        default=(), description="Files that existed before the cycle (`before_task`)."
    )
    last_attempts: tuple[MovedFile, ...] = Field(
        default=(), description="Per path, the last attempt's file that was moved."
    )
    ended: Ending | None = Field(default=None, description="How it closed; None while open.")
    ended_at: AwareDatetime | None = Field(default=None, description="When it closed.")

    @property
    def open(self) -> bool:
        return self.ended is None


class MoveToken(BaseModel):
    """What `PreviousOutputs.undo` needs to reverse one `move_aside`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    before: Cycle | None = Field(default=None, description="The cycle before the move.")
    moved: tuple[MovedFile, ...] = Field(default=(), description="The files it moved.")


def _safe_rel(rel: str) -> bool:
    """A repo-relative path with no `..`, not absolute, not empty."""
    pure = PurePosixPath(rel)
    return (
        bool(rel) and not pure.is_absolute() and ".." not in pure.parts and rel == pure.as_posix()
    )


def _write_bytes_atomic(path: Path, data: bytes, mode: int | None = None) -> None:
    """Write `data` to `path` through a temporary file next to it, then set `mode`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.chipgraph-tmp")
    try:
        tmp.write_bytes(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_json(path: Path, model: BaseModel) -> None:
    _write_bytes_atomic(path, (model.model_dump_json(indent=2) + "\n").encode("utf-8"))


class PreviousOutputs:
    """The moved-aside outputs of one task (see the module docstring)."""

    def __init__(self, queue: TaskQueue, task_id: str, root: Path | None = None) -> None:
        self.task_id = task_id
        self.root = (root or queue.layout.root).resolve()
        self.dir = queue.runtime_dir / "previous" / state_key(task_id)

    # --- reading -------------------------------------------------------------------

    def cycle(self) -> Cycle | None:
        """The task's current or last cycle, or None if nothing was ever moved for it."""
        path = self.dir / "cycle.json"
        if not path.is_file():
            return None
        return Cycle.model_validate_json(path.read_text(encoding="utf-8"))

    def open_cycle(self) -> Cycle | None:
        """The task's open cycle, or None."""
        cycle = self.cycle()
        return cycle if cycle is not None and cycle.open else None

    def slot_file(self, moved: MovedFile) -> Path:
        """Where the bytes of `moved` are kept."""
        return self.dir / moved.slot / moved.path

    def read_text(self, moved: MovedFile) -> str:
        """The text of a moved file (undecodable bytes replaced)."""
        return self.slot_file(moved).read_bytes().decode("utf-8", errors="replace")

    # --- helpers -------------------------------------------------------------------

    def _tree_path(self, rel: str) -> Path | None:
        """The working-tree path of an output, or None if it must not be touched."""
        if not _safe_rel(rel):
            return None
        full = self.root / rel
        try:
            full.parent.resolve().relative_to(self.root)
        except (OSError, ValueError):
            return None
        return full

    def _new_slot(self, prefix: str, dispatch: int) -> str:
        name = f"{prefix}{dispatch}"
        k = 1
        while (self.dir / f"{name}.json").exists() or (self.dir / name).exists():
            k += 1
            name = f"{prefix}{dispatch}.{k}"
        return name

    def _save(self, full: Path, rel: str, slot: str, origin: Origin) -> MovedFile:
        """Copy a working-tree file into `slot`, checked against its hash."""
        data = full.read_bytes()
        mode = stat.S_IMODE(full.stat().st_mode)
        digest = hashlib.sha256(data).hexdigest()
        target = self.dir / slot / rel
        _write_bytes_atomic(target, data)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:  # pragma: no cover
            raise OSError(f"copy of {rel} in {target} does not match the original")
        return MovedFile(
            path=rel, origin=origin, slot=slot, sha256=digest, mode=mode, size=len(data)
        )

    def _put_back(self, moved: MovedFile) -> None:
        """Write a kept file back to the working tree, byte for byte, with its mode."""
        full = self._tree_path(moved.path)
        if full is None:  # pragma: no cover - never moved, then
            return
        data = self.slot_file(moved).read_bytes()
        if hashlib.sha256(data).hexdigest() != moved.sha256:
            raise OSError(f"the kept copy of {moved.path} ({self.slot_file(moved)}) is damaged")
        _write_bytes_atomic(full, data, moved.mode)

    def _close(self, cycle: Cycle, ending: Ending) -> None:
        _write_json(
            self.dir / "cycle.json", cycle.model_copy(update={"ended": ending, "ended_at": _now()})
        )

    # --- dispatch ------------------------------------------------------------------

    def move_aside(self, outputs: Sequence[str], *, dispatch: int) -> MoveToken:
        """Move each of `outputs` that is a file out of the tree, for dispatch `dispatch`.

        Opens a cycle when none is open (the files found are `before_task`); otherwise
        the files found are earlier attempts'. Every file is first copied into the state
        backend and checked against its hash, the records are written, and only then
        are the files removed. On any error the files already removed go back and the
        cycle is as before. Returns what `undo` needs.
        """
        before = self.cycle()
        origin: Origin
        if before is not None and before.open:
            cycle, origin = before, "attempt"
        else:
            cycle, origin = Cycle(task_id=self.task_id, since=dispatch), "before_task"
        slot = self._new_slot("d", dispatch)
        moved: list[MovedFile] = []
        removed: list[MovedFile] = []
        try:
            found: list[tuple[Path, str]] = []
            for rel in dict.fromkeys(outputs):
                full = self._tree_path(rel)
                if full is not None and full.is_file() and not full.is_symlink():
                    found.append((full, rel))
            moved = [self._save(full, rel, slot, origin) for full, rel in found]
            _write_json(
                self.dir / f"{slot}.json",
                SlotRecord(
                    task_id=self.task_id, dispatch=dispatch, kind="dispatch", files=tuple(moved)
                ),
            )
            if origin == "before_task":
                cycle = cycle.model_copy(update={"originals": tuple(moved)})
            else:
                paths = {m.path for m in moved}
                last = tuple(m for m in cycle.last_attempts if m.path not in paths)
                cycle = cycle.model_copy(update={"last_attempts": (*last, *moved)})
            _write_json(self.dir / "cycle.json", cycle)
            for (full, _), kept in zip(found, moved, strict=True):
                full.unlink()
                removed.append(kept)
        except BaseException:
            for kept in removed:
                self._put_back(kept)
            if before is not None:
                _write_json(self.dir / "cycle.json", before)
            else:
                (self.dir / "cycle.json").unlink(missing_ok=True)
            raise
        return MoveToken(before=before, moved=tuple(moved))

    def undo(self, token: MoveToken) -> None:
        """The dispatch that moved `token`'s files failed: put them all back as they were.

        The copies stay in the state backend; the cycle is as it was before the move
        (a cycle this move opened is closed as `undone`).
        """
        for kept in token.moved:
            self._put_back(kept)
        if token.before is not None:
            _write_json(self.dir / "cycle.json", token.before)
        else:
            cycle = self.cycle()
            if cycle is not None:
                self._close(cycle, "undone")

    # --- endings -------------------------------------------------------------------

    def accept(self) -> None:
        """The task was accepted: close the cycle; the new files stay."""
        cycle = self.open_cycle()
        if cycle is not None:
            self._close(cycle, "accepted")

    def restore(self, *, dispatch: int) -> list[str]:
        """The task ended without being accepted: put every `before_task` file back.

        A working-tree file the restore replaces (the last attempt's) is kept first, in
        an `e<dispatch>` slot. Returns the paths restored. A no-op without an open cycle.
        """
        cycle = self.open_cycle()
        if cycle is None:
            return []
        replaced: list[MovedFile] = []
        slot = self._new_slot("e", dispatch)
        restored: list[str] = []
        for original in cycle.originals:
            full = self._tree_path(original.path)
            if full is None:  # pragma: no cover - never moved, then
                continue
            if (
                full.is_file()
                and not full.is_symlink()
                and hashlib.sha256(full.read_bytes()).hexdigest() != original.sha256
            ):
                replaced.append(self._save(full, original.path, slot, "attempt"))
            self._put_back(original)
            restored.append(original.path)
        if replaced:
            _write_json(
                self.dir / f"{slot}.json",
                SlotRecord(
                    task_id=self.task_id, dispatch=dispatch, kind="ending", files=tuple(replaced)
                ),
            )
        self._close(cycle, "restored")
        return restored

    # --- the context ---------------------------------------------------------------

    def context(
        self, outputs: Iterable[str], denied: Callable[[str], bool]
    ) -> tuple[list[dict[str, object]], list[str]]:
        """`get_context`'s `previous_outputs`, and the paths withheld from the role.

        Per output of the open cycle: the last attempt's text (`content`) and, for a
        file that existed before the task, its text (`existing_output`), each capped
        (`MAX_FILE_CHARS`, `MAX_TOTAL_CHARS` over all, `truncated` when cut). A path
        `denied` for the role is listed without any text.
        """
        cycle = self.open_cycle()
        if cycle is None:
            return [], []
        attempts = {m.path: m for m in cycle.last_attempts}
        originals = {m.path: m for m in cycle.originals}
        budget = MAX_TOTAL_CHARS
        entries: list[dict[str, object]] = []
        withheld: list[str] = []

        def text_of(moved: MovedFile) -> dict[str, object]:
            nonlocal budget
            text = self.read_text(moved)
            cap = max(min(MAX_FILE_CHARS, budget), 0)
            cut = len(text) > cap
            text = text[:cap]
            budget -= len(text)
            return {"content": text, "truncated": cut}

        for rel in dict.fromkeys(outputs):
            attempt, original = attempts.get(rel), originals.get(rel)
            if attempt is None and original is None:
                continue
            if denied(rel):
                withheld.append(rel)
                entries.append(
                    {
                        "path": rel,
                        "withheld": (
                            "a file of a kind your role must not see; its text is not shown"
                        ),
                    }
                )
                continue
            entry: dict[str, object] = {"path": rel}
            if attempt is not None:
                entry["label"] = "your previous attempt (moved aside; write the file anew)"
                entry.update(text_of(attempt))
            if original is not None:
                entry["existing_output"] = {
                    "label": "the file as it was before this task (put back if it fails)",
                    **text_of(original),
                }
            entries.append(entry)
        return entries, withheld


__all__ = [
    "MAX_FILE_CHARS",
    "MAX_TOTAL_CHARS",
    "Cycle",
    "MoveToken",
    "MovedFile",
    "PreviousOutputs",
    "SlotRecord",
    "moves_outputs",
]
