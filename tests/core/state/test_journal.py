"""Tests for `chipgraph.core.state.journal`."""

from __future__ import annotations

import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chipgraph.core.contracts.event import Event, RunManifest
from chipgraph.core.state.journal import (
    Journal,
    JournalError,
    read,
    read_manifest,
    replay,
    write_manifest,
)
from chipgraph.core.state.layout import StateLayout

RUN_ID = "run-1"
SHA = "e" * 64


def make_event(seq: int, type_: str = "rule_start", **kwargs: object) -> Event:
    return Event(run_id=RUN_ID, seq=seq, ts=datetime.now(UTC), type=type_, **kwargs)  # type: ignore[arg-type]


def test_append_and_read_round_trip(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.jsonl")
    e0 = make_event(0, type_="run_start")
    e1 = make_event(1, type_="rule_start", rule_instance="pack/rule[block=timer]")
    journal.append(e0)
    journal.append(e1)

    result = read(journal.path)
    assert result.truncated is False
    assert result.events == (e0, e1)


def test_next_seq_starts_at_zero(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.jsonl")
    assert journal.next_seq() == 0
    journal.append(make_event(0))
    assert journal.next_seq() == 1


def test_append_rejects_seq_gap(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.jsonl")
    journal.append(make_event(0))
    with pytest.raises(JournalError):
        journal.append(make_event(2))


def test_append_rejects_run_id_mismatch(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.jsonl")
    journal.append(make_event(0))
    other_run_event = Event(run_id="other-run", seq=1, ts=datetime.now(UTC), type="rule_start")
    with pytest.raises(JournalError):
        journal.append(other_run_event)


def test_read_invalid_middle_line_raises_with_line_number(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    e0 = make_event(0)
    e2 = make_event(2)
    path.write_text(e0.model_dump_json() + "\nnot valid json\n" + e2.model_dump_json() + "\n")
    with pytest.raises(JournalError, match="line 2"):
        read(path)


def _spawn_crash_writer(path: Path) -> int:
    """Spawn a subprocess that writes complete events, then a partial line, then dies."""
    script = textwrap.dedent(
        f"""
        import json
        from datetime import UTC, datetime

        from chipgraph.core.contracts.event import Event

        path = {str(path)!r}
        events = [
            Event(run_id={RUN_ID!r}, seq=i, ts=datetime.now(UTC), type="rule_start")
            for i in range(3)
        ]
        with open(path, "a", encoding="utf-8") as f:
            for e in events:
                f.write(e.model_dump_json() + "\\n")
                f.flush()
            # Now write a partial, truncated line and die without a trailing newline.
            f.write('{{"schema_version": 1, "run_id": "run-1", "seq": 3, "trunc')
            f.flush()
        import os

        os._exit(1)
        """
    )
    import subprocess

    proc = subprocess.run([sys.executable, "-c", script], check=False)
    return proc.returncode


def test_crash_mid_write_truncated_and_repaired(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    _spawn_crash_writer(path)

    result = read(path)
    assert result.truncated is True
    assert len(result.events) == 3
    assert [e.seq for e in result.events] == [0, 1, 2]

    # A fresh Journal repairs the file and can append with the correct next seq.
    journal = Journal(path)
    assert journal.next_seq() == 3
    journal.append(make_event(3))

    result2 = read(path)
    assert result2.truncated is False
    assert [e.seq for e in result2.events] == [0, 1, 2, 3]


def test_replay_realistic_sequence() -> None:
    ts = datetime.now(UTC)
    events = [
        Event(run_id=RUN_ID, seq=0, ts=ts, type="run_start"),
        Event(run_id=RUN_ID, seq=1, ts=ts, type="rule_start", rule_instance="a"),
        Event(run_id=RUN_ID, seq=2, ts=ts, type="rule_start", rule_instance="b"),
        Event(run_id=RUN_ID, seq=3, ts=ts, type="rule_done", rule_instance="a"),
        Event(
            run_id=RUN_ID, seq=4, ts=ts, type="rule_fail", rule_instance="b", failure_label="infra"
        ),
        Event(run_id=RUN_ID, seq=5, ts=ts, type="rule_start", rule_instance="c"),
        Event(run_id=RUN_ID, seq=6, ts=ts, type="gate_wait", rule_instance="c"),
        Event(run_id=RUN_ID, seq=7, ts=ts, type="gate_decision", rule_instance="c"),
        Event(run_id=RUN_ID, seq=8, ts=ts, type="rule_done", rule_instance="d"),
        Event(run_id=RUN_ID, seq=9, ts=ts, type="tool_call"),
        Event(run_id=RUN_ID, seq=10, ts=ts, type="run_stop"),
    ]
    state = replay(events)
    assert state.run_id == RUN_ID
    assert state.started is True
    assert state.stopped is True
    assert state.last_seq == 10
    assert state.rules == {
        "a": "done",
        "b": "failed",
        "c": "pending",
        "d": "done",
    }
    assert state.failures == {"b": "infra"}


def test_manifest_round_trip(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    manifest = RunManifest(
        run_id=RUN_ID,
        started_at=datetime.now(UTC),
        target="check:all",
        chipgraph_version="0.0.1",
        profile_hash=SHA,
    )
    write_manifest(layout, manifest)
    loaded = read_manifest(layout, RUN_ID)
    assert loaded == manifest

    # Atomic: no leftover tmp file.
    assert not layout.manifest(RUN_ID).with_suffix(".json.tmp").exists()
