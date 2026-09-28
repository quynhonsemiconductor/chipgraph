"""The run journal: an append-only JSONL log of `Event`s, and the state replayed from it.

DESIGN.md 6.2: "Journal chi ghi them. State duoc dung lai tu journal cong voi hash cua
artifact." (The journal is append-only. State is rebuilt from the journal plus artifact
hashes.) This module implements the journal itself and replay into a `RunState`; it does
not know about artifact hashes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.contracts.event import Event, RunManifest
from chipgraph.core.contracts.types import FailureLabel
from chipgraph.core.state.layout import StateLayout

RuleStatus = Literal["pending", "running", "done", "failed", "waiting_gate"]
"""The status of a single rule instance, as derived from the journal."""

_EVENTS_WITHOUT_RULE_INSTANCE = frozenset({"run_start", "run_stop", "tool_call"})


class JournalError(Exception):
    """The journal is inconsistent: a bad sequence number, run id, or a corrupt line."""


class RunState(BaseModel):
    """The state of a run, rebuilt by replaying its journal."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    started: bool = False
    stopped: bool = False
    rules: dict[str, RuleStatus] = Field(default_factory=dict)
    failures: dict[str, FailureLabel | None] = Field(default_factory=dict)
    last_seq: int = -1


class JournalRead(BaseModel):
    """The result of reading a journal file: the events found, and whether it was truncated."""

    model_config = ConfigDict(frozen=True)

    events: tuple[Event, ...] = ()
    truncated: bool = False


class Journal:
    """An append-only JSONL journal of `Event`s for a single run.

    Each line is one `Event`, written with `model_dump_json()`. `append` fsyncs after every
    write, so a killed process loses at most the in-flight line. On construction, `repair()`
    removes any trailing partial line left by a previous crash, so the next `append` sees a
    clean, complete file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._run_id: str | None = None
        self._last_seq: int = -1
        self.repair()
        self._load_tail_state()

    def _load_tail_state(self) -> None:
        result = read(self.path)
        if result.events:
            self._run_id = result.events[-1].run_id
            self._last_seq = result.events[-1].seq

    def repair(self) -> None:
        """Truncate the file to the end of its last complete, valid JSON line, if any.

        This undoes a crash mid-write: a missing trailing newline, or a last line that is
        not valid JSON. Complete lines before that point are untouched.
        """
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        if not raw:
            return
        lines = raw.split(b"\n")
        # split(b"\n") on a file ending in "\n" yields a trailing empty element.
        if lines and lines[-1] == b"":
            lines.pop()
        if not lines:
            return
        last = lines[-1]
        ends_with_newline = raw.endswith(b"\n")
        valid_last = False
        if ends_with_newline:
            try:
                json.loads(last)
                valid_last = True
            except json.JSONDecodeError:
                valid_last = False
        if valid_last:
            return
        # Truncate off the partial trailing line, keeping all prior complete lines.
        good_len = sum(len(line) + 1 for line in lines[:-1])
        with open(self.path, "r+b") as f:
            f.truncate(good_len)
            f.flush()
            os.fsync(f.fileno())

    def next_seq(self) -> int:
        """The `seq` value the next appended event must use."""
        return self._last_seq + 1

    def append(self, event: Event) -> None:
        """Append one event, enforcing strictly increasing `seq` and a stable `run_id`."""
        if self._run_id is not None and event.run_id != self._run_id:
            raise JournalError(
                f"journal {self.path}: run_id mismatch: expected {self._run_id!r}, "
                f"got {event.run_id!r}"
            )
        expected_seq = self.next_seq()
        if event.seq != expected_seq:
            raise JournalError(f"journal {self.path}: expected seq {expected_seq}, got {event.seq}")
        line = event.model_dump_json()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._run_id = event.run_id
        self._last_seq = event.seq


def read(path: Path) -> JournalRead:
    """Read every complete event line from a journal file.

    A truncated last line (no trailing newline, or invalid JSON, on the final line only)
    is ignored and reported via `JournalRead.truncated`. An invalid line that is not the
    last one raises `JournalError` naming the 1-based line number.
    """
    if not path.exists():
        return JournalRead()
    raw = path.read_text(encoding="utf-8")
    if raw == "":
        return JournalRead()
    ends_with_newline = raw.endswith("\n")
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return JournalRead()

    events: list[Event] = []
    truncated = False
    n = len(lines)
    for i, line in enumerate(lines):
        line_no = i + 1
        is_last = i == n - 1
        try:
            event = Event.model_validate(json.loads(line))
        except Exception as exc:
            if is_last:
                truncated = True
                break
            raise JournalError(f"journal {path}: invalid line at line {line_no}") from exc
        if is_last and not ends_with_newline:
            # Valid JSON, but the file was cut off before its trailing newline: treat as
            # a partial write and drop it too, so repair() and append() agree on the cut.
            truncated = True
            break
        events.append(event)
    return JournalRead(events=tuple(events), truncated=truncated)


def replay(events: list[Event] | tuple[Event, ...]) -> RunState:
    """Rebuild a `RunState` by folding a sequence of events in order."""
    run_id = ""
    started = False
    stopped = False
    rules: dict[str, RuleStatus] = {}
    failures: dict[str, FailureLabel | None] = {}
    last_seq = -1

    for event in events:
        run_id = event.run_id
        last_seq = event.seq

        if event.type == "run_start":
            started = True
            continue
        if event.type == "run_stop":
            stopped = True
            continue
        if event.type == "tool_call":
            continue

        rule_instance = event.rule_instance
        if rule_instance is None:
            # Lenient: ignore events that should carry a rule_instance but don't.
            continue

        if event.type == "rule_start":
            rules[rule_instance] = "running"
        elif event.type == "rule_done":
            rules[rule_instance] = "done"
        elif event.type == "rule_fail":
            rules[rule_instance] = "failed"
            failures[rule_instance] = event.failure_label
        elif event.type == "gate_wait":
            rules[rule_instance] = "waiting_gate"
        elif event.type == "gate_decision":
            rules[rule_instance] = "pending"
        # check_result, agent_turn: recorded in the journal but do not change rule status.

    return RunState(
        run_id=run_id,
        started=started,
        stopped=stopped,
        rules=rules,
        failures=failures,
        last_seq=last_seq,
    )


def write_manifest(layout: StateLayout, manifest: RunManifest) -> None:
    """Atomically write a run's manifest: write to a tmp file, then `os.replace`."""
    path = layout.manifest(manifest.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def read_manifest(layout: StateLayout, run_id: str) -> RunManifest:
    """Read a run's manifest."""
    path = layout.manifest(run_id)
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
