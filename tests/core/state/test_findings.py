"""Tests for `chipgraph.core.state.findings`: `FindingStore` and `effective_status`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from chipgraph.core.contracts import Approval, ArtifactRef, Evidence, Finding
from chipgraph.core.state.findings import FindingStore, effective_status, waiver_gate_id
from chipgraph.core.state.layout import StateLayout

SHA_A = "a" * 64
SHA_B = "b" * 64
NOW = datetime.now(UTC)


def _finding(**overrides: object) -> Finding:
    ref = ArtifactRef(kind="rtl", path="design/timer/rtl/m_cnt.sv")
    kwargs: dict[str, object] = dict(
        layer=1,
        severity="error",
        source="check:cross_chip",
        evidence=(Evidence(file="design/timer/rtl/m_cnt.sv", line=12),),
        claim="width mismatch",
        artifacts=(ref,),
        artifact_hashes={".:design/timer/rtl/m_cnt.sv": SHA_A},
        first_seen=NOW,
        last_seen=NOW,
    )
    kwargs.update(overrides)
    return Finding(**kwargs)  # type: ignore[arg-type]


def _approval(finding: Finding, *, hashes: dict[str, str] | None = None) -> Approval:
    return Approval(
        gate_id=waiver_gate_id(finding.id),
        by="nghiavt",
        at=datetime.now(UTC),
        artifact_hashes=hashes or dict(finding.artifact_hashes),
        decision="waive",
        note="known issue, tracked in JIRA-42",
    )


def test_upsert_keeps_first_seen(tmp_path: Path) -> None:
    store = FindingStore(StateLayout(tmp_path))
    first = _finding(first_seen=NOW, last_seen=NOW)
    (stored1,) = store.upsert([first])
    assert stored1.first_seen == NOW

    later = NOW + timedelta(hours=1)
    second = _finding(first_seen=later, last_seen=later)
    (stored2,) = store.upsert([second])

    assert stored2.fingerprint == stored1.fingerprint
    assert stored2.first_seen == NOW  # kept from the first insert
    assert stored2.last_seen == later  # bumped


def test_get_returns_none_when_missing(tmp_path: Path) -> None:
    store = FindingStore(StateLayout(tmp_path))
    assert store.get("nonexistent") is None


def test_get_by_id(tmp_path: Path) -> None:
    store = FindingStore(StateLayout(tmp_path))
    (stored,) = store.upsert([_finding()])
    assert store.get_by_id(stored.id) == stored
    assert store.get_by_id("F-doesnotexist") is None


def test_list_filters_by_status_layer_severity_source(tmp_path: Path) -> None:
    store = FindingStore(StateLayout(tmp_path))
    a = _finding(layer=1, severity="error", source="check:cross_chip")
    b = _finding(
        layer=4,
        severity="warning",
        source="lint",
        evidence=(Evidence(file="design/timer/rtl/m_cnt.sv", line=99),),
        claim="latch inferred",
        artifact_hashes={},
        artifacts=(),
    )
    store.upsert([a, b])

    assert [f.source for f in store.list(layer=1)] == ["check:cross_chip"]
    assert [f.source for f in store.list(severity="warning")] == ["lint"]
    assert [f.source for f in store.list(source="lint")] == ["lint"]
    assert len(store.list(status="open")) == 2


def test_mark_fixed_marks_findings_not_reseen(tmp_path: Path) -> None:
    store = FindingStore(StateLayout(tmp_path))
    a = _finding()
    b = _finding(
        evidence=(Evidence(file="design/timer/rtl/m_cnt.sv", line=44),),
        claim="another issue",
        artifact_hashes={},
        artifacts=(),
    )
    (stored_a, stored_b) = store.upsert([a, b])

    fixed = store.mark_fixed([stored_a.fingerprint], source="check:cross_chip")

    assert [f.fingerprint for f in fixed] == [stored_b.fingerprint]
    assert store.get(stored_a.fingerprint).status == "open"  # type: ignore[union-attr]
    assert store.get(stored_b.fingerprint).status == "fixed"  # type: ignore[union-attr]


def test_upsert_keeps_waived_status_on_rediscovery(tmp_path: Path) -> None:
    store = FindingStore(StateLayout(tmp_path))
    (stored,) = store.upsert([_finding()])
    waived = stored.model_copy(update={"status": "waived"})
    store.upsert([waived])

    rediscovered = _finding(first_seen=NOW + timedelta(hours=2), last_seen=NOW + timedelta(hours=2))
    (result,) = store.upsert([rediscovered])
    assert result.status == "waived"


def test_effective_status_waived_when_waiver_current() -> None:
    finding = _finding()
    waiver = _approval(finding)
    current_hashes = dict(finding.artifact_hashes)
    assert effective_status(finding, [waiver], current_hashes) == "waived"


def test_effective_status_open_when_no_waiver() -> None:
    finding = _finding()
    current_hashes = dict(finding.artifact_hashes)
    assert effective_status(finding, [], current_hashes) == "open"


def test_effective_status_expires_when_hash_changes() -> None:
    finding = _finding()
    waiver = _approval(finding)
    current_hashes = {".:design/timer/rtl/m_cnt.sv": SHA_B}  # artifact changed
    assert effective_status(finding, [waiver], current_hashes) == "open"


def test_effective_status_fixed_takes_priority() -> None:
    finding = _finding(status="fixed")
    waiver = _approval(finding)
    current_hashes = dict(finding.artifact_hashes)
    assert effective_status(finding, [waiver], current_hashes) == "fixed"
