"""Tests for `FilelistCheck`."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import FakeRunner, make_ctx, write

from chipgraph.checks import FilelistCheck
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.protocols import Check
from chipgraph.core.plugin_api.types import ToolContext

_FILELIST = "design/{block}/{block}.f"
_SOURCES = "design/{block}/rtl/**/*.sv"


def _spec(**args: object) -> CheckSpec:
    return CheckSpec(id="lang-sv/filelist", capability="layout", adapter="filelist", args=args)


def _run(spec: CheckSpec, ctx: ToolContext) -> CheckResult:
    return asyncio.run(FilelistCheck().run(spec, ctx))


def test_is_a_check() -> None:
    check = FilelistCheck()
    assert isinstance(check, Check)
    assert check.id == "filelist"


def test_pass_with_package_first_nested_include_and_comments(
    tmp_path: Path, runner: FakeRunner
) -> None:
    write(tmp_path, "design/timer/rtl/timer_pkg.sv", "package timer_pkg; endpackage\n")
    write(tmp_path, "design/timer/rtl/timer.sv", "module timer; endmodule\n")
    write(tmp_path, "design/timer/rtl/sub/timer_core.sv", "module timer_core; endmodule\n")
    write(
        tmp_path,
        "design/timer/sub.f",
        "rtl/sub/timer_core.sv\n",
    )
    write(
        tmp_path,
        "design/timer/timer.f",
        "// header comment\n"
        "+incdir+rtl\n"
        "rtl/timer_pkg.sv  # package first\n"
        "-F sub.f\n"
        "rtl/timer.sv\n",
    )

    spec = _spec(filelist=_FILELIST, sources=_SOURCES)
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "pass", result.issues
    assert result.issues == ()


def test_missing_listed_file_reports_line_number(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/pwm/rtl/pwm.sv", "module pwm; endmodule\n")
    write(
        tmp_path,
        "design/pwm/pwm.f",
        "rtl/pwm.sv\nrtl/ghost.sv\n",
    )

    spec = _spec(filelist=_FILELIST, sources=_SOURCES)
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "fail"
    missing = [i for i in result.issues if "listed but missing" in i.msg]
    assert len(missing) == 1
    assert missing[0].file == "design/pwm/pwm.f"
    assert missing[0].line == 2
    assert missing[0].severity == "error"


def test_unlisted_source(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/gpio/rtl/gpio.sv", "module gpio; endmodule\n")
    write(tmp_path, "design/gpio/gpio.f", "")

    spec = _spec(filelist=_FILELIST, sources=_SOURCES)
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "fail"
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.file == "design/gpio/rtl/gpio.sv"
    assert "is not in" in issue.msg


def test_package_order_violation(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/spi/rtl/spi.sv", "module spi; endmodule\n")
    write(tmp_path, "design/spi/rtl/spi_pkg.sv", "package spi_pkg; endpackage\n")
    write(
        tmp_path,
        "design/spi/spi.f",
        "rtl/spi.sv\nrtl/spi_pkg.sv\n",
    )

    spec = _spec(filelist=_FILELIST, sources=_SOURCES)
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "fail"
    order_issues = [i for i in result.issues if i.rule == "order"]
    assert len(order_issues) == 1
    assert order_issues[0].line == 2
    assert "must come before" in order_issues[0].msg


def test_duplicate_entry_is_a_warning(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/i2c/rtl/i2c.sv", "module i2c; endmodule\n")
    write(
        tmp_path,
        "design/i2c/i2c.f",
        "rtl/i2c.sv\nrtl/i2c.sv\n",
    )

    spec = _spec(filelist=_FILELIST, sources=_SOURCES)
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "pass"
    dup = [i for i in result.issues if "listed twice" in i.msg]
    assert len(dup) == 1
    assert dup[0].severity == "warning"


def test_absolute_path_is_a_warning(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/uart/rtl/uart.sv", "module uart; endmodule\n")
    abs_path = (tmp_path / "design/uart/rtl/uart.sv").resolve()
    write(tmp_path, "design/uart/uart.f", f"{abs_path}\n")

    spec = _spec(filelist=_FILELIST, sources=_SOURCES)
    result = _run(spec, make_ctx(tmp_path, runner))

    warnings = [i for i in result.issues if i.severity == "warning"]
    assert len(warnings) == 1
    assert "absolute path" in warnings[0].msg
    assert result.status == "pass"


def test_env_var_expansion_and_unknown_var_warning(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/adc/rtl/adc.sv", "module adc; endmodule\n")
    write(
        tmp_path,
        "design/adc/adc.f",
        "$RTL_DIR/adc.sv\n$MISSING_VAR/ghost.sv\n",
    )

    spec = _spec(filelist=_FILELIST, sources=_SOURCES)
    ctx = make_ctx(tmp_path, runner, env={"RTL_DIR": "rtl"})
    result = _run(spec, ctx)

    assert result.status == "pass", result.issues
    warnings = [i for i in result.issues if "unknown environment variable" in i.msg]
    assert len(warnings) == 1
    assert warnings[0].line == 2


def test_block_without_filelist(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/dma/rtl/dma.sv", "module dma; endmodule\n")

    spec = _spec(filelist=_FILELIST, sources=_SOURCES)
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "fail"
    assert len(result.issues) == 1
    assert "has RTL sources but no filelist" in result.issues[0].msg


def test_bad_args_is_an_error(tmp_path: Path, runner: FakeRunner) -> None:
    spec = _spec(filelist="design/{block}/{other}.f", sources=_SOURCES)
    result = _run(spec, make_ctx(tmp_path, runner))

    assert result.status == "error"


def test_idempotency_key_changes_when_a_file_changes(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/wdt/rtl/wdt.sv", "module wdt; endmodule\n")
    write(tmp_path, "design/wdt/wdt.f", "design/wdt/rtl/wdt.sv\n")

    spec = _spec(filelist=_FILELIST, sources=_SOURCES)
    ctx = make_ctx(tmp_path, runner)

    first = _run(spec, ctx)
    write(tmp_path, "design/wdt/rtl/wdt.sv", "module wdt; // changed\nendmodule\n")
    second = _run(spec, ctx)

    assert first.idempotency_key != second.idempotency_key
