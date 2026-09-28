"""Tests for Event and RunManifest."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from chipgraph.core.contracts import Event, RunManifest

SHA = "e" * 64


def test_event_round_trip() -> None:
    event = Event(
        run_id="run-1",
        seq=0,
        ts=datetime.now(UTC),
        type="rule_start",
        rule_instance="digital-rtl/rtl_module[block=timer]",
        payload={"note": "starting"},
    )
    assert Event.model_validate_json(event.model_dump_json()) == event


def test_event_failure_label_allowed_on_rule_fail() -> None:
    event = Event(
        run_id="run-1", seq=1, ts=datetime.now(UTC), type="rule_fail", failure_label="verification"
    )
    assert event.failure_label == "verification"


def test_event_failure_label_rejected_when_not_rule_fail() -> None:
    with pytest.raises(ValidationError):
        Event(
            run_id="run-1",
            seq=1,
            ts=datetime.now(UTC),
            type="rule_done",
            failure_label="verification",
        )


def test_event_seq_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        Event(run_id="run-1", seq=-1, ts=datetime.now(UTC), type="run_start")


def test_run_manifest_round_trip() -> None:
    manifest = RunManifest(
        run_id="run-1",
        started_at=datetime.now(UTC),
        target="check:all",
        chipgraph_version="0.0.1",
        profile_hash=SHA,
        profile_sources=(".chipgraph.yml@abc123",),
        packs={"digital-rtl": "1.0.0"},
    )
    assert RunManifest.model_validate_json(manifest.model_dump_json()) == manifest
