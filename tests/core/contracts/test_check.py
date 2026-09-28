"""Tests for CheckSpec, Issue, and CheckResult."""

import pytest
from pydantic import ValidationError

from chipgraph.core.contracts import CheckResult, CheckSpec, Issue


def test_check_spec_round_trip() -> None:
    spec = CheckSpec(id="lint/verible", capability="lint", adapter="cmd", args={"flags": ["-v"]})
    assert CheckSpec.model_validate_json(spec.model_dump_json()) == spec


def test_check_result_round_trip() -> None:
    result = CheckResult(
        check_id="lint/verible",
        status="fail",
        issues=(Issue(file="a.sv", line=3, msg="bad indent"),),
        log_tail="some log",
        duration_s=1.5,
        idempotency_key="key-1",
    )
    assert CheckResult.model_validate_json(result.model_dump_json()) == result


def test_check_result_ok_property() -> None:
    passed = CheckResult(check_id="x", status="pass", duration_s=0.1, idempotency_key="k")
    skipped = CheckResult(check_id="x", status="skipped", duration_s=0.0, idempotency_key="k")
    failed = CheckResult(check_id="x", status="fail", duration_s=0.1, idempotency_key="k")
    assert passed.ok
    assert skipped.ok
    assert not failed.ok


def test_check_result_pass_with_error_issue_rejected() -> None:
    with pytest.raises(ValidationError):
        CheckResult(
            check_id="x",
            status="pass",
            issues=(Issue(msg="oops", severity="error"),),
            duration_s=0.1,
            idempotency_key="k",
        )


def test_check_result_pass_with_warning_issue_ok() -> None:
    result = CheckResult(
        check_id="x",
        status="pass",
        issues=(Issue(msg="fyi", severity="warning"),),
        duration_s=0.1,
        idempotency_key="k",
    )
    assert result.ok


def test_check_result_log_tail_is_truncated() -> None:
    long_log = "x" * 5000
    result = CheckResult(
        check_id="x", status="pass", log_tail=long_log, duration_s=0.1, idempotency_key="k"
    )
    assert len(result.log_tail) == 4000
    assert result.log_tail.startswith("…")
    assert result.log_tail[1:] == long_log[-3999:]


def test_check_result_log_tail_under_limit_untouched() -> None:
    result = CheckResult(
        check_id="x", status="pass", log_tail="short", duration_s=0.1, idempotency_key="k"
    )
    assert result.log_tail == "short"


def test_issue_line_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Issue(msg="bad", line=0)
