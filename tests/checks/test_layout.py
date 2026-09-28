"""Tests for `LayoutCheck`."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import FakeRunner, make_ctx, write

from chipgraph.checks import LayoutCheck
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.protocols import Check

_TEMPLATES = {
    "rtl": "design/{block}/rtl/{module}.sv",
    "wrapper": "design/{block}/rtl/m_qnsc_wrap_{ip}.sv",
}
_SCOPE = ["design/**/*.sv"]


def _spec(**args: object) -> CheckSpec:
    return CheckSpec(id="lang-sv/layout", capability="layout", adapter="layout", args=args)


def _run(spec: CheckSpec, ctx) -> CheckResult:  # type: ignore[no-untyped-def]
    return asyncio.run(LayoutCheck().run(spec, ctx))


def test_is_a_check() -> None:
    check = LayoutCheck()
    assert isinstance(check, Check)
    assert check.id == "layout"


def test_pass_when_every_file_matches_a_template(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/timer/rtl/timer.sv", "module timer; endmodule\n")
    write(tmp_path, "design/timer/rtl/m_qnsc_wrap_timer.sv", "module wrap; endmodule\n")

    spec = _spec(templates=_TEMPLATES, scope=_SCOPE)
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "pass"
    assert result.issues == ()


def test_stray_file_fails_with_its_path(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/timer/rtl/timer.sv", "x")
    # Missing the mandatory "rtl/" directory segment: matches no template.
    write(tmp_path, "design/timer/stray.sv", "x")

    spec = _spec(templates=_TEMPLATES, scope=_SCOPE)
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "fail"
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.file == "design/timer/stray.sv"
    assert issue.rule == "layout"
    assert issue.severity == "error"
    assert "not in any layout template" in issue.msg


def test_ignore_exempts_files(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/timer/rtl/timer.sv", "x")
    write(tmp_path, "vendor/thirdparty/rtl/odd_name.sv", "x")

    spec = _spec(
        templates=_TEMPLATES,
        scope=["design/**/*.sv", "vendor/**/*.sv"],
        ignore=["vendor/**"],
    )
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "pass"


def test_values_violation(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/mystery/rtl/mystery.sv", "x")

    spec = _spec(
        templates=_TEMPLATES,
        scope=_SCOPE,
        values={"block": ["timer", "pwm"]},
    )
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "fail"
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.file == "design/mystery/rtl/mystery.sv"
    assert "block `mystery` is not one of" in issue.msg


def test_double_star_template_matches_any_depth(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/timer/rtl/sub/timer_core.sv", "x")

    spec = _spec(
        templates={"rtl": "design/{block}/rtl/**/*.sv"},
        scope=_SCOPE,
    )
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "pass"


def test_identical_templates_is_a_bad_args_error(tmp_path: Path, runner: FakeRunner) -> None:
    dup_template = "design/{block}/rtl/{module}.sv"
    spec = _spec(
        templates={"rtl": dup_template, "dup": dup_template},
        scope=_SCOPE,
    )
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "error"
    assert len(result.issues) == 1
    assert "identical" in result.issues[0].msg


def test_idempotency_key_changes_when_a_file_changes(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/timer/rtl/timer.sv", "module timer; endmodule\n")
    spec = _spec(templates=_TEMPLATES, scope=_SCOPE)
    ctx = make_ctx(tmp_path, runner)

    first = _run(spec, ctx)
    write(tmp_path, "design/timer/rtl/timer.sv", "module timer; // changed\nendmodule\n")
    second = _run(spec, ctx)

    assert first.idempotency_key != second.idempotency_key
