"""Tests for `GeneratedCheck`, and its `stamp`/`verify` helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import FakeRunner, make_ctx, write

from chipgraph.checks import GeneratedCheck, stamp, verify
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.protocols import Check
from chipgraph.core.plugin_api.types import ToolContext


def _spec(**args: object) -> CheckSpec:
    return CheckSpec(
        id="digital-rtl/generated", capability="layout", adapter="generated", args=args
    )


def _run(spec: CheckSpec, ctx: ToolContext) -> CheckResult:
    return asyncio.run(GeneratedCheck().run(spec, ctx))


def test_is_a_check() -> None:
    check = GeneratedCheck()
    assert isinstance(check, Check)
    assert check.id == "generated"


def test_stamp_then_verify_is_true() -> None:
    text = "module qnsc_pkg;\nendmodule\n"
    stamped = stamp(text)
    assert verify(stamped) is True
    # Stamping again is a no-op.
    assert stamp(stamped) == stamped


def test_verify_none_when_no_marker() -> None:
    assert verify("module qnsc_pkg;\nendmodule\n") is None


def test_edited_file_fails(tmp_path: Path, runner: FakeRunner) -> None:
    stamped = stamp("module qnsc_pkg;\nendmodule\n")
    edited = stamped + "// extra line added by hand\n"
    write(tmp_path, "design/top/rtl/qnsc_pkg.sv", edited)

    spec = _spec(files=["design/top/rtl/*.sv"])
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "fail"
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.rule == "generated"
    assert issue.severity == "error"
    assert "edited by hand" in issue.msg


def test_no_marker_is_a_warning_by_default(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/top/rtl/qnsc_pkg.sv", "module qnsc_pkg;\nendmodule\n")

    spec = _spec(files=["design/top/rtl/*.sv"])
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "pass"
    assert len(result.issues) == 1
    assert result.issues[0].severity == "warning"
    assert "no generation marker" in result.issues[0].msg


def test_no_marker_is_an_error_with_require_marker(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/top/rtl/qnsc_pkg.sv", "module qnsc_pkg;\nendmodule\n")

    spec = _spec(files=["design/top/rtl/*.sv"], require_marker=True)
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "fail"
    assert result.issues[0].severity == "error"


def test_different_comment_prefixes() -> None:
    for prefix, expected_start in (
        ("//", "// chipgraph:generated"),
        ("#", "# chipgraph:generated"),
        ("--", "-- chipgraph:generated"),
        (";", "; chipgraph:generated"),
        ("<!--", "<!-- chipgraph:generated"),
    ):
        stamped = stamp("some content\nmore content\n", comment_prefix=prefix)
        assert stamped.startswith(expected_start)
        assert verify(stamped) is True


def test_idempotency_key_changes_when_a_file_changes(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/top/rtl/qnsc_pkg.sv", stamp("module qnsc_pkg;\nendmodule\n"))
    spec = _spec(files=["design/top/rtl/*.sv"])
    ctx = make_ctx(tmp_path, runner)

    first = _run(spec, ctx)
    write(tmp_path, "design/top/rtl/qnsc_pkg.sv", stamp("module qnsc_pkg;\n// v2\nendmodule\n"))
    second = _run(spec, ctx)

    assert first.idempotency_key != second.idempotency_key


def test_bad_args_is_an_error(tmp_path: Path, runner: FakeRunner) -> None:
    spec = _spec()
    result = _run(spec, make_ctx(tmp_path, runner))
    assert result.status == "error"
