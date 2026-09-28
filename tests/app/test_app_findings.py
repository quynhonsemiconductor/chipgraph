"""Tests for `chipgraph.app.findings`: check-result -> Finding, and waiving."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import init_git, write_profile

from chipgraph.adapters.review.file import FileReview
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.app.findings import findings_from_check, layer_for_check, waive
from chipgraph.core.contracts import CheckResult, Evidence, Finding, Issue
from chipgraph.core.state.findings import FindingStore, effective_status, waiver_gate_id


def test_layer_for_check_known_ids() -> None:
    assert layer_for_check("cross_chip") == 1
    assert layer_for_check("lint") == 4
    assert layer_for_check("trace") == 5


def test_layer_for_check_default_is_1() -> None:
    assert layer_for_check("some_unknown_check") == 1


def test_findings_from_check_with_file_evidence() -> None:
    result = CheckResult(
        check_id="lint",
        status="fail",
        issues=(
            Issue(file="design/timer/rtl/m_cnt.sv", line=12, rule="latch", msg="latch inferred"),
        ),
        duration_s=0.1,
        idempotency_key="k",
    )
    findings = findings_from_check(result, layer=4)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.layer == 4
    assert finding.source == "check:lint"
    assert finding.evidence[0].file == "design/timer/rtl/m_cnt.sv"
    assert finding.evidence[0].line == 12
    assert finding.claim == "latch inferred"
    assert finding.severity == "error"


def test_findings_from_check_without_file_uses_check_id_as_model_key() -> None:
    result = CheckResult(
        check_id="nope",
        status="error",
        issues=(Issue(severity="error", msg="no adapter configured"),),
        duration_s=0.0,
        idempotency_key="k",
    )
    findings = findings_from_check(result)

    assert len(findings) == 1
    evidence = findings[0].evidence[0]
    assert evidence.file is None
    assert evidence.model_key == "check:nope"


def test_findings_from_check_no_issues_is_empty() -> None:
    result = CheckResult(check_id="ok", status="pass", duration_s=0.1, idempotency_key="k")
    assert findings_from_check(result) == []


def test_findings_from_check_records_artifact_hashes(tmp_path: Path) -> None:
    init_git(tmp_path)
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "m_cnt.sv").write_text("module m_cnt; endmodule\n")
    write_profile(tmp_path, "project: demo\nadapters: {}\n")
    ctx = AppContext.load(tmp_path)

    result = CheckResult(
        check_id="lint",
        status="fail",
        issues=(Issue(file="design/m_cnt.sv", line=1, msg="bad"),),
        duration_s=0.1,
        idempotency_key="k",
    )
    (finding,) = findings_from_check(result, layer=4, store=ctx.store)

    assert finding.artifacts[0].path == "design/m_cnt.sv"
    assert ".:design/m_cnt.sv" in finding.artifact_hashes


def _waivable_finding(tmp_path: Path) -> tuple[AppContext, Finding]:
    init_git(tmp_path)
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "m_cnt.sv").write_text("module m_cnt; endmodule\n")
    write_profile(tmp_path, "project: demo\nadapters: {}\n")
    ctx = AppContext.load(tmp_path)

    result = CheckResult(
        check_id="lint",
        status="fail",
        issues=(Issue(file="design/m_cnt.sv", line=1, msg="bad"),),
        duration_s=0.1,
        idempotency_key="k",
    )
    (finding,) = findings_from_check(result, layer=4, store=ctx.store)
    store = FindingStore(ctx.layout)
    (stored,) = store.upsert([finding])
    return ctx, stored


def test_waive_records_decision_and_marks_waived(tmp_path: Path) -> None:
    ctx, finding = _waivable_finding(tmp_path)
    store = FindingStore(ctx.layout)

    waived = waive(
        finding,
        by="nghiavt",
        reason="tracked in JIRA-42",
        store=store,
        review=ctx.review,
        current_hashes=ctx.store.current_hashes(finding.artifacts),
    )

    assert waived.status == "waived"
    decision_files = list((ctx.root / ".chipgraph" / "decisions").glob("*.yml"))
    assert len(decision_files) == 1

    waivers = ctx.review.approvals(waiver_gate_id(finding.id))
    current_hashes = ctx.store.current_hashes(finding.artifacts)
    assert effective_status(waived, waivers, current_hashes) == "waived"


def test_waive_requires_a_reason(tmp_path: Path) -> None:
    ctx, finding = _waivable_finding(tmp_path)
    store = FindingStore(ctx.layout)
    with pytest.raises(ValueError, match="reason"):
        waive(finding, by="nghiavt", reason="   ", store=store, review=ctx.review)


def test_waive_expires_when_artifact_changes(tmp_path: Path) -> None:
    ctx, finding = _waivable_finding(tmp_path)
    store = FindingStore(ctx.layout)
    waive(
        finding,
        by="nghiavt",
        reason="tracked",
        store=store,
        review=ctx.review,
        current_hashes=ctx.store.current_hashes(finding.artifacts),
    )

    (tmp_path / "design" / "m_cnt.sv").write_text("module m_cnt; wire a; endmodule\n")

    waivers = ctx.review.approvals(waiver_gate_id(finding.id))
    current_hashes = ctx.store.current_hashes(finding.artifacts)
    assert effective_status(finding, waivers, current_hashes) == "open"


def test_waive_without_artifacts_raises_app_error() -> None:
    from datetime import UTC, datetime

    finding = Finding(
        layer=1,
        severity="warning",
        source="critic",
        evidence=(Evidence(model_key="design.timer.reset_polarity"),),
        claim="ambiguous",
        first_seen=datetime.now(UTC),
        last_seen=datetime.now(UTC),
    )
    store = FindingStore.__new__(FindingStore)  # not used; waive fails before touching it
    review = FileReview(Path("/does/not/matter"))
    with pytest.raises(AppError):
        waive(finding, by="nghiavt", reason="ok", store=store, review=review)
